# pyright: strict, reportPrivateUsage=false
"""OOXML field substitution adapter used by the rendering application service."""

import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any, cast

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue] lxml has incomplete stubs.

from carbotek._native_adapter import OP_CENTS, OP_IF_EQUAL, OP_ISO_DATE, OP_RU_NUMBER
from carbotek.errors import CarbotekError, FormatterError, MarkerResolutionError
from carbotek.formatters import FormatterRegistry, MarkerContext, shared_registry
from carbotek.formatters import condition as conditions
from carbotek.formatters.aggregator import js_join
from carbotek.formatters.aggregator import parse_float_js as js_parse_float
from carbotek.formatters.date import format_date
from carbotek.formatters.jsnumber import js_to_string
from carbotek.formatters.number import format_number, number_string
from carbotek.formatters.registry import UnknownFormatterError
from carbotek.formatters.ru import digit_currency
from carbotek.formatters.string import LINE_BREAK, strip_xml_controls
from carbotek.markers import (
    Marker,
    VariableDeclaration,
    VariableEnvironment,
    VariableExpressionError,
    parse,
    parse_declaration,
    scan,
    substitute,
)
from carbotek.markers.math import BadMathematicalExpression, is_math_candidate
from carbotek.markers.math import evaluate as math_evaluate
from carbotek.markers.math import terms as math_terms
from carbotek.markers.syntax import FormatterCall, printed
from carbotek.ooxml.ns import qn
from carbotek.ooxml.xml import parse_xml, sanitize_xml_text, serialize_xml
from carbotek.options import RenderOptions
from carbotek.render_state import StateSlot
from carbotek.typing_helpers import is_list, is_mapping

from .blocks import HiddenBlocks
from .dialect import DIALECT, SHEET, WORD, Dialect
from .directives import (
    DEFERRED_DIRECTIVES,
    StructuralDirectiveExecutor,
    compile_directive_spans,
    is_directive,
    marker_nodes_outside,
    remove_span_boundaries,
)
from .loops import (
    COMPLEMENT_ROOT,
    COPY_FEATURES,
    INDEX_SUFFIX,
    KEEP_RENDERED,
    LOOP_SEGMENT,
    PART_HAS_BLOCKS,
    RENDERED_COPIES,
    CopyFeatures,
    LoopExpander,
    _filter_values,
)
from .normalize import normalize_document, rename_bookmarks_in_copy, repair_bookmarks, repair_empty_table_cells
from .plan import PartPlan, SetStatement, analyze_part, extract_sets
from .session import CURRENT_RENDER, RenderSession, render_session
from .set_markers import SET_MARKER, SetTarget, decompose, with_iterators
from .strings import (
    IS_HIDDEN,
    RENDERED_TAG,
    TEXT_COMPILER,
    PlainField,
    TextCompiler,
    TextPiece,
    materialize,
)

_TEMPLATED_PARTS = ("word/document.xml", "word/header", "word/footer", "word/footnotes.xml", "word/endnotes.xml")
_INDEXED_SEGMENT = re.compile(r"^(?P<field>[^[]*)\[(?P<index>\d+)]$")
_ITERATOR_SEGMENT = LOOP_SEGMENT
_AGGREGATE_FILTER_SEGMENT = re.compile(r"^(?P<field>[^[]+)\[(?P<filters>[^]]+)]$")


