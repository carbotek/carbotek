# pyright: strict, reportPrivateUsage=false
"""Variable declarations and a deliberately small, safe expression interpreter."""

import ast
import operator
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from carbotek.errors import MarkerError
from carbotek.typing_helpers import is_mapping

_DECLARATION = re.compile(r"^\{\s*#\s*([A-Za-z_]\w*)\s*(?:\(([^)]*)\))?\s*=\s*(.*?)\s*\}$", re.DOTALL)
_OPERATORS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: cast(Any, operator.truediv),  # pyright: ignore[reportUnknownMemberType]
    ast.Mod: operator.mod,
    ast.Pow: cast(Any, operator.pow),  # pyright: ignore[reportUnknownMemberType]
}


class VariableExpressionError(MarkerError, ValueError):
    """A declaration contains syntax outside the intentionally supported expression subset."""


@dataclass(frozen=True, slots=True)
class VariableDeclaration:
    """Named, optionally parameterized expression declared inside a template."""

    name: str
    parameters: tuple[str, ...]
    expression: str


def parse_declaration(raw: str) -> VariableDeclaration | None:
    """Parse a `{#name = expression}` declaration, returning ``None`` for ordinary text."""
    match = _DECLARATION.fullmatch(raw)
    if match is None:
        return None
    parameter_text = match.group(2)
    parameters = () if parameter_text is None or not parameter_text.strip() else tuple(parameter_text.split(","))
    return VariableDeclaration(match.group(1), tuple(parameter.strip() for parameter in parameters), match.group(3))


@dataclass(slots=True)
class VariableEnvironment:
    """Application service for declarations, isolated from template rendering state."""

    data: Mapping[str, Any]
    complement: Mapping[str, Any]
    declarations: Mapping[str, VariableDeclaration]

    def evaluate(self, name: str, arguments: tuple[Any, ...] = ()) -> Any:
        """Evaluate a declaration with positional arguments and only whitelisted AST nodes."""
        declaration = self.declarations.get(name)
        if declaration is None:
            raise VariableExpressionError(f"Unknown template variable: {name}")
        if len(arguments) != len(declaration.parameters):
            raise VariableExpressionError(f"Variable {name} expects {len(declaration.parameters)} argument(s)")
        values = dict(zip(declaration.parameters, arguments, strict=True))
        return _ExpressionEvaluator(self.data, self.complement, values).evaluate(declaration.expression)


@dataclass(frozen=True, slots=True)
class _ExpressionEvaluator:
    data: Mapping[str, Any]
    complement: Mapping[str, Any]
    values: Mapping[str, Any]

    def evaluate(self, expression: str) -> Any:
        try:
            node = ast.parse(expression, mode="eval").body
        except SyntaxError as error:
            raise VariableExpressionError("Invalid variable expression") from error
        return self._visit(node)

    def _visit(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            return _OPERATORS[type(node.op)](self._visit(node.left), self._visit(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd | ast.USub):
            return (
                operator.pos(self._visit(node.operand))
                if isinstance(node.op, ast.UAdd)
                else operator.neg(self._visit(node.operand))
            )
        if isinstance(node, ast.Name):
            return self._name(node.id)
        if isinstance(node, ast.Attribute):
            return self._attribute(node)
        raise VariableExpressionError(f"Unsupported expression syntax: {type(node).__name__}")

    def _name(self, name: str) -> Any:
        if name == "d":
            return self.data
        if name == "c":
            return self.complement
        if name in self.values:
            return self.values[name]
        raise VariableExpressionError(f"Unknown expression name: {name}")

    def _attribute(self, node: ast.Attribute) -> Any:
        value = self._visit(node.value)
        if not is_mapping(value) or node.attr not in value:
            raise VariableExpressionError(f"Unknown data path segment: {node.attr}")
        return value[node.attr]


def substitute(body: str, declarations: Mapping[str, VariableDeclaration]) -> str:
    """Macro substitution of ``$name``/``$name(args)`` in a marker body (parser.preprocessMarkers).

    Parameters are replaced positionally in the declared code, longest names first, before the
    marker text is compiled.
    """
    if "$" not in body:
        return body
    result = body
    for declaration in declarations.values():
        code = re.sub(r"\s", "", declaration.expression)
        ordered = sorted(enumerate(declaration.parameters), key=lambda item: -len(item[1]))
        for index, parameter in ordered:
            code = code.replace(parameter, f"_${index}_")
        pattern = re.compile(r"\$" + re.escape(declaration.name) + r"(?!\w)(?:\(([\s\S]+?)\))?")
        for match in pattern.finditer(body):
            replacement = code
            if match.group(1) is not None:
                for index, argument in enumerate(re.split(r" *, *", match.group(1))):
                    replacement = replacement.replace(f"_${index}_", argument)
            result = result.replace(match.group(0), replacement)
    return result
