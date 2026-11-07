"""Library upkeep dialogs (0.9.0): 「清理临时文件夹」 (D55) and 「镜像到…」 (D57), both from the
library page. The work runs on the window's worker thread through ``enginesave.repo`` and
``enginesave.mirror``; each dialog has one primary button."""

from __future__ import annotations

import logging
import os
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, ttk
from typing import TYPE_CHECKING, Any

from enginesave.errors import EngineSaveError
from enginesave.gui import state
from enginesave.gui.app import locked_job, set_enabled
from enginesave.gui.i18n import _
from enginesave.gui.views.dialogs.base import PAD, WARNING_COLOR, Dialog, scrolled_tree
from enginesave.repo import Repo

if TYPE_CHECKING:
    from enginesave.gui.app import App
    from enginesave.migrate import MigrationPlan, MigrationResult
    from enginesave.mirror import MirrorResult
    from enginesave.repo import TmpFolder, TmpRemoval

log = logging.getLogger("enginesave.gui")


class TmpDialog(Dialog):
    """The leftovers of interrupted runs (D55): those that may go start ticked, those changed
    in the last ten minutes are greyed; one click deletes the ticked ones and collects the
    object store."""

    def __init__(self, app: App, repo: Repo) -> None:
        super().__init__(app, _(state.TMP_TITLE), min_size=(860, 380))
        self.repo = repo
        self.folders: dict[str, TmpFolder] = {}
        self.ticked: set[Path] = set()
        self.done = False
        ttk.Label(self.body, text=_(state.TMP_INTRO), wraplength=820).pack(anchor="w")
        self.tree = scrolled_tree(
            self.body,
            [
                ("check", _("勾选"), 46),
                ("path", _("文件夹"), 430),
                ("size", _("大小"), 80),
                ("files", _("文件数"), 60),
                ("modified", _("最后修改"), 130),
                ("note", _("说明"), 110),
            ],
            height=8,
        )
        self.tree.column("check", anchor="center")
        self.tree.tag_configure("recent", foreground="#8a8a8a")
        self.tree.bind("<ButtonRelease-1>", self.click)
        self.tree.bind("<space>", self.space)
        self.summary = tk.StringVar(value=_(state.READING))
        ttk.Label(self.body, textvariable=self.summary, wraplength=820).pack(
            anchor="w", pady=(PAD, 0)
        )
        self.button(_("关闭"), self.cancel, action=False)
        self.delete_button = self.primary(state.tmp_primary_text(0), self.delete)
        self.refresh_buttons()
        self.show()
        self.run_soon(
            _("读取临时文件夹"), lambda _p: repo.tmp_folders(), self.loaded, status=_(state.READING)
        )

    def loaded(self, folders: list[TmpFolder]) -> None:
        self.tree.delete(*self.tree.get_children())
        self.folders = {f"t{index}": folder for index, folder in enumerate(folders)}
        self.ticked = state.tmp_ticks(folders)
        for iid, folder in self.folders.items():
            values = state.tmp_row_values(folder, folder.path in self.ticked)
            tags = ("recent",) if folder.recent() else ()
            self.tree.insert("", "end", iid=iid, values=values, tags=tags)
        recent = sum(1 for f in folders if f.recent())
        self.summary.set(
            _("{n} 个临时文件夹，{n2} 个可以删除", n=len(folders), n2=len(self.ticked))
            + (_("，{recent} 个可能正在使用", recent=recent) if recent else "")
            + _("。")
        )
        self.refresh_buttons()

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
        folder = self.folders.get(iid)
        if folder is None or folder.recent() or self.done or self.busy:
            return
        self.ticked ^= {folder.path}
        self.tree.set(
            iid, "check", state.CHECKED if folder.path in self.ticked else state.UNCHECKED
        )
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        if not self.winfo_exists():
            return
        self.delete_button.configure(text=state.tmp_primary_text(len(self.ticked)))
        set_enabled(self.delete_button, bool(self.ticked) and not self.done and not self.busy)

    def delete(self) -> None:
        if self.busy or self.done or not self.ticked:
            return
        chosen, repo = list(self.ticked), self.repo

        def job(_progress: Callable[[Any], None]) -> TmpRemoval:
            return repo.remove_tmp_dirs(chosen, should_stop=self.app.stop_requested)

        self.run(
            _("清理临时文件夹"),
            job,
            self.deleted,
            status=_("正在删除临时文件夹…"),
            cancellable=True,
            lock=repo.root,  # D73 4
        )

    def deleted(self, result: TmpRemoval) -> None:
        text = state.tmp_done_text(result)
        log.info(_("清理临时文件夹：%s"), text)
        for path in result.leftovers:
            log.warning(_("没能删除：%s"), path)
        self.done = True
        self.summary.set(text)
        for iid, folder in self.folders.items():
            if str(folder.path) in result.removed and self.tree.exists(iid):
                self.tree.delete(iid)
        self.refresh_buttons()
        self.app.library_changed()


