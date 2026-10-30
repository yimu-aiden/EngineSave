from __future__ import annotations

import pytest

from enginesave.engines.renpy.literals import DynamicValue, evaluate


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ('"MoonlitHarbor"', "MoonlitHarbor"),
        ("'single'", "single"),
        ('_("Moonlit Harbor")', "Moonlit Harbor"),
        ("__('dunder')", "dunder"),
        ('_p("""Multi\nline""")', "Multi\nline"),
        ('u"unicode"', "unicode"),
        ('r"C:\\saves"', "C:\\saves"),
        ('"implicit" "concat"', "implicitconcat"),
        ("None", None),
        ("True", True),
        ("False", False),
        ("42", 42),
        ("-3", -3),
        ('["a", "b"]', ["a", "b"]),
        ('("a",)', ["a"]),
        ("[]", []),
        ('_(_("nested"))', "nested"),
    ],
)
def test_literals(expr: str, expected: object) -> None:
    assert evaluate(expr) == expected


@pytest.mark.parametrize(
    "expr",
    [
        'os.path.join(config.basedir, "saves")',
        '"a" + "b"',
        'f"{x}"',
        "some_name",
        "1.5",
        '["a", other]',
        "[1, 2]",
        '_("x", "y")',
        'gettext("x")',
        "{'a': 1}",
        "",
        "(",
        '"x" if y else "z"',
    ],
)
def test_dynamic(expr: str) -> None:
    value = evaluate(expr)
    assert isinstance(value, DynamicValue)
    assert value.raw == expr


def test_overlong_expression_is_dynamic() -> None:
    assert isinstance(evaluate('"' + "x" * 20_000 + '"'), DynamicValue)


@pytest.mark.parametrize("sign", ["", "-"])
def test_an_int_too_long_to_write_out_is_dynamic(sign: str) -> None:
    """A short hexadecimal literal can stand for more than the 4300 digits Python writes out;
    kept as an int it would break the report and the scan cache (M32 review)."""
    value = evaluate(sign + "0x" + "f" * 3700)
    assert isinstance(value, DynamicValue)
    assert evaluate(sign + "0x" + "f" * 100) == int(sign + "0x" + "f" * 100, 16)
