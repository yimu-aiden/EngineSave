"""The window's scan and backup actions (「扫描」, 「备份已勾选项」, 「预览…」), split out of
``gui/app.py`` in 0.6.0 (D38). ``App`` mixes this class in; the methods use the window's
settings, task runner, views and log like any other ``App`` method.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from enginesave.errors import EngineSaveError, Stopped
from enginesave.gui import state
from enginesave.gui.i18n import _
from enginesave.models import DetectedGame, GameStatus, ScanReport
from enginesave.repo import Repo, destination_state

if TYPE_CHECKING:
    from enginesave.backup import BackupPlan, BackupResult

    # the scan's games compared again with their snapshots, and the records just written
    Compared = tuple[dict[str, state.Change], dict[str, str]]
    Scanned = tuple[ScanReport, dict[str, state.Change]]
    BackedUp = tuple[Path, BackupResult, Compared | None]

log = logging.getLogger("enginesave.gui")


def changes_after(
    report: ScanReport | None,
    plan: BackupPlan,
    repo: Path,
    result: BackupResult,
    include_candidates: bool,
) -> Compared | None:
    """After a backup (on the worker thread): the scan's games compared with their latest
    snapshots again, the ones just backed up by the records they went into (D54); those
    records are handed back too, for the scan's games to remember. The backup is done by
    then: a comparison that fails only leaves the scan page as it was."""
    if report is None:
        return None
    try:
        ids = state.backed_up_ids(plan, result)
        changes = state.save_changes(
            report, repo, include_candidates=include_candidates, game_ids=ids
        )
    except Exception:  # noqa: BLE001 - never turn a finished backup into a failed one
        log.exception(_("备份后的存档比对失败（备份本身已完成）"))
        return None
    return changes, ids


class ScanAndBackup:
    """Scan the roots, back up what is ticked (or preview it first), show the result."""

    # -------------------------------------------------------------------------------- scan

    def scan(self) -> None:
        roots = [Path(r) for r in self.settings.scan_roots]
        if not roots:
            self.info(_("还没有扫描根目录：先点“添加…”选择存放游戏的文件夹。"))
            return
        from enginesave.scan import ScanOptions, run_scan

        options = ScanOptions(
            roots=roots,
            saves_root_override=self.saves_override(),
            env=self.env,
            repo=self._scan_repo(),
            excludes=list(self.settings.scan_excludes),  # D63: 「扫描时跳过的文件夹」
            should_stop=self.stop_requested,  # D58: 「取消」, between folders
        )
        cache_file = self.cache_file
        include_candidates = self.settings.include_candidates

        def job(_progress: Callable[[Any], None]) -> Scanned | None:
            from enginesave.scan_cache import ScanCache

            try:
                report = run_scan(dataclasses.replace(options, cache=ScanCache.load(cache_file)))
                # D54 3: each backed-up game against its latest snapshot, here on the worker;
                # 「取消」 between games too (D61 3)
                changes = state.save_changes(
                    report,
                    options.repo,
                    include_candidates=include_candidates,
                    should_stop=options.should_stop,
                )
            except Stopped:
                return None  # the last scan's results stay as they were
            return report, changes

        log.info(_("开始扫描（只读）：%s"), "; ".join(str(r) for r in roots))
        self.run(_("扫描"), job, self.scanned, status=_("正在扫描（只读）…"), cancellable=True)

    def _scan_repo(self) -> Path | None:
        """The library, when it exists, so known games are recognised (read only)."""
        path = self.repo_path()
        if path is None:
            return None
        try:
            return path if destination_state(path) == "existing" else None
        except EngineSaveError:
            return None

    def scanned(self, value: Scanned | None) -> None:
        if value is None:  # D58
            log.info(_("已取消扫描：扫描页仍是上次扫描的结果"))
            self.status_var.set(_("已取消扫描"))
            return
        report, changes = value
        self.report, self.changes = report, changes
        self.library_stale = False
        self.selection.load(report, state.unchanged_keys(changes))  # D54 4: the first group
        self.scan_view.load(report)
        self.last_scan = time.strftime("%Y-%m-%d %H:%M:%S")
        self.refresh_status()
        log.info(_("扫描完成：%s"), state.scan_summary(report))
        if report.games:
            log.info(state.groups_log(report.games, changes))
        took = state.scan_time_text(report.timing)
        if took:
            log.info(took)
        for slow in report.timing.get("slowest", []):
            log.info(_("慢目录：%.1f 秒  %s"), slow["seconds"], slow["path"])
        for warning in report.warnings:
            log.warning(_("扫描警告：%s"), warning)
        skipped = state.skip_summary(report.skipped)  # one line, not one per folder (D34)
        if skipped:
            log.info(_("%s（全部列表见“查看跳过的目录”）"), skipped)
        self.status_var.set(_("；").join(t for t in (_("扫描完成"), took, skipped) if t))
        if not report.games and not report.unlinked:
            self.info(state.scan_summary(report))

    # ------------------------------------------------------------------------------ backup

    def backup_inputs(self) -> tuple[list[Path], Path, dict[str, Any]] | None:
        """Scan roots, library and ``prepare_backup`` options, or ``None`` after saying why:
        the ticked games and the ticked orphaned folders (D42)."""
        if self.report is None:
            self.info(_("请先扫描。"))
            return None
        repo = self.repo_path()
        if repo is None:
            self.info(_("请先在上方选择或新建备份库。"))
            return None
        if not self.writable_library():  # D67 8: a library of format 1 is migrated first
            return None
        options = state.backup_options(self.selection, self.report, self.settings)
        if not options["select_roots"] and not options["include_unlinked"]:
            self.info(_("没有勾选任何游戏或孤立存档目录。"))
            return None
        return [Path(r) for r in self.settings.scan_roots], repo, options

    def backup_checked(self) -> None:
        """「备份已勾选项」 (D32), on the scan page and in the orphaned-folders dialog: one click
        plans the ticked items with a read-only rescan, writes what will be copied to the log
        panel and backs it up; no preview dialog."""
        inputs = self.backup_inputs()
        if inputs is None:
            return
        roots, repo, options = inputs
        override, env = self.saves_override(), self.env
        cache_file = self.cache_file
        report, include_candidates = self.report, self.settings.include_candidates
        stop = self.stop_requested  # D58: 「取消」 while rescanning or between files

        def job(
            progress: Callable[[Any], None],
        ) -> tuple[BackupPlan | None, BackedUp | None]:
            from enginesave.backup import execute_backup, prepare_backup
            from enginesave.scan_cache import ScanCache

            cache = ScanCache.load(cache_file)
            try:
                plan = prepare_backup(
                    roots,
                    repo,
                    saves_root_override=override,
                    env=env,
                    cache=cache,
                    should_stop=stop,
                    **options,
                )
            except Stopped:
                return None, None
            if not plan.games:
                return plan, None
            for line in state.backup_plan_lines(plan):
                log.info(line)
            progress(("plan", state.plan_totals(plan)))
            store = Repo.open_or_create(plan.dest)

            def step(done: int, total: int, name: str) -> None:
                progress(("step", (done, total, name)))

            result = execute_backup(plan, store, progress=step, should_stop=stop)
            changes = changes_after(report, plan, store.root, result, include_candidates)
            return plan, (store.root, result, changes)

        log.info(_("备份已勾选项：先只读地重新扫描所勾选的项目，再开始备份。"))
        self.run(
            _("备份"),
            job,
            self.checked_backed_up,
            progress=self.one_click_progress,
            status=_("正在准备备份（只读扫描）…"),
            determinate=True,
            cancellable=True,
        )

    def one_click_progress(self, value: tuple[str, Any]) -> None:
        kind, payload = value
        if kind == "plan":
            self.backup_totals = payload
            games, files, size = payload
            self.status_var.set(
                _(
                    "开始备份：{games} 项，{files} 个文件，{v}…",
                    games=games,
                    files=files,
                    v=state.size_text(size),
                )
            )
            return
        done, total, _name = payload
        self.progress.configure(maximum=max(total, 1), value=done)
        self.status_var.set(state.backup_status(self.backup_totals, payload))

    def checked_backed_up(self, value: tuple[BackupPlan | None, BackedUp | None]) -> None:
        plan, outcome = value
        if plan is None:  # D58: stopped while rescanning
            log.info(_("已取消备份：还在扫描，什么都没有备份"))
            self.status_var.set(_("已取消备份"))
            return
        if outcome is None:
            reasons = "\n".join(_("{v}：{v2}", v=s["name"], v2=s["reason"]) for s in plan.skipped)
            log.info(_("没有可以备份的内容：跳过 %d 项"), len(plan.skipped))
            self.info(_("没有可以备份的内容。\n\n{reasons}", reasons=reasons).strip())
            return
        self.backed_up(outcome, len(plan.games))

    def preview_backup(self) -> None:
        """「预览…」: the same plan shown in a dialog first, for whoever wants to look."""
        inputs = self.backup_inputs()
        if inputs is None:
            return
        roots, repo, options = inputs
        override, env = self.saves_override(), self.env
        cache_file = self.cache_file
        stop = self.stop_requested  # D58: 「取消」 while rescanning

        def job(_progress: Callable[[Any], None]) -> BackupPlan | None:
            from enginesave.backup import prepare_backup
            from enginesave.scan_cache import ScanCache

            cache = ScanCache.load(cache_file)
            try:
                return prepare_backup(
                    roots,
                    repo,
                    saves_root_override=override,
                    env=env,
                    cache=cache,
                    should_stop=stop,
                    **options,
                )
            except Stopped:
                return None

        self.run(
            _("预览备份"),
            job,
            self.show_preview,
            status=_("正在重新扫描并生成备份计划（只读）…"),
            cancellable=True,
        )

    def show_preview(self, plan: BackupPlan | None) -> None:
        from enginesave.gui.views.dialogs import BackupPreviewDialog

        if plan is None:  # D58: stopped while rescanning
            log.info(_("已取消预览备份：还在扫描"))
            self.status_var.set(_("已取消预览备份"))
            return
        log.info(_("备份预览：%d 项，跳过 %d 项"), len(plan.games), len(plan.skipped))
        if not plan.games:
            reasons = "\n".join(_("{v}：{v2}", v=s["name"], v2=s["reason"]) for s in plan.skipped)
            self.info(_("没有可以备份的内容。\n\n{reasons}", reasons=reasons).strip())
            return
        BackupPreviewDialog(self, plan, self.run_backup)

    def run_backup(self, plan: BackupPlan) -> None:
        report, include_candidates = self.report, self.settings.include_candidates
        stop = self.stop_requested  # D58: 「取消」, between files

        def job(progress: Callable[[Any], None]) -> BackedUp:
            from enginesave.backup import execute_backup

            repo = Repo.open_or_create(plan.dest)
            result = execute_backup(
                plan,
                repo,
                progress=lambda done, total, name: progress((done, total, name)),
                should_stop=stop,
            )
            return (
                repo.root,
                result,
                changes_after(report, plan, repo.root, result, include_candidates),
            )

        log.info(_("开始备份到 %s：%d 项"), plan.dest, len(plan.games))
        self.run(
            _("备份"),
            job,
            lambda value: self.backed_up(value, len(plan.games)),
            progress=self.backup_progress,
            status=_("正在备份…"),
            determinate=True,
            cancellable=True,
        )

    def backup_progress(self, value: tuple[int, int, str]) -> None:
        done, total, name = value
        self.progress.configure(maximum=max(total, 1), value=done)
        self.status_var.set(
            _("正在备份 {name}（{v}/{total}）…", name=name, v=done + 1, total=total)
        )

    def backed_up(self, value: BackedUp, planned: int | None = None) -> None:
        from enginesave.gui.views.dialogs import BackupResultDialog

        repo_root, result, compared = value
        if result.cancelled:  # D58: the ones done are whole, the one being copied left nothing
            log.info(state.cancelled_backup_text(len(result.results), planned))
        if compared is not None and self.report is not None:
            # D54: what was just backed up has no new saves now; ticks are only added, for
            # games that gained new saves since the scan. The scan's games remember the
            # records they went into, for the next comparison.
            changes, ids = compared
            self.retick(state.newly_changed(self.changes, changes))
            self.untick_done(result, ids)
            self.changes = changes
            for game in self.report.games:
                game.known_game_id = ids.get(state.game_key(game), game.known_game_id)
            self.scan_view.load(self.report)
        counts = state.result_counts(result)
        if not result.cancelled:  # a cancelled one has its line above
            log.info(
                _("备份完成：成功 %d，部分完成 %d，失败 %d，跳过 %d"),
                counts["ok"],
                counts["partial"],
                counts["failed"],
                counts["skipped"],
            )
        for r in result.results:
            level = logging.INFO if r.status == "ok" else logging.WARNING
            log.log(
                level,
                _("%s %s：快照 %s，%d 个文件，%s，storage %s %s"),
                r.status.upper(),
                r.display_name,
                r.snapshot_id,
                r.file_count,
                state.size_text(r.total_bytes),
                r.storage,
                r.message,
            )
        if hint := state.storage_hint(result):
            log.warning(hint)
        dialog = BackupResultDialog(self, result, repo_root)
        # D64: the automatic mirror starts once the library page has been read again; not when
        # 「取消」 was pressed during the backup, after its last safe point
        stopped = self.stop_requested()
        self.library.reload(then=lambda: self.auto_mirror(dialog, result, repo_root, stopped))

    # --------------------------------------------------------------- automatic mirror (D64)

    def auto_mirror(
        self, dialog: Any, result: BackupResult, repo_root: Path, stopped: bool = False
    ) -> None:
        """After a backup that worked, mirror the library to the folder of its last mirror
        that worked (``mirrors.json``) when the setting says so: in the background,
        cancellable, its line added to the result dialog. A folder that is not there (a drive
        not plugged in) or that the check refuses is only a line in the log and the status
        bar; the folder is looked at on the worker thread (a network drive may hang)."""
        from enginesave.mirror import last_mirror

        settings = self.settings
        if not settings.auto_mirror:
            return
        if not state.backup_succeeded(result):  # D66 9: said, in the log
            log.info(state.auto_mirror_skipped(state.not_mirrored_reason(result)))
            return
        last = last_mirror(self.settings_file.parent, repo_root)
        if last is None:  # never mirrored with success: nothing to follow
            log.info(state.auto_mirror_skipped(_("这个备份库还没有成功镜像过")))
            return
        if stopped:
            self.auto_mirror_skipped(_("备份时按了“取消”"))
            return
        if not dialog.winfo_exists():  # M44 review: closed (or 「清理…」) before it started
            self.auto_mirror_skipped(_("结果窗口已关闭"))
            return
        target = Path(last["to"])
        roots = [Path(r) for r in settings.scan_roots]
        override, env, verify = self.saves_override(), self.env, settings.mirror_verify
        stop = self.stop_requested

        def job(progress: Callable[[Any], None]) -> Any:
            from enginesave.errors import UsageError
            from enginesave.mirror import forbidden_places, mirror_repo

            if not target.is_dir():  # never created here: a drive not plugged in, say
                raise UsageError(_("镜像文件夹不在（{target}）", target=target))
            repo = Repo.open(repo_root)
            places = forbidden_places(repo, saves_root=override, env=env, also=roots)
            return mirror_repo(
                repo,
                target,
                forbidden=places,
                verify=verify,
                progress=lambda done, total, name: progress((done, total, name)),
                should_stop=stop,
            )

        def failed(exc: BaseException) -> None:
            if not isinstance(exc, EngineSaveError):
                log.error(_("自动镜像出错"), exc_info=exc)
            self.auto_mirror_skipped(str(exc))
            if dialog.winfo_exists():
                dialog.mirrored(state.auto_mirror_skipped(str(exc)))

        log.info(_("自动镜像：%s → %s"), repo_root, target)
        started = self.run(
            _("自动镜像"),
            job,
            lambda mirrored: self.auto_mirrored(mirrored, dialog, repo_root),
            on_error=failed,
            progress=self.mirror_progress,
            status=_("正在自动镜像到 {target}…", target=target),
            determinate=True,
            cancellable=True,
        )
        if started and dialog.winfo_exists():
            dialog.mirroring(target)

    def auto_mirror_skipped(self, reason: str) -> None:
        text = state.auto_mirror_skipped(reason)
        log.info(text)
        self.status_var.set(text)

    def auto_mirrored(self, result: Any, dialog: Any, repo_root: Path) -> None:
        from enginesave.mirror import record_mirror

        text = state.auto_mirror_text(result)
        log.log(logging.INFO if result.exit_code == 0 else logging.WARNING, text)
        for item in [*result.failed, *result.unreadable]:
            log.warning(_("自动镜像：%s"), item)
        self.status_var.set(text)
        if result.exit_code == 0 and not result.cancelled:  # 「上次镜像」, as by hand
            record_mirror(self.settings_file.parent, repo_root, result.target)
            self.library.reload()
        if dialog.winfo_exists():
            dialog.mirrored(text)

    def library_changed(self, compare: bool = False) -> None:
        """The library was written to: its page is read again. After a removal or an import
        (``compare``) the scan page's games are also compared again with what the library
        holds now (D54), once the page has loaded."""
        self.library.reload(then=self.compare_again if compare else None)

    def compare_again(self) -> None:
        """The scan's games against the library as it is now (read-only, worker thread)."""
        report, repo = self.report, self._scan_repo()
        if report is None:
            return
        include_candidates = self.settings.include_candidates

        def job(_progress: Callable[[Any], None]) -> dict[str, state.Change]:
            return state.save_changes(report, repo, include_candidates=include_candidates)

        self.run(_("比对存档"), job, lambda changes: self.compared_again(report, changes))

    def compared_again(self, report: ScanReport, changes: dict[str, state.Change]) -> None:
        if report is not self.report:  # scanned again meanwhile
            return
        self.retick(state.newly_changed(self.changes, changes))
        self.changes = changes
        self.scan_view.load(report)

    def retick(self, keys: set[str]) -> None:
        """Games that lost their "no new saves" (D54): ticked again, so that 「备份已勾选项」
        does not leave them out. Ticks are only ever added here."""
        ticked = [k for k in keys if k in self.selection.games and k not in self.selection.locked]
        for key in ticked:
            self.selection.games[key] = True
        if ticked:
            log.info(_("%d 款游戏不再是“无新存档”，已重新勾选"), len(ticked))

    def untick_done(self, result: BackupResult, ids: dict[str, str]) -> None:
        """D61 ①: the games this backup stored a complete snapshot for are unticked, their
        ticks are spent (otherwise they would wait, ticked, in the closed 「已备份、无新存档」
        group for the next click); a partial or failed one stays ticked."""
        done = {r.game_id for r in result.results if r.status == "ok" and r.snapshot_id}
        keys = [k for k, gid in ids.items() if gid in done and self.selection.games.get(k)]
        for key in keys:
            self.selection.games[key] = False
        if keys:
            log.info(_("%d 款刚备份完的游戏已取消勾选"), len(keys))

    def library_switched(self) -> None:
        """Another library is in use: the comparison was with the old one, so it is dropped
        and the games it folded away are ticked again; what the scan knew of the old library
        (the records its games are in, the suggested links) goes too (D61 ④), until a new
        scan compares with this one."""
        if self.report is None:
            return
        self.retick(state.newly_changed(self.changes, {}))
        self.changes = {}
        for game in self.report.games:
            game.known_game_id = None
            game.link_suggestions = []
        self.library_stale = True
        self.scan_view.load(self.report)
        log.info(
            _(
                "备份库已更换：扫描页已不再与备份比对，原先“无新存档”的游戏已重新勾选；重新扫描后按这个备份库比对"
            )
        )

    # ---------------------------------------------------------------- confirm / link (D48 3)

    def confirm_link(self, game: DetectedGame) -> Any:
        """「确认 / 关联…」 for a scanned game; it writes into the library, so one must be set."""
        path = self.repo_path()
        if path is None or destination_state(path) != "existing":
            self.info(_("请先在上方选择备份库（确认 / 关联写在备份库的记录里）。"))
            return None
        if self.library_stale:  # D61 4: what the scan knew of a library is gone; scan first
            self.info(
                _(
                    "{LIBRARY_SWITCHED}：确认 / 关联要知道这款游戏在这个备份库里的记录。",
                    LIBRARY_SWITCHED=_(state.LIBRARY_SWITCHED),
                )
            )
            return None
        if not self.writable_library():  # D67 8
            return None
        from enginesave.gui.views.dialogs import ConfirmLinkDialog

        try:
            return ConfirmLinkDialog(self, game, Repo.open(path))
        except EngineSaveError as exc:
            self.show_error(exc)
            return None

    def game_confirmed(
        self, old: DetectedGame, fresh: DetectedGame, change: state.Change | None = None
    ) -> None:
        """After 「确认 / 关联…」: the game's row shows its folder scanned again (read-only),
        ticked when it can be backed up now and has new saves (D54); the library may hold a
        new record."""
        report = self.report
        index = next((i for i, g in enumerate(report.games) if g is old), None) if report else None
        if report is not None and index is not None:
            fresh.shared_global_with = fresh.shared_global_with or list(old.shared_global_with)
            report.games[index] = fresh
            self.changes.pop(state.game_key(old), None)
            if change is not None and fresh.status is GameStatus.CONFIRMED:
                self.changes[state.game_key(fresh)] = change
            unchanged = change is not None and not change.new_saves
            self.selection.replace(old, fresh, unchanged)
            self.scan_view.load(report)
        self.library_changed()
