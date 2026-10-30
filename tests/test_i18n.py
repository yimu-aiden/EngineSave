"""0.11.0 (D68, M47): the window in English. Every text the window looks up has an English
entry without Chinese characters and with the same placeholders; no Chinese text of the window
is left outside ``_()`` (module-level constants are looked up where they are used)."""

from __future__ import annotations

import ast
import re
import string
from pathlib import Path

import pytest

from enginesave.gui import i18n
from enginesave.gui.i18n_en import EN

GUI = Path(__file__).resolve().parents[1] / "src" / "enginesave" / "gui"
CJK = re.compile(r"[　-鿿＀-￯]")
UNTRANSLATED = {"EXCLUDE_PROBES"}  # folder names a pattern is tried on, not shown


def fields(text: str) -> list[str]:
    return sorted(
        f[1] + (f"!{f[3]}" if f[3] else "") + (f":{f[2]}" if f[2] else "")
        for f in string.Formatter().parse(text)
        if f[1] is not None
    )


def printf(text: str) -> list[str]:
    return re.findall(r"%(?:\([^)]*\))?[-#0+]*\d*(?:\.\d+)?[sdirfx]", text)


def gui_files() -> list[Path]:
    return [p for p in sorted(GUI.rglob("*.py")) if p.name not in ("i18n.py", "i18n_en.py")]


def looked_up() -> dict[str, str]:
    """Every literal text of a ``_()`` call and every Chinese string a module- or class-level
    constant holds, with the file it is in."""
    found: dict[str, str] = {}
    for path in gui_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.setdefault(node.args[0].value, path.name)
        scopes = [tree, *[n for n in tree.body if isinstance(n, ast.ClassDef)]]
        for scope in scopes:
            for node in scope.body:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id in UNTRANSLATED for t in targets):
                    continue
                for inner in ast.walk(node.value):
                    if (
                        isinstance(inner, ast.Constant)
                        and isinstance(inner.value, str)
                        and CJK.search(inner.value)
                    ):
                        found.setdefault(inner.value, path.name)
    return found


def test_every_text_has_english_with_the_same_placeholders() -> None:
    texts = looked_up()
    missing = sorted(t for t in texts if CJK.search(t) and t not in EN)
    assert not missing, f"{len(missing)} texts without English, e.g. {missing[:3]}"
    for zh, entry in EN.items():
        forms = entry if isinstance(entry, tuple) else (entry,)
        assert len(forms) in (1, 2), f"a pair is (one, many): {zh!r}"
        if len(forms) == 2:
            assert fields(zh), f"a pair needs its one number to choose by: {zh!r}"
        for en in forms:
            assert not CJK.search(en), f"Chinese left in the English of {zh!r}: {en!r}"
            assert fields(zh) == fields(en), f"placeholders of {zh!r} -> {en!r}"
            assert printf(zh) == printf(en), f"printf tokens of {zh!r} -> {en!r}"
    unused = sorted(set(EN) - set(texts))
    assert not unused, f"{len(unused)} English entries no text uses, e.g. {unused[:3]}"


def test_no_chinese_text_of_the_window_is_outside_the_lookup() -> None:
    """Inside functions every Chinese literal is the text of a ``_()`` call (docstrings and
    ``log.debug`` aside)."""
    stray = []
    for path in gui_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        allowed: set[int] = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if (
                isinstance(body, list)
                and body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                allowed.add(id(body[0].value))  # docstrings
            if isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id in ("_", "N_"))
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "debug")
            ):
                for arg in node.args:
                    allowed.update(id(n) for n in ast.walk(arg))
        functions = [
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for function in functions:
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and CJK.search(node.value)
                    and id(node) not in allowed
                ):
                    stray.append(f"{path.name}:{node.lineno} {node.value[:30]!r}")
    assert not stray, f"{len(stray)} Chinese texts not looked up: {stray[:5]}"