@dataclass(frozen=True, slots=True)
class DocxTextRenderer:
    """Substitutes basic fields in parsed text nodes, never manipulating XML as strings."""

    registry: FormatterRegistry = field(default_factory=shared_registry)

    def create_session(self, data: Mapping[str, Any] | list[Any], options: RenderOptions) -> RenderSession:
        context = MarkerContext(
            data=data if isinstance(data, dict) else {}, complement=deepcopy(dict(options.complement))
        )
        context.complement.setdefault("now", datetime.now(UTC).isoformat())
        declarations = {
            declaration.name: declaration
            for occurrence in scan(options.variable_str, include_aliases=True)
            if (declaration := parse_declaration(occurrence.raw)) is not None
        }
        variables = VariableEnvironment(context.data, context.complement, declarations)
        options = replace(options, complement=context.complement)

        # Capture the evaluation inputs, not the owning session: a self-reference retains XML
        # trees until cyclic GC even after the public render context has been released.
        def evaluator(marker: Marker, scope: Mapping[str, Any], condition_result: bool) -> Any:
            return self._evaluate(marker, data, options, scope, variables, context, condition_result=condition_result)

        return RenderSession(data, options, context, variables, evaluator)

    def compile_part(
        self,
        content: bytes,
        *,
        dialect: Dialect = WORD,
    ) -> tuple[bytes, PartPlan, tuple[SetStatement, ...]]:
        root = parse_xml(content)
        token = DIALECT.set(dialect)
        try:
            self._validate_formatters(root)
            spans = compile_directive_spans(
                root,
                can_promote=lambda marker: _can_promote_crossing_value(marker, self.registry),
                loop_ranges=lambda: LoopExpander.template_ranges(root),
            )
            plan = analyze_part(root, dialect, self.registry)
            statements = extract_sets(root, dialect)
            return serialize_xml(root) if statements or spans else content, plan, statements
        finally:
            DIALECT.reset(token)

    def execute_sets(self, statements: tuple[SetStatement, ...], session: RenderSession) -> None:
        for statement in statements:
            self._run_set(statement.marker, statement.target, session.data, session.options, session.context, {}, 0)
        session.preapplied_sets = True

    def render_part(
        self,
        content: bytes,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        *,
        normalized: bool = False,
    ) -> bytes:
        """Render one WordprocessingML part and serialize valid XML."""
        return serialize_xml(self.render_tree(content, data, options, normalized=normalized))

    def render_tree(
        self,
        content: bytes,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        *,
        normalized: bool = False,
        dialect: Dialect = WORD,
    ) -> etree._Element:
        """Render one WordprocessingML (or SpreadsheetML) part into a tree the post-phase features keep working on."""
        return copy_context().run(
            self._isolated_render_tree, content, data, options, normalized=normalized, dialect=dialect
        )

    def _isolated_render_tree(
        self,
        content: bytes,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        *,
        normalized: bool,
        dialect: Dialect,
    ) -> etree._Element:
        if CURRENT_RENDER.get() is None:
            working = deepcopy(dict(data)) if is_mapping(data) else deepcopy(data)
            session = self.create_session(working, options)
            session.register_features = False
            with render_session(session):
                root = self._isolated_render_tree(content, working, options, normalized=normalized, dialect=dialect)
                return parse_xml(session.restore(serialize_xml(root)))
        token = DIALECT.set(dialect)
        hidden_token = PART_HIDDEN_BLOCKS.set(HiddenBlocks())
        try:
            return self._render_tree(content, data, options, normalized=normalized, dialect=dialect)
        finally:
            PART_HIDDEN_BLOCKS.reset(hidden_token)
            DIALECT.reset(token)

    def _render_tree(
        self,
        content: bytes,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        *,
        normalized: bool,
        dialect: Dialect,
    ) -> etree._Element:
        root = parse_xml(content)
        word = dialect is WORD
        if not normalized and word:
            normalize_document(root)  # a compiled template is already normalized
        session = CURRENT_RENDER.get()
        if session is None or session.part_plan is None:
            compile_directive_spans(
                root,
                can_promote=lambda marker: _can_promote_crossing_value(marker, self.registry),
                loop_ranges=lambda: LoopExpander.template_ranges(root),
            )
        runtime_data = (
            session.data if session is not None else deepcopy(dict(data)) if is_mapping(data) else deepcopy(data)
        )
        context = (
            session.context
            if session is not None
            else MarkerContext(
                data=runtime_data if isinstance(runtime_data, dict) else {},
                complement=deepcopy(dict(options.complement)),
            )
        )
        if session is None or not session.preapplied_sets:
            self._preapply_sets(root, runtime_data, options, context)
        plan = session.part_plan if session is not None else None
        if session is not None and session.preapplied_sets and plan is not None and not plan.aliases:
            variables = session.variables
        else:
            variables = self._collect_variables(
                root,
                context.data,
                context.complement,
                options.variable_str,
                session.variables.declarations if session is not None else None,
            )
            if session is not None:
                session.variables = variables
        if plan is None or plan.aliases:
            plan = self._analyze_render_part(root, dialect, validate=plan is None)
        if session is not None:
            session.features.update(plan.features)
        context.reserved_bookmark_names.update(
            name for start in root.iter(qn("w:bookmarkStart")) if (name := start.get(qn("w:name")))
        )
        # Structural directives run inside _render_element: once per loop copy with its iterators,
        # then for the rest of the part, so :drop/:keep also work on repeated rows and paragraphs.
        copies: list[list[etree._Element]] = []
        copies_token = RENDERED_COPIES.set(copies)
        deferred_token = DEFERRED_DIRECTIVES.set(False)
        blocks = PART_HAS_BLOCKS.set(plan.blocks)
        compiler = TEXT_COMPILER.set(self._text_compiler(runtime_data, options, variables, context))
        hidden_state = IS_HIDDEN.set(lambda: context.is_hidden)
        # Color bindings rewrite runs of the whole part after rendering: copies must be in the tree.
        keep = KEEP_RENDERED.set(True)
        try:
            LoopExpander().expand(
                root,
                {**runtime_data, COMPLEMENT_ROOT: context.complement}
                if isinstance(runtime_data, dict)
                else runtime_data,
                lambda element, iterators: self._render_element(
                    element, runtime_data, options, iterators, variables, context
                ),
            )
            deferred = DEFERRED_DIRECTIVES.get()
        finally:
            DEFERRED_DIRECTIVES.reset(deferred_token)
            PART_HAS_BLOCKS.reset(blocks)
            RENDERED_COPIES.reset(copies_token)
            TEXT_COMPILER.reset(compiler)
            IS_HIDDEN.reset(hidden_state)
            KEEP_RENDERED.reset(keep)
        # Loop copies are rendered: the pass over the rest of the part does not need to walk them,
        # unless a copy left a directive for it or a conditional block may hide text inside them.
        # Column directives change every row, copies included: keep the copies attached then.
        columns = plan.columns
        rendered: set[etree._Element] = set()
        for run in copies:
            for copy in run:
                if (deferred or columns) and copy.tag == RENDERED_TAG:
                    rendered.update(materialize(copy))
                else:
                    rendered.add(copy)
        if session is not None:
            for copy in rendered:
                session.rendered_texts.update(copy.iter(*dialect.texts))
        directive_token = DEFERRED_DIRECTIVES.set(deferred or columns)
        detached_token = SKIPPED_COPIES.set(rendered)
        # What the whole-part pass can meet is known from the (small) template: markers in attributes
        # and drop/keep directives. Loop copies were rendered already; a big expanded document is
        # then not walked attribute by attribute for nothing.
        part_features = COPY_FEATURES.set(
            CopyFeatures(
                attribute_markers=plan.attribute_markers,
                directives=plan.directives,
                blocks=plan.blocks,
            )
        )
        try:
            self._render_element(root, runtime_data, options, {}, variables, context)
        finally:
            COPY_FEATURES.reset(part_features)
            SKIPPED_COPIES.reset(detached_token)
            DEFERRED_DIRECTIVES.reset(directive_token)
        if not word:
            remove_span_boundaries(root)
            _plain_line_breaks(root)
            return root
        _inject_line_breaks(root)
        remove_span_boundaries(root)
        repair_empty_table_cells(root)
        repair_bookmarks(root)
        return root

    def _analyze_render_part(self, root: etree._Element, dialect: Dialect, *, validate: bool) -> PartPlan:
        # Compiled parts were validated already.
        if validate:
            self._validate_formatters(root)
        return analyze_part(root, dialect, self.registry)

    def _text_compiler(
        self,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        variables: VariableEnvironment,
        context: MarkerContext,
    ) -> TextCompiler:
        """Compile a loop-copy text or attribute into literals and per-item marker functions.

        Mirrors ``_replace`` / ``_render_attribute`` for a copy that no conditional block hides:
        markers of other passes stay literal, deferred ones are frozen, the rest is resolved (a
        compiled function first, the general ``_resolve`` otherwise).
        """

        def resolved(marker: Marker, native_text: bool) -> TextPiece:
            compiled = _compiled_marker(self.registry, marker)

            def piece(iterators: Mapping[str, Any]) -> str:
                if compiled is not None and (value := compiled(iterators, options, context)) is not _NO_ITERATOR:
                    return cast(str, value)
                return self._resolve(marker, data, options, iterators, variables, context)

            if (
                native_text
                and compiled is not None
                and marker.root == "d"
                and (plan := _loop_item_plan(marker.path)) is not None
                and len(plan[1]) == 1
            ):
                return _native_plain_field(marker, plan, piece, self.registry, options)
            return piece

        def frozen(raw: str) -> TextPiece:
            return lambda iterators: "" if context.is_hidden else freeze_marker(raw, iterators)

        def replaced(text: str) -> TextPiece:
            return lambda iterators: self._replace(text, data, options, iterators, variables, context)

        def compile_text(text: str, tag: str, attribute: str | None, blocks: bool) -> list[TextPiece] | None:
            if blocks and attribute is None:
                stateful = any(
                    call.name in _BLOCK_FORMATTERS or self.registry.metadata(call.name).uses_marker_context
                    for occurrence in scan(text)
                    if (marker_plan := _marker_plan(occurrence.raw)).marker is not None
                    for call in marker_plan.marker.formatters
                )
                if stateful or (compiled_pieces := compile_text(text, tag, attribute, False)) is None:
                    return [replaced(text)]  # block transitions need the ordered tree semantics

                return [_visible_text(compiled_pieces, context)]
            if context.is_hidden:
                return None
            pieces: list[TextPiece] = []
            position = 0
            deferred_attribute = attribute is not None and (
                (tag == qn("wp:docPr") and attribute == "descr") or attribute == qn("r:id")
            )
            for occurrence in scan(text):
                if attribute is None:
                    plan = _marker_plan(occurrence.raw)
                    if plan.marker is None:
                        continue  # left to its own pass, like _replace
                    marker, deferred = plan.marker, plan.deferred
                else:
                    marker = parse(occurrence.raw)
                    deferred = (
                        deferred_attribute
                        or marker.root not in {"d", "c"}
                        or _is_deferred_marker(marker, attribute=True)
                    )
                if any(formatter.name in _BLOCK_FORMATTERS for formatter in marker.formatters):
                    return None
                pieces.append(text[position : occurrence.start])
                pieces.append(frozen(occurrence.raw) if deferred else resolved(marker, attribute is None))
                position = occurrence.end
            pieces.append(text[position:])
            return pieces

        return compile_text

    def _validate_formatters(self, root: etree._Element) -> None:
        """Reject unknown formatters before rendering, like the JS builder compiling every marker.

        JS fails even for markers that would never execute (a loop over an empty array, a hidden
        block), so the check covers the whole part, not only the markers that get evaluated.
        Structural directives are handled by the preprocessor in JS and are skipped here.
        """
        blocks: list[str] = []
        for node in root.iter(*DIALECT.get().texts, qn("wp:docPr")):
            if node.tag == qn("wp:docPr"):
                self._validate_image_marker(node)
                continue
            for occurrence in scan(node.text or ""):
                if is_directive(occurrence.raw) or _is_bind_color(occurrence.raw):
                    continue
                _validate_marker_syntax(occurrence.raw)
                marker = parse(occurrence.raw)
                _validate_conditional_blocks(marker, blocks, self.registry)
                for call in marker.formatters:
                    if call.name not in self.registry:
                        error = self.registry.unknown(call.name)
                        raise UnknownFormatterError(f'{error} Source: "{printed(occurrence.raw)}"')
                    if self.registry.metadata(call.name).accepts_math and call.arguments:
                        try:
                            math_terms(call.arguments[0])
                        except BadMathematicalExpression as error:
                            raise BadMathematicalExpression(f'{error} Source: "{printed(occurrence.raw)}"') from error
        if blocks:
            raise MarkerResolutionError("Missing showEnd or hideEnd of these tags: " + ", ".join(blocks))

    def _validate_image_marker(self, properties: etree._Element) -> None:
        """Unknown formatters of an image marker, reported as JS prints its rewritten marker.

        lib/image.js turns the alt-text marker into ``:scaleImage(width, cx, emu, fit)`` on
        ``wp:extent`` (``contain``/``fillWidth``) or ``:generateImageDocxId()`` on ``pic:cNvPr``
        (``fill``); the first of them in document order is the one the builder rejects.
        """
        description = properties.get("descr", "")
        fits = re.findall(r":imageFit\(['\"]?(.*?)['\"]?\)", description)
        fit = next((item for item in reversed(fits) if item in {"contain", "fill", "fillWidth"}), "fillWidth")
        occurrences = scan(re.sub(r":imageFit\(['\"]?(.*?)['\"]?\)", "", description))
        occurrence = next((item for item in occurrences if not is_directive(item.raw)), None)
        if occurrence is None or _is_bind_color(occurrence.raw):
            return
        body = printed(occurrence.raw)[1:-1].replace(":barcode", ":isImage:barcode")
        for call in parse("{" + body + "}").formatters:
            if call.name in self.registry:
                continue
            drawing = properties.getparent()
            extent = drawing.find(qn("wp:extent")) if drawing is not None else None
            if fit != "fill" and extent is not None:
                source = f"{{{body}:scaleImage(width,{extent.get('cx')},emu,{fit})}}"
            else:
                source = f"{{{body}:generateImageDocxId()}}"
            raise UnknownFormatterError(f'{self.registry.unknown(call.name)} Source: "{source}"')

    @staticmethod
    def is_templated(name: str) -> bool:
        """Whether an OPC part belongs to the DOCX templating surface."""
        return name.startswith(_TEMPLATED_PARTS)

    def preapply_sets(
        self,
        root: etree._Element,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        context: MarkerContext,
        *,
        dialect: Dialect | None = None,
    ) -> None:
        token = DIALECT.set(dialect or WORD)
        try:
            self._preapply_sets(root, data, options, context)
        finally:
            DIALECT.reset(token)

    def _preapply_sets(
        self, root: etree._Element, data: Mapping[str, Any] | list[Any], options: RenderOptions, context: MarkerContext
    ) -> None:
        """Remove the ``:set`` markers of the part and execute them first, in document order (JS)."""
        pending: list[tuple[str, SetTarget]] = []
        for text in root.iter(*DIALECT.get().texts):
            content = text.text or ""
            if ":set(" not in content:
                continue

            def extract(match: re.Match[str]) -> str:
                full = match.group(0)
                target = decompose(match.group(2), full)
                pending.append((with_iterators(match.group(1), full) + "}", target))
                return ""

            text.text = SET_MARKER.sub(extract, content)
        for raw, target in pending:
            marker = parse(raw)
            if marker.root not in {"d", "c"}:
                continue
            self._run_set(marker, target, data, options, context, {}, 0)

    def _run_set(
        self,
        marker: Marker,
        target: SetTarget,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        context: MarkerContext,
        iterators: Mapping[str, Any],
        start: int,
    ) -> None:
        """Execute one set marker, once per item of each ``[i, …]`` array of its path."""
        path, source = _rooted(marker.root, marker.path, data if marker.root == "d" else context.complement)
        for index in range(start, len(path)):
            match = _LOOP_SEGMENT.fullmatch(path[index])
            if match is None:
                continue
            prefix = path[:index]
            key = ".".join((*prefix, match.group("field")))
            array_path = (*prefix, match.group("field")) if match.group("field") else prefix
            array = self._resolve_path(array_path, source, iterators, False) if array_path else source
            if not is_list(array):
                return
            filters = tuple(
                part.strip()
                for part in (match.group("filters") or "").split(",")
                if part.strip() and not part.strip().startswith("*")
            )
            # :set may append into its source array; keep the pre-write snapshot here.
            values = _filter_values(array, filters) if filters else list(array)
            for item in values:
                self._run_set(marker, target, data, options, context, {**iterators, key: item}, index + 1)
            return
        session = CURRENT_RENDER.get()
        variables = (
            session.variables if session is not None else VariableEnvironment(context.data, context.complement, {})
        )
        value = self._evaluate(marker, data, options, iterators, variables, context)
        if target.root == "d":
            context.parent = data
        elif target.root == "c":
            context.parent = context.complement
        else:
            context.parent = list(iterators.values())[-1] if iterators else data
        for call in target.calls:
            arguments: tuple[Any, ...] = ()
            if call.key is not None:
                arguments = (call.key,)
                if call.value is not None:
                    arguments += (formatter_argument(call.value, data, context.complement, iterators, marker),)
            value = self.registry.apply(call.name, value, arguments, marker_context=context)

    def _render_element(  # noqa: C901 - ordered structural, text and attribute passes
        self,
        element: etree._Element,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        iterators: Mapping[str, Any],
        variables: VariableEnvironment,
        context: MarkerContext,
    ) -> None:
        session = CURRENT_RENDER.get()
        if session is not None:
            session.iterators = iterators
        features = COPY_FEATURES.get()
        rendered = set(SKIPPED_COPIES.get()) if not iterators else _NO_COPIES
        if features is None or features.directives:
            StructuralDirectiveExecutor().apply(
                element,
                lambda marker: self._directive_value(marker, data, options, context, iterators),
                loop_copy=bool(iterators),
                rendered=set() if DEFERRED_DIRECTIVES.get() else rendered,
            )
        if iterators and (features is None or features.bookmarks):
            rename_bookmarks_in_copy(element, context.bookmark_names, context.reserved_bookmark_names)
        if features is None or features.blocks:
            # Conditional blocks hide plain text as well: visit every text node in document order.
            # A rendered loop copy is not walked again, unless a block hides it.
            dialect = DIALECT.get()
            texts = dialect.texts
            # A hidden block's elements are removed after the pass, as JS does.
            hidden = PART_HIDDEN_BLOCKS.get() or HiddenBlocks()
            events = hidden.events
            for node in _texts_and_copies(element, rendered, texts) if rendered else element.iter(*texts):
                if node in rendered or node.tag not in texts:
                    if context.is_hidden:
                        copies = materialize(node) if node.tag == RENDERED_TAG else [node]
                        for text in (text for copy in copies for text in copy.iter(*texts)):
                            text.text = ""
                    continue
                if session is not None and node in session.rendered_texts:
                    if context.is_hidden:
                        node.text = ""
                    continue
                node.text = self._replace(node.text or "", data, options, iterators, variables, context, events)
                if session is not None:
                    session.rendered_texts.add(node)
                if events:
                    hidden.consume(node)
            hidden.remove()
        else:
            for text in _marker_text_nodes(element, iterators, rendered):
                if session is not None and text in session.rendered_texts:
                    continue
                text.text = self._replace(text.text or "", data, options, iterators, variables, context)
                if session is not None:
                    session.rendered_texts.add(text)
        for node in _attribute_marker_nodes(element, features, rendered):
            for name, value in tuple(node.attrib.items()):
                if session is not None and (node, name) in session.rendered_attributes:
                    continue
                if "{" in value and scan(value):
                    if session is not None:
                        session.rendered_attributes.add((node, name))
                    node.set(
                        name, self._render_attribute(node, name, value, data, options, iterators, variables, context)
                    )

    def _render_attribute(
        self,
        node: etree._Element,
        name: str,
        value: str,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        iterators: Mapping[str, Any],
        variables: VariableEnvironment,
        context: MarkerContext,
    ) -> str:
        """Render markers in an XML attribute; features resolved after the loops only get frozen paths."""
        deferred = (node.tag == qn("wp:docPr") and name == "descr") or name == qn("r:id")
        pieces: list[str] = []
        position = 0
        for occurrence in scan(value):
            pieces.append(value[position : occurrence.start])
            marker = parse(occurrence.raw)
            if deferred or marker.root not in {"d", "c"} or _is_deferred_marker(marker, attribute=True):
                pieces.append(freeze_marker(occurrence.raw, iterators))
            else:
                pieces.append(self._resolve(marker, data, options, iterators, variables, context))
            position = occurrence.end
        pieces.append(value[position:])
        return "".join(pieces)

    def _replace(
        self,
        text: str,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        iterators: Mapping[str, Any],
        variables: VariableEnvironment,
        context: MarkerContext,
        events: list[tuple[str, str]] | None = None,
    ) -> str:
        """Render the markers of one text; ``events`` gets where a hidden block starts and ends.

        ``("begin", text before)`` when the text starts being hidden, ``("end", text after)`` when
        it stops, for the structural removal of hidden blocks.
        """
        pieces: list[str] = []
        ends: list[int] = []
        position = 0
        for occurrence in scan(text):
            plan = _marker_plan(occurrence.raw)
            if plan.marker is None:
                continue  # consumed by the color feature / by the structural pass over the whole part
            marker = plan.marker
            if plan.deferred:
                if not context.is_hidden:
                    pieces.append(text[position : occurrence.start])
                    pieces.append(freeze_marker(occurrence.raw, iterators))
                position = occurrence.end
                continue
            was_hidden = context.is_hidden
            compiled = _compiled_marker(self.registry, marker) if iterators else None
            value: Any = compiled(iterators, options, context) if compiled is not None else _NO_ITERATOR
            if value is _NO_ITERATOR:
                value = self._resolve(marker, data, options, iterators, variables, context)
            if not was_hidden:
                pieces.append(text[position : occurrence.start])
            if not was_hidden or _is_block_end(marker):
                pieces.append(value)
            position = occurrence.end
            if events is not None and was_hidden != context.is_hidden:
                if was_hidden:
                    ends.append(len(events))
                    events.append(("end", str(len(pieces))))
                else:
                    events.append(("begin", "".join(pieces)))
        if not context.is_hidden:
            pieces.append(text[position:])
        for index in ends:
            events[index] = ("end", "".join(pieces[int(events[index][1]) :]))  # type: ignore[index]
        return "".join(pieces)

    def _resolve(
        self,
        marker: Marker,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        iterators: Mapping[str, Any],
        variables: VariableEnvironment,
        context: MarkerContext,
    ) -> str:
        return printable(self._evaluate(marker, data, options, iterators, variables, context))

    def _evaluate(
        self,
        marker: Marker,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        iterators: Mapping[str, Any],
        variables: VariableEnvironment,
        context: MarkerContext,
        *,
        condition_result: bool = False,
    ) -> Any:
        if marker.root == "t":
            key = marker.path[0]
            value: Any = options.translations.get(options.lang, {}).get(key, key)
            return self._pipeline(value, marker, options, context, data, iterators, condition_result=condition_result)
        if marker.root == "$":
            try:
                arguments = tuple(_argument(argument) for argument in marker.path[1:])
                return self._pipeline(
                    variables.evaluate(marker.path[0], arguments),
                    marker,
                    options,
                    context,
                    data,
                    iterators,
                    condition_result=condition_result,
                )
            except VariableExpressionError as error:
                raise MarkerResolutionError(str(error)) from error
        source = data if marker.root == "d" else context.complement
        aggregate_index, has_aggregate = _aggregate_shape(marker)
        value = self._resolve_path(*_rooted(marker.root, marker.path, source), iterators, has_aggregate)
        if (
            value is None
            and not has_aggregate
            and any(formatter.name in {"ifEQ", "ifNE", "convCurr"} for formatter in marker.formatters)
            and _missing_marker_path(*_rooted(marker.root, marker.path, source), iterators)
        ):
            value = conditions.JS_UNDEFINED
        if aggregate_index is not None:
            value, marker = self._prepare_aggregate(
                value, marker, aggregate_index, source, iterators, data, options, context
            )
        return self._pipeline(value, marker, options, context, data, iterators, condition_result=condition_result)

    def _prepare_aggregate(
        self,
        value: Any,
        marker: Marker,
        aggregate_index: int,
        source: Mapping[str, Any] | list[Any],
        iterators: Mapping[str, Any],
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        context: MarkerContext,
    ) -> tuple[Any, Marker]:
        prefix: list[str] = []
        target_key: str | None = None
        target_index = -1
        for index, segment in enumerate(marker.path):
            match = _ITERATOR_SEGMENT.fullmatch(segment)
            if match is not None:
                key = ".".join((*prefix, match.group("field")))
                if key in iterators:
                    target_key, target_index = key, index
                prefix.append(match.group("field"))
            else:
                prefix.append(segment)
        outer_iterators = iterators
        context.aggregate_forward = _in_rendered_loop(marker.path, iterators)
        if target_key is not None and not any(segment.endswith("[]") for segment in marker.path[target_index + 1 :]):
            outer_iterators = {key: item for key, item in iterators.items() if key != target_key}
        rows = self._resolve_path(marker.path[:-1], source, outer_iterators, True)
        if not is_list(rows) or not marker.path:
            return value, marker
        if rows and not is_mapping(rows[0]):
            return value, marker
        field = marker.path[-1]
        if field.startswith("[") or "[" in field:
            return value, marker
        aggregate = marker.formatters[aggregate_index]
        group_args = aggregate.arguments[1:] if aggregate.name in {"aggStr", "aggStrD"} else aggregate.arguments
        if iterators and any(argument.startswith(".") for argument in group_args):
            current_groups = tuple(
                formatter_argument(argument, data, context.complement, iterators) for argument in group_args
            )
            current_key = js_join(current_groups, "_")
            rows = [
                row
                for row in rows
                if js_join(
                    (
                        self._resolve_path(tuple(argument[1:].split(".")), row, {}, False)
                        if argument.startswith(".")
                        else current_value
                        for argument, current_value in zip(group_args, current_groups, strict=True)
                    ),
                    "_",
                )
                == current_key
            ]
        values = [row.get(field) if is_mapping(row) else None for row in rows]
        if aggregate_index:
            context.marker_key = f"{marker.root}:{'.'.join(marker.path)}"
            transformed: list[Any] = []
            prior = marker.formatters[:aggregate_index]
            conditional = any(self.registry.metadata(formatter.name).is_conditional for formatter in prior)
            for row, item in zip(rows, values, strict=True):
                if conditional and is_mapping(row):
                    # Conditions before an aggregator run per element with their own state
                    # (check-agg-condition-reset); relative arguments read the element.
                    element_marker = Marker(marker.root, (field,), prior, marker.raw)
                    transformed.append(self._format(item, element_marker, options, context, row, {}))
                    continue
                for formatter in prior:
                    arguments = tuple(
                        self._resolve_path(tuple(argument[1:].split(".")), row, {}, False)
                        if argument.startswith(".") and is_mapping(row)
                        else formatter_argument(argument, data, context.complement, iterators)
                        for argument in formatter.arguments
                    )
                    item = _apply_formatter(self.registry, formatter.name, item, arguments, options, context)
                transformed.append(item)
            values = transformed
            marker = replace(marker, formatters=marker.formatters[aggregate_index:])
        return values, marker

    def _directive_value(
        self,
        marker: Marker,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        context: MarkerContext,
        iterators: Mapping[str, Any],
    ) -> Any:
        """The common evaluator returns a condition for a directive-only pipeline."""
        session = CURRENT_RENDER.get()
        if session is not None:
            return session.evaluate(marker, iterators, condition_result=True)
        variables = VariableEnvironment(context.data, context.complement, {})
        return self._evaluate(marker, data, options, iterators, variables, context, condition_result=True)

    @staticmethod
    def _resolve_path(
        path: tuple[str, ...], source: Mapping[str, Any] | list[Any], iterators: Mapping[str, Any], has_aggregate: bool
    ) -> Any:
        """Resolve a marker path, including collection projection for aggregates."""
        if not has_aggregate and (fast := _resolve_loop_item(path, iterators)) is not _NO_ITERATOR:
            return fast
        return DocxTextRenderer._walk_path(path, source, iterators, has_aggregate)

    @staticmethod
    def _walk_path(
        path: tuple[str, ...],
        source: Mapping[str, Any] | list[Any],
        iterators: Mapping[str, Any],
        has_aggregate: bool,
        *,
        distinguish_missing: bool = False,
    ) -> Any:
        """Segment-by-segment resolution: parents, projections, filters, indexes and iterators."""
        missing = conditions.JS_UNDEFINED if distinguish_missing else None
        value: Any = source
        ancestors: list[Any] = [source]
        prefix: list[str] = []
        for segment in path:
            if segment == "..":
                if len(ancestors) <= 1:
                    raise MarkerResolutionError(f"Cannot access parent object in {'.'.join(path)!r} (too high)")
                ancestors.pop()
                value = ancestors[-1]
                if prefix:
                    prefix.pop()
                continue
            if "[" in segment and not has_aggregate and not (is_mapping(value) or is_list(value)):
                return missing
            if segment.endswith("[]"):
                field = segment[:-2]
                value = _aggregate_sequence(value, field, path)
                prefix.append(segment)
                ancestors.append(value)
                continue
            aggregate_filter = _AGGREGATE_FILTER_SEGMENT.fullmatch(segment) if has_aggregate else None
            if (
                aggregate_filter is not None
                and _ITERATOR_SEGMENT.fullmatch(segment) is None
                and _INDEXED_SEGMENT.fullmatch(segment) is None
            ):
                value = _aggregate_filtered(value, aggregate_filter, path)
                prefix.append(segment)
                ancestors.append(value)
                continue
            search_filter = None if has_aggregate else _AGGREGATE_FILTER_SEGMENT.fullmatch(segment)
            if (
                search_filter is not None
                and _ITERATOR_SEGMENT.fullmatch(segment) is None
                and _INDEXED_SEGMENT.fullmatch(segment) is None
            ):
                matches = _aggregate_filtered(value, search_filter, path)
                if not matches:
                    return missing
                value = matches[0]
                prefix.append(segment)
                ancestors.append(value)
                continue
            iterator_value = _iterator_value(value, segment, iterators, has_aggregate, path, prefix)
            if iterator_value is not _NO_ITERATOR:
                value = iterator_value
                prefix.append(segment)
                ancestors.append(value)
                continue
            indexed = _INDEXED_SEGMENT.fullmatch(segment)
            if indexed is not None:
                value = _indexed_item(value, indexed, path)
                if value is _NO_ITERATOR:
                    return missing
                prefix.append(segment)
                ancestors.append(value)
                continue
            if segment.startswith("[") or not is_mapping(value):
                if is_list(value):
                    value = [item.get(segment) if is_mapping(item) else None for item in value]
                    prefix.append(segment)
                    ancestors.append(value)
                    continue
                return missing
            value = value.get(segment, missing)
            prefix.append(segment)
            ancestors.append(value)
        return value

    @staticmethod
    def _collect_variables(
        root: etree._Element,
        data: Mapping[str, Any],
        complement: Mapping[str, Any],
        variable_str: str = "",
        initial: Mapping[str, VariableDeclaration] | None = None,
    ) -> VariableEnvironment:
        declarations: dict[str, VariableDeclaration] = {}
        for occurrence in scan(variable_str, include_aliases=True):
            declaration = parse_declaration(occurrence.raw)
            if declaration is not None:
                declarations[declaration.name] = declaration
        declarations.update(initial or {})
        for text in root.iter(*DIALECT.get().texts):
            value = text.text or ""
            for occurrence in reversed(scan(value, include_aliases=True)):
                declaration = parse_declaration(occurrence.raw)
                if declaration is None:
                    continue
                declarations[declaration.name] = declaration
                value = f"{value[: occurrence.start]}{value[occurrence.end :]}"
            text.text = value
        if declarations:
            substitute_variables(root, declarations)
        return VariableEnvironment(data, complement, declarations)

    def _format(
        self,
        value: Any,
        marker: Marker,
        options: RenderOptions,
        context: MarkerContext,
        data: Mapping[str, Any] | list[Any],
        iterators: Mapping[str, Any],
    ) -> str:
        return printable(self._pipeline(value, marker, options, context, data, iterators))

    def _pipeline(
        self,
        value: Any,
        marker: Marker,
        options: RenderOptions,
        context: MarkerContext,
        data: Mapping[str, Any] | list[Any],
        iterators: Mapping[str, Any],
        *,
        condition_result: bool = False,
    ) -> Any:
        if not marker.formatters:
            return value
        context.marker_key = f"{marker.root}:{'.'.join(marker.path)}:{marker.raw}"
        condition: bool | None = None
        use_and = False
        for formatter in marker.formatters:
            if formatter.name in {"and", "or"}:
                use_and = formatter.name == "and"
                if formatter.arguments:
                    value = formatter_argument(formatter.arguments[0], data, context.complement, iterators, marker)
                continue
            if formatter.name in CONDITION_TESTS:
                expected = (
                    formatter_argument(formatter.arguments[0], data, context.complement, iterators, marker)
                    if formatter.arguments
                    else None
                )
                matched = CONDITION_TESTS[formatter.name](value, expected)
                condition = matched if condition is None else condition and matched if use_and else condition or matched
                continue
            if formatter.name in {"ifEmpty", "ifEqual", "ifContain"}:
                arguments = tuple(
                    formatter_argument(argument, data, context.complement, iterators, marker)
                    for argument in formatter.arguments
                )
                matched, message, continuation = _legacy_condition(formatter.name, value, arguments)
                if matched:
                    value = message
                    if continuation is not True and continuation != "true":
                        break
                continue
            if formatter.name in {"show", "elseShow"}:
                matched = (
                    condition is (formatter.name == "show")
                    if condition is not None
                    else conditions.js_truthy(value) == (formatter.name == "show")
                )
                if matched:
                    value = (
                        formatter_argument(formatter.arguments[0], data, context.complement, iterators, marker)
                        if formatter.arguments
                        else None
                    )
                    condition = None
                    break
                continue
            if formatter.name == "hide":
                return ""
            if formatter.name in {"showBegin", "hideBegin"}:
                matched = condition if condition is not None else conditions.js_truthy(value)
                hidden = not matched if formatter.name == "showBegin" else matched
                _begin_block(context, formatter, hidden)
                return ""
            if formatter.name in {"showEnd", "hideEnd"}:
                _end_block(context, formatter)
                return ""
            value = _apply_formatter(
                self.registry,
                formatter.name,
                value,
                tuple(
                    _math_argument(argument, data, context.complement, iterators, marker)
                    if self.registry.metadata(formatter.name).accepts_math
                    and index == 0
                    and is_math_candidate(argument)
                    else formatter_argument(argument, data, context.complement, iterators, marker)
                    for index, argument in enumerate(formatter.arguments)
                ),
                options,
                context,
            )
        return condition if condition_result and condition is not None else value


