"""Evaluate simple Python literals from Ren'Py scripts without executing anything."""

from __future__ import annotations

import ast
from dataclasses import dataclass

MAX_EXPR_LENGTH = 10_000
_TRANSLATION_WRAPPERS = frozenset({"_", "__", "_p"})


@dataclass(frozen=True)
class DynamicValue:
    """An expression that is not a plain literal (calls, operators, names, f-strings...)."""

    raw: str


def _unwrap(node: ast.expr) -> ast.expr:
    while (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _TRANSLATION_WRAPPERS
        and len(node.args) == 1
        and not node.keywords
    ):
        node = node.args[0]
    return node


def printable_int(value: object) -> bool:
    """False for an int too long to write out (Python refuses more than 4300 digits; a long
    hexadecimal literal gets there): such a value is treated as dynamic, never kept."""
    try:
        str(value)
    except ValueError:
        return False
    return True


def _scalar(node: ast.expr) -> tuple[bool, object]:
    if isinstance(node, ast.Constant) and (
        node.value is None or isinstance(node.value, str | bool | int)
    ):
        return printable_int(node.value), node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub | ast.UAdd)
        and isinstance(node.operand, ast.Constant)
        and type(node.operand.value) is int
        and printable_int(node.operand.value)
    ):
        value = node.operand.value
        return True, -value if isinstance(node.op, ast.USub) else value
    return False, None


def evaluate(expr: str) -> object:
    """Return the literal value of ``expr`` or a :class:`DynamicValue`.

    Accepted: string literals (any prefix/quotes, implicit concatenation), ``None``, booleans,
    integers, optionally wrapped in ``_()``, ``__()`` or ``_p()``; and lists/tuples whose items
    are all string literals (returned as ``list[str]``). Everything else is dynamic.
    """
    text = expr.strip()
    if not text or len(text) > MAX_EXPR_LENGTH:
        return DynamicValue(expr)
    try:
        node = _unwrap(ast.parse(text, mode="eval").body)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return DynamicValue(expr)
    ok, value = _scalar(node)
    if ok:
        return value
    if isinstance(node, ast.List | ast.Tuple) and all(
        isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts
    ):
        return [item.value for item in node.elts]
    return DynamicValue(expr)
