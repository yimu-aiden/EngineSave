"""D47: one name for one thing in the window. Every string literal under ``src/enginesave/gui``
(docstrings and log messages included, and the constant parts of f-strings) is free of the
wordings 0.8.0 retired; the command line's English output is not part of this."""

from __future__ import annotations

import ast
from pathlib import Path

GUI = Path(__file__).resolve().parents[1] / "src" / "enginesave" / "gui"
RETIRED = (
    "安装目录",  # 游戏目录 (D47 2)
    "游戏文件夹",
    "安装位置",
    "根文件夹",
    "未关联",  # 孤立存档目录 (D47 4)
    "目录不存在",  # 不存在 (D47 5)
    "目录已不在",
    "本体已清理",  # 已清理
    "无法恢复",  # 无法还原 (D47 6)
    "不可恢复",
    "saves root：",  # 全局存档根目录 (D47 3)
    "存档根下",
    "全局目录",  # 全局存档目录
    "备份所选",  # 备份已勾选项 (D47 7)
    "已清理",  # 不存在 (D51: who removed a folder changes nothing)
    "仅备份",  # 本体不在 (D51)
)


def literals(source: str) -> list[tuple[int, str]]:
    """``(line, text)`` of every string constant: adjacent literals are joined by the parser,
    an f-string's constant parts are constants of their own."""
    return [
        (node.lineno, node.value)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def retired_words(source: str) -> list[tuple[int, str]]:
    return [(line, word) for line, text in literals(source) for word in RETIRED if word in text]


def test_the_window_uses_the_wording_table() -> None:
    files = sorted(GUI.rglob("*.py"))
    assert len(files) > 20, "the whole window is scanned"
    hits = [
        f"{path.relative_to(GUI)}:{line}: {word}"
        for path in files
        for line, word in retired_words(path.read_text(encoding="utf-8"))
    ]
    assert hits == []


def test_the_scan_sees_through_f_strings_and_joined_literals() -> None:
    source = 'a = f"安装{1}目录不存在"\nb = ("全局" "目录")\n"""未关联"""\nc = 1  # 安装目录\n'
    assert sorted(retired_words(source)) == [(1, "目录不存在"), (2, "全局目录"), (3, "未关联")]