def _begin_block(context: MarkerContext, call: FormatterCall, hidden: bool) -> None:
    span = _span_identifier(call)
    if span is not None and span in context.conditional_spans:
        # A span crossing a loop end starts outside the expanded copies in JS.
        # Its first condition governs the whole span; later copies cannot change it.
        return
    if span is not None:
        context.conditional_spans[span] = len(context.conditional_blocks)
    context.conditional_blocks.append(hidden)


def _end_block(context: MarkerContext, call: FormatterCall) -> None:
    span = _span_identifier(call)
    if span is not None and span not in context.conditional_spans:
        # An empty loop removes its generated begin but can leave the end outside.
        # That end must not close an unrelated surrounding user block.
        return
    if not context.conditional_blocks:
        raise MarkerResolutionError(f"Missing conditional block start for {call.name}")
    if span is not None and span in context.conditional_spans:
        index = context.conditional_spans.pop(span)
        context.conditional_blocks.pop(index)
        context.conditional_spans = {
            key: position - (position > index) for key, position in context.conditional_spans.items()
        }
    else:
        context.conditional_blocks.pop()


def _span_identifier(call: FormatterCall) -> str | None:
    if call.arguments and re.fullmatch(r"'ct-span-[0-9a-f]{32}'", call.arguments[0]):
        return call.arguments[0]
    return None