class MirrorDialog(Dialog):
    """「镜像到…」 (D57): the folder (the last mirror's, or chosen here), whether to verify
    afterwards, and one click. The folder is checked first (read-only); what is wrong with it
    disables the button. Progress is in the status bar."""

    def __init__(self, app: App, repo: Repo, target: Path | None) -> None:
        super().__init__(app, _(state.MIRROR_TITLE), min_size=(760, 300))
        self.repo = repo
        self.target: Path | None = target
        self.ok = False  # the target passed the check
        self.done = False
        ttk.Label(self.body, text=_(state.MIRROR_INTRO), wraplength=720).pack(anchor="w")
        ttk.Label(self.body, text=_("备份库：{root}", root=repo.root)).pack(
            anchor="w", pady=(PAD, 0)
        )
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(4, 0))
        self.target_var = tk.StringVar(value=_(state.MIRROR_NONE))
        ttk.Label(row, text=_("镜像到：")).pack(side="left")
        ttk.Label(row, textvariable=self.target_var).pack(side="left")
        change = ttk.Button(row, text=_("更换文件夹…"), command=self.choose)
        change.pack(side="right")
        self.verify = tk.BooleanVar(value=app.settings.mirror_verify)
        verify = ttk.Checkbutton(self.body, text=_(state.MIRROR_VERIFY), variable=self.verify)
        verify.pack(anchor="w", pady=(PAD, 0))
        self.repair = tk.BooleanVar(value=False)  # D59 3
        repair = ttk.Checkbutton(self.body, text=_(state.MIRROR_REPAIR), variable=self.repair)
        repair.pack(anchor="w")
        self.actions += [change, verify, repair]  # disabled while a check or the mirror runs
        self.check_var = tk.StringVar(value="")
        self.check_label = ttk.Label(self.body, textvariable=self.check_var, wraplength=720)
        self.check_label.pack(anchor="w", pady=(PAD, 0))
        self.result = tk.StringVar(value="")
        ttk.Label(self.body, textvariable=self.result, wraplength=720).pack(anchor="w")
        self.button(_("关闭"), self.cancel, action=False)
        self.mirror_button = self.primary(state.mirror_primary_text(target), self.start)
        self.refresh_buttons()
        self.show()
        if target is not None:
            self.check()

    def choose(self) -> None:
        if self.busy:
            return
        chosen = filedialog.askdirectory(
            parent=self,
            title=_("选择镜像文件夹（空文件夹，或之前的镜像；最好在另一块盘上）"),
            initialdir=str(self.target.parent if self.target else ""),
            mustexist=False,
        )
        if chosen:
            self.target = Path(os.path.normpath(chosen))
            self.check()

    def check(self) -> None:
        """Read-only, on the worker: the folder may be used (D57 1), and how."""
        target, repo = self.target, self.repo
        if target is None:
            return
        self.ok = False
        self.target_var.set(str(target))
        self.mirror_button.configure(text=state.mirror_primary_text(target))
        self.check_var.set(_("正在检查这个文件夹…"))
        self.check_label.configure(foreground="")
        roots = [Path(r) for r in self.app.settings.scan_roots]
        override, env = self.app.saves_override(), self.app.env

        def job(_progress: Callable[[Any], None]) -> str:
            from enginesave.mirror import check_target, forbidden_places

            places = forbidden_places(repo, saves_root=override, env=env, also=roots)
            return check_target(repo, Path(os.path.abspath(target)), places)

        self.run_soon(
            _("检查镜像文件夹"),
            job,
            self.checked,
            status=_("正在检查镜像文件夹…"),
            on_error=self.refused,
        )

    def checked(self, kind: str) -> None:
        self.ok = True
        text = _(state.MIRROR_NEW) if kind == "new" else _(state.MIRROR_AGAIN)
        if self.target is not None and state.same_drive(self.target, self.repo.root):
            text += " " + _(state.SAME_DRIVE)
        self.check_var.set(text)
        self.refresh_buttons()

    def refused(self, exc: BaseException) -> None:
        self.ok = False
        self.check_var.set(_("不能用这个文件夹：{exc}", exc=exc))
        self.check_label.configure(foreground=WARNING_COLOR)
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        if not self.winfo_exists():
            return
        ready = self.ok and self.target is not None and not self.done and not self.busy
        set_enabled(self.mirror_button, ready)

    def start(self) -> None:
        if not self.ok or self.busy or self.done or self.target is None:
            return
        target, repo, verify, repair = self.target, self.repo, self.verify.get(), self.repair.get()
        roots = [Path(r) for r in self.app.settings.scan_roots]
        override, env = self.app.saves_override(), self.app.env
        stop = self.app.stop_requested

        def job(progress: Callable[[Any], None]) -> MirrorResult:
            from enginesave.mirror import forbidden_places, mirror_repo

            places = forbidden_places(repo, saves_root=override, env=env, also=roots)

            def write(_p: Callable[[Any], None]) -> MirrorResult:
                return mirror_repo(
                    repo,
                    target,
                    forbidden=places,
                    verify=verify,
                    repair=repair,
                    progress=lambda done, total, name: progress((done, total, name)),
                    should_stop=stop,
                )

            if not target.is_dir():  # a new mirror: nothing else can be writing it yet
                return write(progress)
            return locked_job(write, target, _("镜像"))(progress)  # D73 4

        log.info(_("开始镜像：%s → %s"), repo.root, target)
        self.run(
            _("镜像"),
            job,
            self.mirrored,
            status=_("正在镜像…"),
            progress=self.app.mirror_progress,
            on_error=self.failed,
            cancellable=True,
        )

    def failed(self, exc: BaseException) -> None:
        if isinstance(exc, EngineSaveError):  # the folder changed since the check
            self.refused(exc)
            return
        self.app.show_error(exc, parent=self)
        self.app.library_changed()

    def mirrored(self, result: MirrorResult) -> None:
        from enginesave.mirror import record_mirror

        text = state.mirror_done_text(result)
        level = logging.INFO if result.exit_code == 0 and not result.cancelled else logging.WARNING
        log.log(level, text)
        for item in [*result.failed, *result.unreadable]:
            log.warning(_("镜像：%s"), item)
        for check in result.checks or []:
            if not check.clean:
                log.warning(_("镜像校验发现问题：%s %s"), check.display_name, check.snapshot_id)
        if result.objects is not None and not result.objects.clean:
            log.warning(_("镜像的对象库校验发现问题：%s"), result.objects.mismatched[:10])
        for name in result.kept_records:
            log.info(_("镜像里保留了备份库已删除的记录：%s"), name)
        self.done = True
        self.result.set(text)
        if result.exit_code == 0 and not result.cancelled:  # 「上次镜像」 only when it worked
            record_mirror(self.app.settings_file.parent, self.repo.root, result.target)
        self.app.settings.mirror_folder = str(result.target)  # where to go on next time
        self.app.settings.mirror_verify = bool(self.verify.get())  # D64: the automatic one too
        self.app.save_settings()
        self.refresh_buttons()
        self.app.library_changed()


