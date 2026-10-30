"""Static extraction of save-related assignments from Ren'Py ``.rpy`` source text."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from enginesave.engines.renpy.literals import evaluate
from enginesave.models import Evidence

TARGET_VARS = frozenset(
    {
        "config.name",
        "config.version",
        "config.save_directory",
        "config.savedir",
        "config.extra_savedirs",
        "config.window_title",
        "build.name",
        "build.directory_name",
    }
)
SAVE_LOCATION_VARS = frozenset({"config.save_directory", "config.savedir", "config.extra_savedirs"})
MAX_DETAIL = 200

_DEFINE_RE = re.compile(
    r"^(?P<kind>define|default)\s+(?:-?\d+\s+)?"
    r"(?P<store>(?:config|build|gui|persistent)(?:\.[A-Za-z_]\w*)*)\.(?P<name>[A-Za-z_]\w*)"
    r"\s*(?P<op>=|\+=|\|=)(?!=)\s*(?P<expr>.+?)\s*$",
    re.S,
)
_PYTHON_HEADER_RE = re.compile(
    r"^(?:init(?:\s+-?\d+)?\s+)?python(?:\s+early)?(?:\s+hide)?(?:\s+in\s+[\w.]+)?\s*:$"
)
_PY_ASSIGN_RE = re.compile(
    r"^(?:renpy\.|store\.)?config\.(?P<name>savedir|save_directory|extra_savedirs)"
    r"\s*(?P<op>=(?!=)|\+=|\.append\(|\.extend\()\s*(?P<expr>.+?)\s*$",
    re.S,
)
_DOLLAR_RE = re.compile(r"^\$\s*(?P<code>.+)$", re.S)


@dataclass
class Assignment:
    var: str  # e.g. "config.save_directory"
    op: str  # "=", "+=", "|=", "append" or "extend"
    value: object  # literal value or DynamicValue
    evidence: Evidence


def clip(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= MAX_DETAIL else flat[: MAX_DETAIL - 3] + "..."


def logical_lines(text: str) -> Iterator[tuple[int, int, str]]:
    """Yield ``(line_number, indent, text)`` for each logical line with comments removed.

    Lines are joined (with ``\\n``) while inside a triple-quoted string, inside brackets, or
    after a trailing backslash. ``#`` inside string literals is not a comment.
    """
    quote: str | None = None
    depth = 0
    buffer: list[str] = []
    start = indent = 0
    for number, raw in enumerate(text.splitlines(), 1):
        out: list[str] = []
        i, n = 0, len(raw)
        while i < n:
            ch = raw[i]
            if quote:
                if ch == "\\":
                    out.append(raw[i : i + 2])
                    i += 2
                elif raw.startswith(quote, i):
                    out.append(quote)
                    i += len(quote)
                    quote = None
                else:
                    out.append(ch)
                    i += 1
                continue
            if ch == "#":
                break
            if ch in "'\"":
                quote = raw[i : i + 3] if raw[i : i + 3] in ("'''", '"""') else ch
                out.append(quote)
                i += len(quote)
                continue
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            out.append(ch)
            i += 1
        if quote in ("'", '"'):  # unterminated single-line string: give up on it
            quote = None
        line = "".join(out).rstrip()
        if not buffer:
            start = number
            indent = len(line) - len(line.lstrip(" \t"))
        buffer.append(line)
        if quote is None and depth == 0 and not line.endswith("\\"):
            yield start, indent, "\n".join(buffer)
            buffer = []
    if buffer:
        yield start, indent, "\n".join(buffer)


def scan_python_line(code: str, file_label: str, line: int | None) -> list[Assignment]:
    """Match ``config.savedir/save_directory/extra_savedirs`` assignments in one statement."""
    match = _PY_ASSIGN_RE.match(code.strip())
    if not match:
        return []
    op, expr = match.group("op"), match.group("expr")
    if op in (".append(", ".extend("):
        if expr.endswith(")"):
            expr = expr[:-1]
        op = op[1:-1]
    evidence = Evidence(file_label, "python_assign", clip(code), line)
    return [Assignment(f"config.{match.group('name')}", op, evaluate(expr), evidence)]


def scan_python_source(source: str, file_label: str) -> list[Assignment]:
    """Apply :func:`scan_python_line` to every logical line of a python block's source."""
    found: list[Assignment] = []
    for _, _, logical in logical_lines(source):
        if logical.strip():
            found.extend(scan_python_line(logical, file_label, None))
    return found


def parse_rpy(text: str, file_label: str, define_method: str = "rpy_define") -> list[Assignment]:
    """Extract target ``define``/``default`` statements and python save-location assignments."""
    found: list[Assignment] = []
    block_indent: int | None = None
    for number, indent, logical in logical_lines(text):
        stripped = logical.strip()
        if not stripped:
            continue
        if block_indent is not None:
            if indent > block_indent:
                found.extend(scan_python_line(stripped, file_label, number))
                continue
            block_indent = None
        if _PYTHON_HEADER_RE.match(stripped):
            block_indent = indent
            continue
        if match := _DEFINE_RE.match(stripped):
            var = f"{match.group('store')}.{match.group('name')}"
            if var in TARGET_VARS:
                op, expr = match.group("op"), match.group("expr")
                detail = clip(f"{match.group('kind')} {var} {op} {expr}")
                evidence = Evidence(file_label, define_method, detail, number)
                found.append(Assignment(var, op, evaluate(expr), evidence))
            continue
        if match := _DOLLAR_RE.match(stripped):
            found.extend(scan_python_line(match.group("code"), file_label, number))
    return found