def chinese_constants() -> set[str]:
    """The names of the module- and class-level constants of the window that hold Chinese,
    also through another such constant (``f"{LINK_TITLE}…"``)."""
    assignments = []
    for path in gui_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes = [tree, *[n for n in tree.body if isinstance(n, ast.ClassDef)]]
        for scope in scopes:
            for node in scope.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names = {t.id for t in targets if isinstance(t, ast.Name)}
                    assignments.append((names, list(ast.walk(node.value))))
    found: set[str] = set()
    while True:
        more = {
            name
            for names, inner in assignments
            if any(
                (isinstance(n, ast.Constant) and isinstance(n.value, str) and CJK.search(n.value))
                or (isinstance(n, ast.Name) and n.id in found)
                for n in inner
            )
            for name in names
        } - UNTRANSLATED
        if more <= found:
            return found
        found |= more


# Uses of such a constant that are looked up later, by the code they are handed to
LOOKED_UP_LATER = {
    ("app.py", "FAILED"),  # the default title of show_error, which looks it up
    ("common.py", "CANCELLING"),  # the cancel button's text, looked up by the dialog
    ("dialogs/base.py", "CANCEL"),
    ("cleaning.py", "CLEANUP_OUTCOMES"),  # its keys tested; the values looked up at use
    ("guidance.py", "FIRST_STEPS"),  # each step looked up in the loop
    ("dialogs/removing.py", "KIND_TEXTS"),  # record_values looks the kind up
    ("library_view.py", "VERIFY_COLUMNS"),  # make_tree looks the headings up
    ("library_view.py", "RECORD_COLUMNS"),
    ("library_view.py", "SNAPSHOT_COLUMNS"),
    ("scan_view.py", "GAME_COLUMNS"),
}


def test_module_texts_are_looked_up_where_they_are_used() -> None:
    """A Chinese module constant used inside a function sits in a ``_()`` call (or is a
    mapping or tuple whose values are looked up by the code that reads them): the case of
    「确认 / 关联…」 and the remove dialog's kind (M47 review)."""
    names = chinese_constants()
    bare = []
    for path in gui_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where = path.relative_to(GUI).as_posix().removeprefix("views/").removeprefix("state/")
        parents: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
        for node in ast.walk(tree):
            name = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else None
            )
            if name not in names or not isinstance(getattr(node, "ctx", None), ast.Load):
                continue
            # 1.0.0 (D70 11): ``X[key]`` and ``X.get(key)`` are a text: in _() as well; only
            # going over all of it (.items(), .values(), .keys()) leaves the lookup to the loop
            inside_function = wrapped = iterated = False
            parent = parents.get(id(node))
            child: ast.AST = node
            while parent is not None:
                if isinstance(parent, ast.Call) and child in parent.args:
                    func = parent.func
                    if isinstance(func, ast.Name) and func.id in ("_", "N_"):
                        wrapped = True
                if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute):
                    if parent.func.attr in ("items", "values", "keys") and child is parent.func:
                        iterated = True
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    inside_function = True
                child, parent = parent, parents.get(id(parent))
            looked_up = wrapped or iterated
            if inside_function and not looked_up and (where, name) not in LOOKED_UP_LATER:
                bare.append(f"{where}:{node.lineno} {name}")
    assert not bare, f"{len(bare)} Chinese constants used without _(): {sorted(set(bare))[:8]}"


GLOSSARY = [  # D68 4: the terms the window uses
    ("游戏目录", "game folder"),
    ("本地存档目录", "local save folder"),
    ("全局存档目录", "global save folder"),
    ("全局存档根目录", "global saves root"),
    ("孤立存档目录", "orphaned save folder"),
    ("对象库", "object store"),
]
BUTTONS = [
    ("扫描与备份", "Scan & back up"),
    ("备份已勾选项", "Back up ticked"),
    ("本体存在", "Game folder present"),
    ("本体不在", "Game folder absent"),
    ("有新存档可备份", "New saves to back up"),
    ("已备份、无新存档", "Backed up, no new saves"),
    ("待确认", "Needs confirmation"),
    ("不支持", "Unsupported"),
    ("预览…", "Preview…"),
    ("全选", "Select all"),
    ("全不选", "Select none"),
    ("刷新", "Refresh"),
    ("校验", "Verify"),
]


