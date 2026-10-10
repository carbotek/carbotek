# pyright: strict
"""One render's data, evaluator and opaque references to template-owned markers."""

import re
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import Context
from dataclasses import dataclass, field
from functools import wraps
from typing import Any
from uuid import uuid4

from carbotek.formatters import MarkerContext
from carbotek.markers import Marker, MarkerOccurrence, VariableEnvironment, parse, scan
from carbotek.options import RenderOptions
from carbotek.render_state import StateSlot

type Evaluator = Callable[[Marker, Mapping[str, Any], bool], Any]


@dataclass(slots=True)
class RenderSession:
    """References are valid only within the render which registered them."""

    data: Mapping[str, Any] | list[Any]
    options: RenderOptions
    context: MarkerContext
    variables: VariableEnvironment
    evaluator: Evaluator
    nonce: str = field(default_factory=lambda: uuid4().hex)
    deferred: dict[str, tuple[Marker, Mapping[str, Any]]] = field(
        default_factory=dict[str, tuple[Marker, Mapping[str, Any]]]
    )
    rendered_texts: set[Any] = field(default_factory=set[Any])
    rendered_attributes: set[tuple[Any, str]] = field(default_factory=set[tuple[Any, str]])
    iterators: Mapping[str, Any] = field(default_factory=dict[str, Any])
    evaluated: dict[tuple[str, tuple[Any, ...], bool], Any] = field(
        default_factory=dict[tuple[str, tuple[Any, ...], bool], Any]
    )
    assets: dict[tuple[Any, ...], tuple[bytes, str]] = field(default_factory=dict[tuple[Any, ...], tuple[bytes, str]])
    features: set[str] = field(default_factory=set[str])
    part_plan: Any = None
    register_features: bool = True
    preapplied_sets: bool = False
    protected_literals: bool = False

    def defer(self, marker: Marker, iterators: Mapping[str, Any]) -> str:
        names = ":".join(call.name for call in marker.formatters)
        token = f"[[ct-{self.nonce}-{len(self.deferred)}:{names}]]"
        self.deferred[token] = marker, dict(iterators)
        return token

    def evaluate(
        self, marker: Marker, iterators: Mapping[str, Any] | None = None, *, condition_result: bool = False
    ) -> Any:
        reference = self.deferred.get(marker.raw)
        key = (marker.raw, marker.formatters, condition_result)
        if reference is not None:
            if key in self.evaluated:
                return self.evaluated[key]
            original, scope = reference
            marker = (
                original
                if marker.formatters == original.formatters
                else Marker(original.root, original.path, marker.formatters, original.raw)
            )
            iterators = scope
        value = self.evaluator(marker, iterators or {}, condition_result)
        if reference is not None:
            self.evaluated[key] = value
        return value

    def protect(self, value: str) -> str:
        """Literal values cannot become template syntax on a later pass."""
        if "{" not in value:
            return value
        self.protected_literals = True
        return value.replace("{", f"[[cl-{self.nonce}]]")

    def restore(self, content: bytes) -> bytes:
        return content.replace(f"[[cl-{self.nonce}]]".encode(), b"{")


CURRENT_RENDER: StateSlot[RenderSession | None] = StateSlot("CURRENT_RENDER", default=None)
_TOKEN = re.compile(r"\[\[ct-[0-9a-f]{32}-\d+:[^\[\]]*\]\]")


@contextmanager
def render_session(session: RenderSession) -> Generator[None]:
    token = CURRENT_RENDER.set(session)
    try:
        yield
    finally:
        CURRENT_RENDER.reset(token)


def feature_scan(text: str) -> tuple[MarkerOccurrence, ...]:
    """Postphases consume registered references, never marker-looking data."""
    session = CURRENT_RENDER.get()
    if session is None:
        return scan(text)
    return tuple(
        MarkerOccurrence(match[0], match.start(), match.end())
        for match in _TOKEN.finditer(text)
        if match[0] in session.deferred
    )


def feature_parse(raw: str) -> Marker:
    session = CURRENT_RENDER.get()
    if session is not None and raw in session.deferred:
        original = session.deferred[raw][0]
        return Marker(original.root, original.path, original.formatters, raw)
    return parse(raw)


def feature_source(marker: Marker) -> str:
    session = CURRENT_RENDER.get()
    if session is not None and marker.raw in session.deferred:
        return session.deferred[marker.raw][0].raw
    return marker.raw


def isolated_render[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Public executions start with fresh renderer and fragment context variables."""

    @wraps(function)
    def execute(*args: P.args, **kwargs: P.kwargs) -> R:
        return Context().run(function, *args, **kwargs)

    return execute