class MigrateDialog(Dialog):
    """D67 8: a library of format 1, asked once when it is opened (and again before a write):
    「迁移」 renames its record folders after the games, 「稍后」 leaves it read-only."""

    def __init__(self, app: App, repo: Repo, plan: MigrationPlan) -> None:
        super().__init__(app, _(state.MIGRATE_TITLE), min_size=(640, 260))
        self.repo = repo
        self.intro = ttk.Label(self.body, wraplength=600)
        self.intro.pack(anchor="w")
        self.details = ttk.Label(self.body, wraplength=600)
        self.details.pack(anchor="w", pady=(PAD, 0))
        self.result = tk.StringVar()
        ttk.Label(self.body, textvariable=self.result, wraplength=600).pack(anchor="w")
        self.button(_(state.LATER), self.cancel, action=False)
        self.migrate_button = self.primary(_(state.MIGRATE), self.migrate)
        self.planned(plan)
        self.show()

    def planned(self, plan: MigrationPlan) -> None:
        """What the dialog says of ``plan``; 「迁移」 is greyed while it is refused."""
        self.plan = plan
        self.intro.configure(text=state.migrate_intro(plan))
        self.details.configure(
            text=state.migrate_details(plan), foreground=WARNING_COLOR if plan.refusals else ""
        )
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        set_enabled(self.migrate_button, not self.busy and not self.plan.refusals)

    def migrate(self) -> None:
        from enginesave.migrate import execute_migration, plan_migration

        repo, stop = self.repo, self.app.stop_requested

        def job(progress: Callable[[Any], None]) -> tuple[MigrationResult, MigrationPlan]:
            # M45 review: planned again right before (a cancelled or failed run renamed some)
            result = execute_migration(
                repo,
                plan_migration(repo),
                progress=lambda done, total, name: progress((done, total, name)),
                should_stop=stop,
            )
            return result, plan_migration(repo)

        self.run(
            _("迁移备份库"),
            job,
            self.migrated,
            status=_("正在迁移备份库…"),
            progress=self.progress,
            cancellable=True,
            lock=repo.root,  # D73 4
        )

    def progress(self, value: tuple[int, int, str]) -> None:
        """The record being migrated, in the status bar."""
        done, total, name = value
        self.app.progress.configure(maximum=max(total, 1), value=done)
        self.app.status_var.set(
            _("正在迁移 {name}（{v}/{total}）…", name=name, v=done + 1, total=total)
        )

    def migrated(self, value: tuple[MigrationResult, MigrationPlan]) -> None:
        result, plan = value
        text = state.migration_done_text(result)
        log.info(text)
        if result.failed or result.cancelled or plan.needed:
            self.result.set(text)
            self.planned(plan)  # what is left, and whether it can go on now
            self.app.library.reload()
            return
        self.close()
        self.app.library.reload()