@pytest.mark.parametrize(("zh", "en"), BUTTONS)
def test_the_glossary_names(zh: str, en: str) -> None:
    """D68 4: these texts are in the table and read exactly so."""
    assert zh in EN, f"{zh!r} is no longer a text of the window"
    assert EN[zh] == en


@pytest.mark.parametrize(("zh", "en"), GLOSSARY)
def test_the_glossary_terms(zh: str, en: str) -> None:
    """D68 4: every text that says the term says it in the same English."""
    using = {k: v for k, v in EN.items() if zh in k}
    assert using, f"no text uses {zh!r}"
    for key, entry in using.items():
        for value in entry if isinstance(entry, tuple) else (entry,):
            assert en.casefold() in value.casefold(), f"{key!r} -> {value!r} lacks {en!r}"


def test_the_remove_rows_say_the_kind_in_english() -> None:
    """M47 review: the remove dialog's record rows looked the kind up nowhere."""
    from enginesave.gui import state
    from enginesave.gui.state.removing import RemoveRecord, record_values
    from enginesave.removal import Inventory

    record = RemoveRecord(
        game_id="g", name="Game", kind="unlinked", installed=False, snapshots=("s1",)
    )
    try:
        i18n.set_language("en")
        values = record_values(record, Inventory(), "", state.KIND_TEXTS)
        assert values[2] == "Orphaned folder · 1 snapshot"
    finally:
        i18n.set_language("zh")
    assert record_values(record, Inventory(), "", state.KIND_TEXTS)[2] == "孤立目录 · 1 个快照"


def test_lookup_falls_back_to_chinese_and_fills_templates() -> None:
    try:
        i18n.set_language("en")
        assert i18n._("扫描与备份") == "Scan & back up"
        assert i18n._("一段没有英文的文字") == "一段没有英文的文字"
        assert "一段没有英文的文字" in i18n.missing()
        i18n.set_language("zh")
        assert i18n._("已勾选 {n} 款", n=3) == "已勾选 3 款"
        i18n.set_language("en")  # a pair: one, many
        assert i18n._("已勾选 {games} 款游戏", games=1) == "1 game ticked"
        assert i18n._("已勾选 {games} 款游戏", games=2) == "2 games ticked"
        assert i18n._("已勾选 {games} 款游戏", games=0) == "0 games ticked"
    finally:
        i18n.set_language("zh")
        i18n._missing.discard("一段没有英文的文字")  # not left for the window's own check


def test_the_counts_read_in_english() -> None:
    """D70 8 (S-223, S-232): one number -- its singular or plural; two numbers -- "label: n";
    and the words D70 8 names."""
    from enginesave.gui import state
    from enginesave.removal import LibraryStats

    try:
        i18n.set_language("en")
        assert i18n._("确认（{count} 个来源）", count=1) == "Confirm (1 source)"
        assert i18n._("确认（{count} 个来源）", count=3) == "Confirm (3 sources)"
        assert i18n._("{file_count} 个文件 · {v}", file_count=1, v="2 KB") == "1 file · 2 KB"
        assert i18n._("永久删除（{records} 条记录）", records=2) == "Delete permanently (2 records)"
        summary = i18n._(
            "{n} 款游戏（{v}）；跳过 {n2}，警告 {n3}", n=1, v="1 CONFIRMED", n2=0, n3=0
        )
        assert summary == "games: 1 (1 CONFIRMED); skipped: 0, warnings: 0"
        assert i18n._("界面语言：") == "Language:" and i18n._("一致") == "Matched"
        overview = state.overview_text(LibraryStats())
        assert "No mirror yet" in overview and "None" not in overview
        note = i18n._("（空的备份库：在“扫描与备份”页备份后会出现在这里）")
        assert note.startswith(" (") and note.endswith('"Scan & back up" tab)')
    finally:
        i18n.set_language("zh")
    assert "上次镜像 无" in state.overview_text(LibraryStats())