def _validate_marker_syntax(raw: str) -> None:
    """Compile-time checks of lib/extracter.js: brackets inside a filter, ``..`` above the root."""
    depth = 0
    for char in raw:
        if char == "[":
            if depth:
                raise MarkerResolutionError(
                    f"Carbotek does not accept square brackets inside array filters in {printed(raw)}. "
                    "Please contact the support to add this feature."
                )
            depth += 1
        elif char == "]":
            depth = max(depth - 1, 0)


def _validate_conditional_blocks(marker: Marker, blocks: list[str], registry: FormatterRegistry) -> None:
    """Pair showBegin/hideBegin with showEnd/hideEnd in document order, like the JS extracter."""
    names = [formatter.name for formatter in marker.formatters]
    if marker.root in {"d", "c"}:
        levels = 0
        for segment in marker.path:
            if segment == "..":
                if levels == 0:
                    body = printed(marker.raw)[1:-1].split(":", 1)[0].strip()
                    raise MarkerResolutionError(f'Cannot access parent object in "_root.{body}" (too high)')
                levels -= 1
            else:
                levels += 1
    if any(name in {"showBegin", "hideBegin"} for name in names):
        blocks.append(printed(marker.raw))
    elif any(name in {"showEnd", "hideEnd"} for name in names):
        if not blocks:
            raise MarkerResolutionError(f"Missing showBegin or hideBegin of this tag {printed(marker.raw)}")
        blocks.pop()
    _validate_conditions_around_aggregator(marker, names, registry)


