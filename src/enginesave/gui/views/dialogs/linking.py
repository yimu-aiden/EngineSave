"""「确认 / 关联…」 (0.8.0, D48 3): from a scanned game's right-click menu or its details, choose
the library record its folder belongs to (the one it is linked to, a suggested one, or a new
one) and confirm its CANDIDATE save folders; one click writes the ``link_root`` and the
``confirm_source`` decisions through the library (``enginesave.linking``), and the game is
scanned again, read-only, so that its row shows the result."""

from __future__ import annotations

import logging
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from enginesave.errors import EngineSaveError
from enginesave.gui import state
from enginesave.gui.app import set_enabled
from enginesave.gui.i18n import _
from enginesave.gui.views.dialogs.base import LINK_COLUMN, OPEN_LINK, PAD, Dialog, scrolled_tree
from enginesave.models import DetectedGame
from enginesave.repo import Repo

if TYPE_CHECKING:
    from enginesave.gui.app import App
    from enginesave.linking import LinkDecision

    Confirmed = tuple[LinkDecision, DetectedGame, state.Change]

log = logging.getLogger("enginesave.gui")


class ConfirmLinkDialog(Dialog):
    def __init__(self, app: App, game: DetectedGame, repo: Repo) -> None:
        super().__init__(app, f"{_(state.LINK_TITLE)} - {game.display_name}", min_size=(860, 460))
        self.game = game
        self.repo = repo
        catalog = repo.load_catalog()
        records = {r["game_id"]: r for r in catalog["games"]}
        wanted = [game.known_game_id, *(s["game_id"] for s in game.link_suggestions)]
        names = {}
        for game_id in (g for g in wanted if g in records):
            try:
                doc = repo.load_game(game_id)
            except EngineSaveError:
                doc = None
            names[game_id] = state.record_choice_name(records[game_id], doc)
        self.names = names
        self.choices, first = state.link_choices(game, names)

        ttk.Label(self.body, text=_(state.LINK_INTRO), wraplength=820).pack(anchor="w")
        ttk.Label(self.body, text=_("游戏目录：{game_root}", game_root=game.game_root)).pack(
            anchor="w", pady=(PAD, 0)
        )
        records_frame = ttk.LabelFrame(self.body, text=_("这个游戏目录属于哪条记录"), padding=4)
        records_frame.pack(fill="x", pady=(PAD, 0))
        self.choice = tk.IntVar(value=first)
        self.choice_buttons = [
            ttk.Radiobutton(records_frame, text=choice.label, variable=self.choice, value=index)
            for index, choice in enumerate(self.choices)
        ]
        for button in self.choice_buttons:
            button.pack(anchor="w")

        sources_frame = ttk.LabelFrame(self.body, text=_("待确认的存档来源"), padding=4)
        sources_frame.pack(fill="both", expand=True, pady=(PAD, 0))
        self.sources = {f"s{i}": s for i, s in enumerate(state.candidate_sources(game))}
        self.ticked = set(self.sources)  # all ticked at first
        self.tree = scrolled_tree(
            sources_frame,
            [
                ("check", _("勾选"), 46),
                ("path", _("路径"), 380),
                ("files", _("文件数"), 60),
                ("size", _("大小"), 80),
                ("reason", _("为什么待确认"), 220),
                LINK_COLUMN,
            ],
            height=4,
        )
        self.tree.column("check", anchor="center")
        for iid, source in self.sources.items():
            self.links[iid] = Path(source.path)
            values = state.candidate_values(source, state.CHECKED)
            self.tree.insert("", "end", iid=iid, values=(*values, _(OPEN_LINK)))
        if not self.sources:
            ttk.Label(sources_frame, text=_(state.NO_CANDIDATES)).pack(anchor="w")
        self.tree.bind("<ButtonRelease-1>", self.click)
        self.tree.bind("<space>", self.space)
        self.link_column(self.tree)

        self.button(_("取消"), self.cancel, action=False)
        self.confirm_button = self.primary(
            state.confirm_primary_text(len(self.ticked)), self.confirm
        )
        self.refresh_buttons()
        self.show()

    # ------------------------------------------------------------------------------ ticks

    def click(self, event: tk.Event) -> None:
        if (
            self.tree.identify_region(event.x, event.y) == "cell"
            and self.tree.identify_column(event.x) == "#1"
        ):
            iid = self.tree.identify_row(event.y)
            if iid:
                self.toggle(iid)

    def space(self, _event: tk.Event) -> str:
        for iid in self.tree.selection():
            self.toggle(iid)
        return "break"

    def toggle(self, iid: str) -> None:
        if iid in self.sources:
            self.ticked ^= {iid}
            self.refresh_buttons()

    def refresh_buttons(self) -> None:
        if not self.winfo_exists():  # closed when the confirmation was written
            return
        for iid in self.sources:
            self.tree.set(iid, "check", state.CHECKED if iid in self.ticked else state.UNCHECKED)
        self.confirm_button.configure(text=state.confirm_primary_text(len(self.ticked)))
        set_enabled(self.confirm_button, not self.busy)

    # ------------------------------------------------------------------------------ confirm

    def chosen(self) -> state.LinkChoice:
        return self.choices[self.choice.get()]

    def confirm(self) -> None:
        if self.busy:
            return
        game, choice = self.game, self.chosen()
        sources = [self.sources[iid] for iid in self.sources if iid in self.ticked]
        root, override, env = self.repo.root, self.app.saves_override(), self.app.env
        cache_file = self.app.cache_file
        include_candidates = self.app.settings.include_candidates

        def job(_progress: Callable[[Any], None]) -> Confirmed:
            from enginesave.linking import confirm_and_link, game_at
            from enginesave.scan_cache import ScanCache

            repo = Repo.open(root)
            decision = confirm_and_link(repo, game, choice.game_id, sources)
            others = [Path(i["game_root"]) for i in game.installs if i.get("game_root")]
            others = [p for p in others if p != Path(game.game_root) and p.is_dir()]
            fresh = game_at(
                Path(game.game_root),
                repo=root,
                saves_root_override=override,
                env=env,
                also=others,
                cache=ScanCache.load(cache_file),
            )
            # D54: which group the game goes into now
            change = state.save_change(fresh, repo, include_candidates=include_candidates)
            return decision, fresh, change

        self.run(
            _("确认 / 关联"),
            job,
            self.confirmed,
            status=_("正在写入 {display_name} 的关联…", display_name=game.display_name),
            lock=root,  # D73 4
        )

    def confirmed(self, value: Confirmed) -> None:
        decision, fresh, change = value
        record = self.names.get(decision.game_id) or fresh.display_name
        log.info(state.link_done_log(self.game, decision, record))
        self.close()
        self.app.game_confirmed(self.game, fresh, change)
