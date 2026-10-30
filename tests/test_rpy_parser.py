from __future__ import annotations

from enginesave.engines.renpy.literals import DynamicValue
from enginesave.engines.renpy.rpy_parser import (
    logical_lines,
    parse_rpy,
    scan_python_source,
)

OPTIONS = '''\
## The name of the game. # not a comment start inside? (it is a comment)
define config.name = _("Moonlit Harbor")  # trailing comment

define gui.show_name = True
define config.version = "v1.0.1"
define gui.about = _p("""
    A long text with a # hash
    and "quotes" inside.
""")
define build.name = "MoonlitHarbor"
define config.has_sound = True
define -2 config.window_title = "Title # with hash"
default persistent.flag = False
define config.save_directory = "MoonlitHarbor"
define config.extra_savedirs = [
    "extra-a",  # comment inside brackets
    "extra-b",
]
init python:
    build.classify('**~', None)
    config.save_directory = "FromPython"
define config.window_icon = "gui/window_icon.png"
'''


def by_var(text: str) -> dict[str, list[object]]:
    result: dict[str, list[object]] = {}
    for a in parse_rpy(text, "game/options.rpy"):
        result.setdefault(a.var, []).append(a.value)
    return result


def test_options_file_extraction() -> None:
    found = parse_rpy(OPTIONS, "game/options.rpy")
    values = {(a.var, a.evidence.method): a.value for a in found}
    assert values[("config.name", "rpy_define")] == "Moonlit Harbor"
    assert values[("config.version", "rpy_define")] == "v1.0.1"
    assert values[("build.name", "rpy_define")] == "MoonlitHarbor"
    assert values[("config.window_title", "rpy_define")] == "Title # with hash"
    assert values[("config.save_directory", "rpy_define")] == "MoonlitHarbor"
    assert values[("config.extra_savedirs", "rpy_define")] == ["extra-a", "extra-b"]
    assert values[("config.save_directory", "python_assign")] == "FromPython"
    assert {a.var for a in found} <= {
        "config.name",
        "config.version",
        "build.name",
        "config.window_title",
        "config.save_directory",
        "config.extra_savedirs",
    }


def test_line_numbers_survive_multiline_strings() -> None:
    found = {a.var: a for a in parse_rpy(OPTIONS, "game/options.rpy")}
    lines = OPTIONS.split("\n")
    sd = next(
        a for a in parse_rpy(OPTIONS, "x") if a.var == "config.save_directory" and a.op == "="
    )
    assert lines[sd.evidence.line - 1].startswith("define config.save_directory")
    assert lines[found["build.name"].evidence.line - 1].startswith("define build.name")
    assert found["config.name"].evidence.detail == 'define config.name = _("Moonlit Harbor")'


def test_python_early_dynamic_savedir() -> None:
    text = (
        "python early:\n"
        "    import os\n"
        '    config.savedir = os.path.join(config.basedir, "saves")\n'
        "\n"
        'define config.save_directory = "Dyn-1"\n'
    )
    found = parse_rpy(text, "game/options.rpy")
    savedir = next(a for a in found if a.var == "config.savedir")
    assert isinstance(savedir.value, DynamicValue)
    assert savedir.evidence.method == "python_assign"
    assert savedir.evidence.line == 3
    assert any(a.var == "config.save_directory" and a.value == "Dyn-1" for a in found)


def test_python_block_ends_at_dedent() -> None:
    text = 'init -10 python in mystore:\n    x = 1\nconfig.save_directory = "not python"\n'
    assert parse_rpy(text, "f") == []


def test_extra_savedirs_append_extend_and_dollar() -> None:
    text = (
        "init python:\n"
        '    config.extra_savedirs.append("mirror")\n'
        '    config.extra_savedirs.extend(["m2", "m3"])\n'
        "label start:\n"
        "    $ config.save_directory = persistent.slot_dir\n"
    )
    found = parse_rpy(text, "f")
    assert [(a.var, a.op, a.value) for a in found[:2]] == [
        ("config.extra_savedirs", "append", "mirror"),
        ("config.extra_savedirs", "extend", ["m2", "m3"]),
    ]
    assert isinstance(found[2].value, DynamicValue)
    assert found[2].var == "config.save_directory"


def test_comparison_is_not_assignment() -> None:
    assert scan_python_source('config.savedir == "x"\nif config.savedir:\n    pass\n', "f") == []


def test_logical_lines_join_brackets_and_backslashes() -> None:
    text = "a = [\n  1,\n  2]\nb = 1 + \\\n  2\n'unterminated\nc = 3\n"
    lines = [(n, t.replace("\n", "|")) for n, _, t in logical_lines(text)]
    assert lines == [
        (1, "a = [|  1,|  2]"),
        (4, "b = 1 + \\|  2"),
        (6, "'unterminated"),
        (7, "c = 3"),
    ]


def test_non_target_and_nested_stores_ignored() -> None:
    text = 'define config.thumbnail_width = 384\ndefine config.sub.name = "x"\ndefine e = 1\n'
    assert parse_rpy(text, "f") == []