def _validate_conditions_around_aggregator(marker: Marker, names: list[str], registry: FormatterRegistry) -> None:
    """builder.js: a closed condition on both sides of an aggregator in a loop is rejected."""
    if not any("[" in segment for segment in marker.path):
        return
    split = next((index for index, name in enumerate(names) if name.startswith(("agg", "cum"))), None)
    if split is None:
        return

    def closes(side: list[str]) -> bool:
        return any(registry.metadata(name).updates_propagation for name in side)

    if closes(names[:split]) and closes(names[split + 1 :]):
        raise MarkerResolutionError(
            "Conditional formatter cannot be used before aggregators and after in the same time "
            f'Source: "{printed(marker.raw)}"'
        )


_COLOR_FORMATTERS = frozenset({"color", "colorToDocx", "getAndConvertColorDocx", "shadeColor"})


def _is_bind_color(raw: str) -> bool:
    return raw.lstrip("{ \t").startswith("bindColor")


def _can_promote_crossing_value(marker: Marker, registry: FormatterRegistry) -> bool:
    """Only ordinary value pipelines can move outside the once-only XML wrapper."""
    if _is_deferred_marker(marker, attribute=False):
        return False
    return all(
        call.name not in _BLOCK_FORMATTERS
        and not registry.metadata(call.name).uses_marker_context
        and not registry.metadata(call.name).mutates_input
        for call in marker.formatters
    )


def _is_deferred_marker(marker: Marker, *, attribute: bool) -> bool:
    """Markers owned by a feature that runs after the loop expansion: kept, only frozen."""
    names = [formatter.name for formatter in marker.formatters]
    if attribute:
        # generateImageXlsxReference: an XLSX picture, resolved by carbotek.xlsx.images after rendering.
        return any(name in _COLOR_FORMATTERS or name == "generateImageXlsxReference" for name in names)
    # Same rule as features.html._is_html_marker (lib/html.js): calls after :html must not have
    # parentheses, otherwise the marker is an ordinary text marker.
    html = "html" in names and _HTML_MARKER.fullmatch(marker.raw) is not None
    return html or (bool(names) and names[-1] == "color")


_HTML_MARKER = re.compile(r"\{[^{}]*?:html\(?[^{()}]*?\)?[^{<()>}]*?\}")


_LOOP_SEGMENT = LOOP_SEGMENT


def freeze_marker(raw: str, iterators: Mapping[str, Any]) -> str:
    """Rewrite the loop iterators of a marker to the absolute positions of the current copy.

    ``{d.rows[i].img:imageFit(fill)}`` in the second copy becomes ``{d.rows[1].img:imageFit(fill)}``.
    Features that run after the loop expansion (images, links, HTML, colors) then resolve the
    marker without iterator scope. Markers outside a loop are returned unchanged.
    """
    marker = parse(raw)
    session = CURRENT_RENDER.get()
    if session is not None and session.register_features:
        return session.defer(marker, iterators)
    if marker.root not in {"d", "c"} or not iterators:
        return raw
    prefix: list[str] = []
    segments: list[str] = []
    changed = False
    for segment in marker.path:
        loop = _LOOP_SEGMENT.fullmatch(segment)
        if loop is not None:
            key = ".".join((*prefix, loop.group("field")))
            if key + INDEX_SUFFIX in iterators:
                segments.append(f"{loop.group('field')}[{iterators[key + INDEX_SUFFIX]}]")
                prefix.append(segment)
                changed = True
                continue
        segments.append(segment)
        prefix.append(segment)
    if not changed:
        return raw
    head = marker.root
    for segment in segments:
        head += "." if segment == ".." else segment if segment.startswith("[") else f".{segment}"
    body = printed(raw)[1:-1]
    formatters = body[_path_end(body) :]
    return "{" + head + formatters + "}"


def _path_end(body: str) -> int:
    """Index of the first top-level ':' of a cleaned marker body (end of the path)."""
    depth = 0
    quote: str | None = None
    for index, character in enumerate(body):
        if quote is not None:
            quote = None if character == quote else quote
        elif character in {"'", '"'}:
            quote = character
        elif character in "([":
            depth += 1
        elif character in ")]":
            depth -= 1
        elif character == ":" and depth == 0:
            return index
    return len(body)


def _missing_plain_path(path: tuple[str, ...], source: Mapping[str, Any] | list[Any]) -> bool:
    value: Any = source
    for name in path:
        if not _PLAIN_FIELD.fullmatch(name) or name == "..":
            return False
        if not is_mapping(value):
            return True
        if name not in value:
            return True
        value = value[name]
    return False


def _missing_marker_path(
    path: tuple[str, ...], source: Mapping[str, Any] | list[Any], iterators: Mapping[str, Any]
) -> bool:
    plan = _loop_item_plan(path)
    if plan is not None and plan[0] in iterators:
        return _missing_plain_path(plan[1], iterators[plan[0]])
    if all(_PLAIN_FIELD.fullmatch(segment) and segment != ".." for segment in path):
        return _missing_plain_path(path, source)
    return (
        DocxTextRenderer._walk_path(path, source, iterators, False, distinguish_missing=True) is conditions.JS_UNDEFINED
    )


def resolve_path(path: tuple[str, ...], source: Mapping[str, Any] | list[Any]) -> Any:
    """Resolve a (possibly frozen, ``field[N]``) marker path without loop scope; ``None`` if absent."""
    try:
        return DocxTextRenderer._resolve_path(path, source, {}, False)
    except MarkerResolutionError:
        return None


_BLOCK_FORMATTERS = frozenset({"showBegin", "hideBegin", "showEnd", "hideEnd"})


@dataclass(frozen=True, slots=True)
class _MarkerPlan:
    """What a marker text means for the text pass, decided once per distinct marker."""

    marker: Marker | None
    skip: bool
    deferred: bool


@lru_cache(maxsize=2048)
def _marker_plan(raw: str) -> _MarkerPlan:
    if _is_bind_color(raw) or is_directive(raw):
        return _MarkerPlan(None, True, False)
    marker = parse(raw)
    return _MarkerPlan(marker, False, _is_deferred_marker(marker, attribute=False))


@lru_cache(maxsize=2048)
def _aggregate_shape(marker: Marker) -> tuple[int | None, bool]:
    """Index of the first accumulating aggregator and whether any aggregator is used."""
    aggregate_index = next(
        (
            index
            for index, formatter in enumerate(marker.formatters)
            if formatter.name.startswith("agg") and "Get" not in formatter.name
        ),
        None,
    )
    return aggregate_index, aggregate_index is not None or any(
        formatter.name.startswith("agg") for formatter in marker.formatters
    )


# A compiled chain step: (kind, *operands), see _compiled_step.
type _CompiledStep = tuple[int, Any, Any, Any, Any]
type CompiledMarker = Callable[[Mapping[str, Any], RenderOptions, MarkerContext], Any]

_FORMATTER, _FORMATTER_OPTIONS, _FORMATTER_CONTEXT, _FORMATTER_BOTH, _CONDITION, _SHOW, _COMBINE, _HIDE = range(8)
# Formatter names _format handles itself that compiled chains do not cover.
_CHAIN_FORMATTERS = frozenset(
    {"ifEmpty", "ifEqual", "ifContain", "escapeForXml"}
    | {"showBegin", "hideBegin", "showEnd", "hideEnd", "cumSum", "cumAvg", "cumCount", "cumCountD"}
)
_NOT_LITERAL = object()


def _native_plain_field(
    marker: Marker,
    plan: tuple[str, tuple[str, ...]],
    piece: Callable[[Mapping[str, Any]], str],
    registry: FormatterRegistry,
    options: RenderOptions,
) -> TextPiece:
    """The marker as an operation of the native section when the Rust code reproduces its chain.

    The operation is read from the steps ``_compiled_marker`` runs, so literal arguments arrive
    decoded exactly as the Python chain uses them (quotes removed, doubled quotes collapsed).
    """
    key, field = plan[0], plan[1][0]
    steps = [_compiled_step(registry, formatter) for formatter in marker.formatters]
    if not steps:
        return PlainField(key, field, piece)
    if len(steps) == 1 and (step := steps[0]) is not None and step[0] <= _FORMATTER_BOTH:
        function, arguments = step[1], step[2]
        if function is format_date and arguments == ("DD.MM.YYYY",):
            return PlainField(key, field, piece, OP_ISO_DATE)
        # The precision spellings JS reads as "2"; high precision skips round() (not native).
        if (
            function is format_number
            and marker.formatters[0].arguments in {("2",), ("'2'",)}
            and options.lang == "ru"
            and not options.use_high_precision_arithmetic
        ):
            return PlainField(key, field, piece, OP_RU_NUMBER)
        if function is digit_currency and not arguments:
            return PlainField(key, field, piece, OP_CENTS)
    if len(steps) == 3:
        condition, shown, otherwise = steps
        if (
            condition is not None
            and shown is not None
            and otherwise is not None
            and condition[:2] == (_CONDITION, conditions.if_equal)
            and shown[:2] == (_SHOW, True)
            and otherwise[:2] == (_SHOW, False)
            and all(type(step[2]) is str for step in (condition, shown, otherwise))
        ):
            return PlainField(key, field, piece, OP_IF_EQUAL, condition[2], shown[2], otherwise[2])
    return piece


@lru_cache(maxsize=2048)
def _compiled_marker(registry: FormatterRegistry, marker: Marker) -> CompiledMarker | None:
    """A loop-copy marker compiled once into a direct function, like the JS builder does.

    Covers the common table cell ``{d.rows[i].a.b:f1(lit):ifEQ(x):show(y):elseShow(z)}``: a loop
    item path with plain fields, ordinary formatters and condition chains whose arguments are
    literals. Everything else (aggregates, blocks, path arguments, math) returns None and keeps the
    general ``_resolve``. The function returns ``_NO_ITERATOR`` when the item is not reachable by
    the plan (the general path decides).
    """
    if marker.root != "d" or _aggregate_shape(marker)[1]:
        return None
    plan = _loop_item_plan(marker.path)
    if plan is None:
        return None
    key, fields = plan
    steps = tuple(_compiled_step(registry, formatter) for formatter in marker.formatters)
    if None in steps:
        return None
    return _compiled_run(
        key, fields, f"{marker.root}:{'.'.join(marker.path)}:{marker.raw}", cast(tuple[_CompiledStep, ...], steps)
    )


def _literal_argument(argument: str) -> Any:
    """A formatter argument that does not depend on the data, or ``_NOT_LITERAL``."""
    literal = _argument(argument)
    if literal is argument and isinstance(literal, str) and argument.startswith(("d.", "c.", ".")):
        return _NOT_LITERAL  # a path argument: resolved per item by the general path
    return literal


def _compiled_step(registry: FormatterRegistry, formatter: FormatterCall) -> _CompiledStep | None:
    """One step of ``_format``'s chain, with its literal arguments decided once."""
    name = formatter.name
    literals = tuple(_literal_argument(argument) for argument in formatter.arguments)
    if _NOT_LITERAL in literals:
        return None
    if name in {"and", "or"}:
        return None if literals else (_COMBINE, name == "and", None, None, None)
    if name in CONDITION_TESTS:
        return _CONDITION, CONDITION_TESTS[name], literals[0] if literals else None, None, None
    if name in {"show", "elseShow"}:
        return _SHOW, name == "show", literals[0] if literals else None, None, None
    if name == "hide":
        return _HIDE, None, None, None, None
    registered = registry.registration(name)
    if registered is None or name in _CHAIN_FORMATTERS or name.startswith("agg"):
        return None
    metadata = registered.metadata
    if metadata.is_conditional or metadata.updates_propagation or metadata.can_inject_xml:
        return None
    if metadata.accepts_math and formatter.arguments and is_math_candidate(formatter.arguments[0]):
        return None
    arguments = registry.prepare_arguments(name, literals)
    kind = _FORMATTER + int(metadata.uses_render_options) + 2 * int(metadata.uses_marker_context)
    return kind, registered.function, arguments, None, None


def _compiled_run(
    key: str, fields: tuple[str, ...], marker_key: str, frozen: tuple[_CompiledStep, ...]
) -> CompiledMarker:
    distinguish_missing = any(
        kind == _CONDITION and function in {conditions.if_equal, conditions.if_not_equal}
        for kind, function, _, _, _ in frozen
    )
    if not frozen and len(fields) == 1:
        field = fields[0]

        def plain(iterators: Mapping[str, Any], _options: RenderOptions, _context: MarkerContext) -> Any:
            item = iterators.get(key, _NO_ITERATOR)
            if is_mapping(item):
                return printable(item.get(field))
            return "" if item is None else _NO_ITERATOR

        return plain

    def run(iterators: Mapping[str, Any], options: RenderOptions, context: MarkerContext) -> Any:
        item: Any = iterators.get(key, _NO_ITERATOR)
        if item is _NO_ITERATOR:
            return _NO_ITERATOR
        for name in fields:
            if is_mapping(item):
                item = item.get(name, conditions.JS_UNDEFINED if distinguish_missing else None)
            elif item is None or item is conditions.JS_UNDEFINED:
                item = conditions.JS_UNDEFINED if distinguish_missing else None
                break  # JS optional chaining
            else:
                return _NO_ITERATOR
        if frozen:
            context.marker_key = marker_key
            return printable(_run_steps(frozen, item, options, context))
        return printable(item)

    return run


def _run_steps(steps: tuple[_CompiledStep, ...], value: Any, options: RenderOptions, context: MarkerContext) -> Any:
    """``_format``'s chain semantics over compiled steps."""
    condition: bool | None = None
    use_and = False
    for kind, first, second, _, _ in steps:
        if kind <= _FORMATTER_BOTH:
            try:
                if kind == _FORMATTER:
                    value = first(value, *second)
                elif kind == _FORMATTER_OPTIONS:
                    value = first(value, *second, render_options=options)
                elif kind == _FORMATTER_CONTEXT:
                    value = first(value, *second, marker_context=context)
                else:
                    value = first(value, *second, render_options=options, marker_context=context)
            except CarbotekError:
                raise
            except (ValueError, TypeError, ArithmeticError, LookupError) as error:
                raise FormatterError(str(error)) from error
        elif kind == _CONDITION:
            matched = first(value, second)
            condition = matched if condition is None else condition and matched if use_and else condition or matched
        elif kind == _SHOW:
            shown = condition is first if condition is not None else conditions.js_truthy(value) == first
            if shown:
                return second
        elif kind == _COMBINE:
            use_and = first
        else:
            return ""
    return value


def _visible_text(pieces: list[TextPiece], context: MarkerContext) -> TextPiece:
    literals = [piece if isinstance(piece, str) else "" for piece in pieces]
    slots = [
        (index, piece.fallback if isinstance(piece, PlainField) else piece)
        for index, piece in enumerate(pieces)
        if not isinstance(piece, str)
    ]

    def visible(iterators: Mapping[str, Any]) -> str:
        # Evaluate ordinary fields even while hidden, preserving formatter side effects.
        parts = literals.copy()
        for index, evaluate in slots:
            parts[index] = evaluate(iterators)
        return "" if context.is_hidden else "".join(parts)

    return visible


def printable(value: Any) -> str:
    if value is None or value is conditions.JS_UNDEFINED:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    result = sanitize_xml_text(
        js_to_string(value)
        if isinstance(value, (dict, list))
        else number_string(value)
        if isinstance(value, float | Decimal)
        else str(value)
    )

    if "{" not in result:
        return result
    session = CURRENT_RENDER.get()
    return session.protect(result) if session is not None else result


def _apply_formatter(
    registry: FormatterRegistry,
    name: str,
    value: Any,
    arguments: tuple[Any, ...],
    options: RenderOptions,
    context: MarkerContext,
) -> Any:
    if name == "escapeForXml":
        return strip_xml_controls(value) if isinstance(value, str) else value  # lxml escapes the Word XML text.
    if name == "cumCount" and is_list(value):
        value = 1  # JS cumCount counts calls; an array value is one call, not its length.
    name = {"cumSum": "aggSum", "cumAvg": "aggAvg", "cumCount": "aggCount", "cumCountD": "aggCountD"}.get(name, name)
    if name.startswith("agg"):
        marker_key = context.marker_key
        context.marker_key = f"{marker_key}:{name.removesuffix('GetReverse').removesuffix('Get')}"
        try:
            return registry.apply(name, value, arguments, render_options=options, marker_context=context)
        finally:
            context.marker_key = marker_key
    return registry.apply(name, value, arguments, render_options=options, marker_context=context)


def _is_block_end(marker: Marker) -> bool:
    return any(formatter.name in {"showEnd", "hideEnd"} for formatter in marker.formatters)


_NO_ITERATOR = object()


def _indexed_item(value: Any, indexed: re.Match[str], path: tuple[str, ...]) -> Any:
    """``field[N]`` (or ``[N]`` on a root array); ``_NO_ITERATOR`` when the item does not exist."""
    if indexed.group("field") == "" and is_list(value):
        sequence: Any = value
    elif not is_mapping(value):
        raise MarkerResolutionError(f"Unsupported array path: {'.'.join(path)}")
    else:
        sequence = value.get(indexed.group("field"))
    index = int(indexed.group("index"))
    if not is_list(sequence) or index >= len(sequence):
        return _NO_ITERATOR
    return sequence[index]


_ARRAY_SEGMENT = re.compile(r"(?P<field>[^[]*)\[(?P<inside>[^\]]*)]")


def _in_rendered_loop(path: tuple[str, ...], iterators: Mapping[str, Any]) -> bool:
    """Whether the innermost array of an aggregate path is walked by a loop being rendered.

    JS then adds the items in that loop's order; otherwise (another array, or a filter such as
    ``rows[v>0]`` that gets a loop of its own) its builder loop runs from the last item.
    """
    prefix: list[str] = []
    inside = False
    for segment in path:
        match = _ARRAY_SEGMENT.fullmatch(segment)
        if match is not None:
            shares_loop = not match.group("inside").strip() or LOOP_SEGMENT.fullmatch(segment) is not None
            inside = shares_loop and ".".join((*prefix, match.group("field"))) in iterators
            prefix.append(match.group("field"))
        else:
            prefix.append(segment)
    return inside


def _aggregate_sequence(value: Any, field: str, path: tuple[str, ...]) -> list[Any]:
    if not field and is_list(value):
        return value
    if is_list(value):
        return [
            member for item in value if is_mapping(item) and isinstance(item.get(field), list) for member in item[field]
        ]
    if is_mapping(value):
        sequence = value.get(field)
        return sequence if is_list(sequence) else []
    raise MarkerResolutionError(f"Unsupported aggregate path: {'.'.join(path)}")


def _aggregate_filtered(value: Any, match: re.Match[str], path: tuple[str, ...]) -> list[Any]:
    if not (is_mapping(value) or is_list(value)):
        raise MarkerResolutionError(f"Unsupported aggregate filter path: {'.'.join(path)}")
    if is_mapping(value):
        sequence = value.get(match.group("field"))
        values = sequence if is_list(sequence) else []
    else:
        values = [
            member
            for item in value
            if is_mapping(item) and is_list(item.get(match.group("field")))
            for member in item[match.group("field")]
        ]
    filters = tuple(part for part in match.group("filters").split(",") if part)
    return _filter_values(values, filters)


_PLAIN_FIELD = re.compile(r"[^\[\].]+")


@lru_cache(maxsize=2048)
def _loop_item_plan(path: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
    """``(iterator key, fields)`` when the path is ``…loop[i].a.b`` with plain fields after its last loop."""
    prefix: list[str] = []
    plan: tuple[str, tuple[str, ...]] | None = None
    for index, segment in enumerate(path):
        if segment == "..":
            return None
        match = LOOP_SEGMENT.fullmatch(segment)
        if match is not None and match.group("suffix") is None:
            rest = path[index + 1 :]
            if all(_PLAIN_FIELD.fullmatch(field) and field != ".." for field in rest):
                plan = (".".join((*prefix, match.group("field"))), rest)
        prefix.append(segment)
    return plan


def _resolve_loop_item(path: tuple[str, ...], iterators: Mapping[str, Any]) -> Any:
    """Fast path planned once per path: a loop item from the iterators, then plain fields.

    Returns ``_NO_ITERATOR`` when the full walk of ``_resolve_path`` is needed.
    """
    plan = _loop_item_plan(path)
    if plan is None or plan[0] not in iterators:
        return _NO_ITERATOR
    item: Any = iterators[plan[0]]
    for name in plan[1]:
        if is_mapping(item):
            item = item.get(name)
        elif item is None:
            return None  # JS optional chaining
        else:
            return _NO_ITERATOR  # list projection or an error: let the full walk decide
    return item


def _iterator_value(
    value: Any,
    segment: str,
    iterators: Mapping[str, Any],
    has_aggregate: bool,
    path: tuple[str, ...],
    prefix: list[str],
) -> Any:
    match = _ITERATOR_SEGMENT.fullmatch(segment)
    if match is None:
        return _NO_ITERATOR
    field = match.group("field")
    key = ".".join((*prefix, field))
    # Aggregate over a loop segment outside its loop: the whole array,
    # still restricted by the segment's filters ({d.rows[i, qty>0].total:aggSum}).
    filters = tuple(
        part.strip()
        for part in (match.group("filters") or "").split(",")
        if part.strip() and not part.strip().startswith("*")
    )
    if field == "" and is_list(value):
        if key in iterators:
            return iterators[key]
        if has_aggregate:
            return _filter_values(value, filters)
    if not is_mapping(value) or field not in value:
        raise MarkerResolutionError(f"Unsupported loop path: {'.'.join(path)}")
    if key not in iterators:
        if has_aggregate and is_list(value[field]):
            return _filter_values(value[field], filters)
        raise MarkerResolutionError(f"Unsupported loop path: {'.'.join(path)}")
    return iterators[key]


def _argument(value: str) -> Any:
    """A literal formatter argument as JS passes it: always a string.

    Single quotes delimit a string and a doubled quote inside is one quote (``'l''arbre'``);
    double quotes are ordinary characters, and ``007`` or ``+5`` keep their spelling.
    """
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def substitute_variables(
    root: etree._Element, declarations: Mapping[str, VariableDeclaration], *, all_text: bool = False
) -> None:
    """Rewrite ``$variable`` markers into their declared code before any compilation (JS macros)."""

    def rewrite(value: str) -> str:
        if "$" not in value:
            return value
        for occurrence in reversed(scan(value)):
            body = substitute(occurrence.raw[1:-1], declarations)
            # The text was a marker before the substitution: without a data root it prints nothing.
            marker = f"{{{body}}}" if re.match(r"\s*(?:[dc](?:[.\[:]|\s*$)|\$|t\()", body) else ""
            value = f"{value[: occurrence.start]}{marker}{value[occurrence.end :]}"
        return value

    texts = DIALECT.get().texts
    for node in root.iter():
        if (all_text or node.tag in texts) and node.text:
            node.text = rewrite(node.text)
        for name, attribute in node.attrib.items():
            if "{" in attribute:
                node.set(name, rewrite(attribute))


# Rendered outermost loop copies, left alone by the pass over the whole part.
SKIPPED_COPIES: StateSlot[set[etree._Element] | frozenset[etree._Element]] = StateSlot(
    "SKIPPED_COPIES", default=frozenset()
)
_NO_COPIES: set[etree._Element] = set()


def _marker_text_nodes(
    element: etree._Element, iterators: Mapping[str, Any], rendered: set[etree._Element]
) -> Iterable[etree._Element]:
    """Text nodes holding a ``{`` that this pass renders."""
    dialect = DIALECT.get()
    if iterators:
        # A loop copy is small: a filtered C iteration is cheaper than setting up an XPath.
        return (text for text in element.iter(*dialect.texts) if (content := text.text) and "{" in content)
    return marker_nodes_outside(element, rendered)[0] if rendered else dialect.marker_texts(element)


def _attribute_marker_nodes(
    element: etree._Element, features: CopyFeatures | None, rendered: set[etree._Element]
) -> Iterable[etree._Element]:
    if features is not None and not features.attribute_markers:
        return ()
    return marker_nodes_outside(element, rendered)[1] if rendered else _with_marker_attributes(element)


def _texts_and_copies(
    element: etree._Element, rendered: set[etree._Element], texts: tuple[str, ...]
) -> Iterator[etree._Element]:
    """Text nodes in document order, yielding a rendered copy as one element instead of its texts.

    Only the template part outside the copies is walked in Python; copies are skipped whole.
    """
    for child in element:
        if child in rendered or (child.tag in texts and not len(child)):
            yield child
        elif len(child):
            yield from _texts_and_copies(child, rendered, texts)


def _inject_line_breaks(root: etree._Element) -> None:
    """Replace the convCRLF marker of the rendered text with ``<w:br/>`` in the same run."""
    for text in list(root.iter(qn("w:t"))):
        content = text.text or ""
        if LINE_BREAK not in content:
            continue
        pieces = content.split(LINE_BREAK)
        text.text = pieces[0]
        anchor = text
        for piece in pieces[1:]:
            line_break = etree.Element(qn("w:br"))
            anchor.addnext(line_break)
            following = etree.Element(qn("w:t"))
            following.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            following.text = piece
            line_break.addnext(following)
            anchor = following


def _plain_line_breaks(root: etree._Element) -> None:
    """SpreadsheetML has no ``w:br``: convCRLF leaves the line break in the text, as JS does."""
    for text in root.iter(*SHEET.texts):
        if text.text and LINE_BREAK in text.text:
            text.text = text.text.replace(LINE_BREAK, "\n")


_ATTRIBUTE_MARKER = re.compile(rb'="[^"]*\{')
_MARKER_ATTRIBUTE_ELEMENTS = etree.XPath("descendant-or-self::*[@*[contains(., '{')]]")


def _with_marker_attributes(element: etree._Element) -> list[etree._Element]:
    """Elements of the subtree with a ``{`` in at least one attribute value."""
    return cast(list[etree._Element], _MARKER_ATTRIBUTE_ELEMENTS(element))


def _rooted(
    root: str, path: tuple[str, ...], source: Mapping[str, Any] | list[Any]
) -> tuple[tuple[str, ...], Mapping[str, Any] | list[Any]]:
    """Complement paths live under ``COMPLEMENT_ROOT`` so their loop iterators never meet ``d``'s."""
    if root == "c":
        return (COMPLEMENT_ROOT, *path), {COMPLEMENT_ROOT: source}
    return path, source


def _math_argument(
    value: str,
    data: Mapping[str, Any] | list[Any],
    complement: Mapping[str, Any],
    iterators: Mapping[str, Any],
    marker: Marker | None,
) -> Any:
    """Arithmetic argument of add/sub/mul/div, evaluated like the JS generated code."""
    if _argument(value) is not value:
        return _argument(value)
    return math_evaluate(
        value, lambda operand: js_parse_float(formatter_argument(operand, data, complement, iterators, marker))
    )


def formatter_argument(
    value: str,
    data: Mapping[str, Any] | list[Any],
    complement: Mapping[str, Any],
    iterators: Mapping[str, Any],
    marker: Marker | None = None,
) -> Any:
    """Resolve Carbotek path arguments while preserving quoted literals."""
    literal = _argument(value)
    if literal is not value or not isinstance(literal, str):
        return literal
    if value.startswith("d."):
        return DocxTextRenderer._resolve_path(tuple(value[2:].split(".")), data, iterators, False)
    if value.startswith("c."):
        return DocxTextRenderer._resolve_path(*_rooted("c", tuple(value[2:].split(".")), complement), iterators, False)
    if value.startswith(".") and marker is not None and marker.root in {"d", "c"}:
        parent_steps = len(value) - len(value.lstrip(".")) - 1
        parent_path = marker.path[:-1]
        if parent_steps > len(parent_path):
            return None
        path = (*parent_path[: len(parent_path) - parent_steps], *value.lstrip(".").split("."))
        source = data if marker.root == "d" else complement
        return DocxTextRenderer._resolve_path(*_rooted(marker.root, path, source), iterators, False)
    if value.startswith(".") and iterators:
        current = list(iterators.values())[-1]
        if is_mapping(current) or is_list(current):
            return DocxTextRenderer._resolve_path(tuple(value[1:].split(".")), current, {}, False)
    return literal


CONDITION_TESTS: dict[str, Callable[[Any, Any], bool]] = {
    "ifEQ": conditions.if_equal,
    "ifNE": conditions.if_not_equal,
    "ifGT": conditions.if_greater,
    "ifGTE": conditions.if_greater_equal,
    "ifLT": conditions.if_less,
    "ifLTE": conditions.if_less_equal,
    "ifIN": conditions.if_in,
    "ifNIN": conditions.if_not_in,
    "ifTE": conditions.if_type_equal,
    "ifEM": lambda value, _expected: bool(conditions.if_empty(value)),
    "ifNEM": lambda value, _expected: bool(conditions.if_not_empty(value)),
}


def _legacy_condition(name: str, value: Any, arguments: tuple[Any, ...]) -> tuple[bool, Any, Any]:
    if name == "ifEmpty":
        matched = conditions.if_empty(value)
        message = arguments[0] if arguments else None
        continuation = arguments[1] if len(arguments) > 1 else None
    else:
        expected = arguments[0] if arguments else None
        matched = conditions.if_equal(value, expected) if name == "ifEqual" else conditions.if_contain(value, expected)
        message = arguments[1] if len(arguments) > 1 else None
        continuation = arguments[2] if len(arguments) > 2 else None
    return matched, message, continuation


# Structural endpoints can lie in different loop copies or outside the loop.
PART_HIDDEN_BLOCKS: StateSlot[HiddenBlocks | None] = StateSlot("PART_HIDDEN_BLOCKS", default=None)
