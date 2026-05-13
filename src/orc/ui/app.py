"""Main Textual application: OrcApp."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from ..session import (
    Session, StateRunning, StateDone, StateFailed,
    StateBlocked, StateAwaitingReview,
    EvFinished, EvErrored, EvRestarted,
)
from ..state import StateManager, StateUpdate, PendingQuestion
from ..worker import (
    WorkerHandle, WorkerEvent,
    EvText, EvToolUse, EvToolResult, EvThinking,
    EvResult, EvClaudeSessionId, EvExited, EvUsage,
    spawn_worker, spawn_worker_resume,
)
from ..orc_brain import OrcProcess, OrcEvent, OrcText, OrcToolUse, OrcResult as OrcResultEv, OrcSystem, OrcThinking, OrcUsage, spawn_orc
from ..backchannel import BackchannelProcess, spawn_backchannel
from ..review import ReviewSession
from ..mcp import write_worker_mcp_config, worker_system_prompt, orc_system_prompt
from ..worktree import create_worktree, remove_worktree, git_root, current_commit, diff_hash, full_diff

from .tabs import TabStrip
from .panel import SessionPanel, OrcPanel
from .modals import ComposeModal, QuestionModal, ConfirmModal, ReviewModal
from .backchannel import BackchannelOverlay


ORC_SESSION_ID = "__orc__"
DEFAULT_ORC_MODEL = "opus"
DEFAULT_WORKER_MODEL = "sonnet"


class OrcApp(App):
    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("q", "quit_confirm", "Quit", show=True),
        Binding("h", "help", "Help", show=True),
        Binding("question_mark", "toggle_backchannel", "Scratch ?", show=True),
        Binding("t,c", "compose_message", "Talk", show=True),
        Binding("tab", "next_tab", "Next tab", show=False),
        Binding("shift+tab", "prev_tab", "Prev tab", show=False),
        Binding("1", "go_tab_1", show=False),
        Binding("2", "go_tab_2", show=False),
        Binding("3", "go_tab_3", show=False),
        Binding("4", "go_tab_4", show=False),
        Binding("5", "go_tab_5", show=False),
        Binding("6", "go_tab_6", show=False),
        Binding("7", "go_tab_7", show=False),
        Binding("8", "go_tab_8", show=False),
        Binding("9", "go_tab_9", show=False),
        Binding("j,down", "scroll_down", show=False),
        Binding("k,up", "scroll_up", show=False),
        Binding("g", "scroll_top", show=False),
        Binding("G,end", "scroll_bottom", show=False),
        Binding("r", "review", "Review", show=True),
        Binding("R", "restart_worker", "Restart", show=True),
        Binding("K", "kill_worker", "Kill", show=True),
        Binding("n", "scratch_claude", "Scratch n", show=True),
        Binding("ctrl+c", "interrupt", "Interrupt", show=False),
    ]

    def __init__(
        self,
        project_dir: Path,
        state_manager: StateManager,
        orc_process: Optional[OrcProcess],
        orc_events_q: asyncio.Queue,
        worker_handles: dict[str, WorkerHandle],
        worker_events_q: asyncio.Queue,
        worktrees_dir: Path,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.project_dir = project_dir
        self.state_manager = state_manager
        self.orc_process = orc_process
        self.orc_events_q = orc_events_q
        self.worker_handles: dict[str, WorkerHandle] = worker_handles
        self.worker_events_q = worker_events_q
        self.worktrees_dir = worktrees_dir

        # In-memory event logs per session_id
        self.event_logs: dict[str, list[tuple[str, str]]] = {ORC_SESSION_ID: []}

        # Active review sessions per worker session_id
        self.reviews: dict[str, ReviewSession] = {}

        # Backchannel overlay state
        self.backchannel: Optional[BackchannelProcess] = None
        self.backchannel_q: asyncio.Queue = asyncio.Queue(maxsize=256)

        # Tab tracking
        self._active_tab: int = 0  # 0 = orc tab
        self._tab_ids: list[str] = [ORC_SESSION_ID]  # session_ids in order

        # State subscription
        self._state_q: asyncio.Queue = self.state_manager.subscribe()

        self._project_name = project_dir.name

    # -----------------------------------------------------------------------
    # Compose
    # -----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield TabStrip(id="tab-strip")
        yield OrcPanel(id=f"panel-{ORC_SESSION_ID}")
        yield Footer()

    async def on_mount(self) -> None:
        self._start_event_loops()
        await self._restore_event_logs()

    # -----------------------------------------------------------------------
    # Background tasks
    # -----------------------------------------------------------------------

    def _start_event_loops(self) -> None:
        self._orc_event_loop()
        self._worker_event_loop()
        self._state_loop()

    @work(exclusive=False)
    async def _orc_event_loop(self) -> None:
        while True:
            ev = await self.orc_events_q.get()
            if ev is None:
                self._log_orc("system", "[orc exited]")
                break
            await self._handle_orc_event(ev)

    @work(exclusive=False)
    async def _worker_event_loop(self) -> None:
        while True:
            ev = await self.worker_events_q.get()
            await self._handle_worker_event(ev)

    @work(exclusive=False)
    async def _state_loop(self) -> None:
        while True:
            update: StateUpdate = await self._state_q.get()
            await self._on_state_update(update)

    # -----------------------------------------------------------------------
    # Event handlers
    # -----------------------------------------------------------------------

    async def _handle_orc_event(self, ev: OrcEvent) -> None:
        if isinstance(ev, OrcText):
            self._log_orc("text", ev.text)
        elif isinstance(ev, OrcToolUse):
            self._log_orc("tool_use", f"→ {ev.name}({_fmt_input(ev.input)})")
        elif isinstance(ev, OrcThinking):
            self._log_orc("thinking", f"[thinking] {ev.text[:120]}")
        elif isinstance(ev, OrcResultEv):
            cost = f" ${ev.cost_usd:.4f}" if ev.cost_usd else ""
            self._log_orc("result", f"[turn done{cost}]")
        elif isinstance(ev, OrcSystem):
            if ev.model:
                self._log_orc("system", f"[model: {ev.model}]")
        elif isinstance(ev, OrcUsage):
            panel = self.query_one(f"#panel-{ORC_SESSION_ID}", OrcPanel)
            panel.update_tokens(ev.context_tokens)
        self._refresh_panel(ORC_SESSION_ID)

    async def _handle_worker_event(self, ev) -> None:
        sid = ev.session_id
        if isinstance(ev, EvText):
            self._log_worker(sid, "text", ev.text)
        elif isinstance(ev, EvToolUse):
            self._log_worker(sid, "tool_use", f"→ {ev.name}({_fmt_input(ev.input)})")
        elif isinstance(ev, EvToolResult):
            prefix = "← error: " if ev.is_error else "← "
            self._log_worker(sid, "tool_result", prefix + ev.text[:200])
        elif isinstance(ev, EvThinking):
            self._log_worker(sid, "thinking", f"[thinking] {ev.text[:120]}")
        elif isinstance(ev, EvClaudeSessionId):
            await self.state_manager.set_claude_session_id(sid, ev.claude_session_id)
        elif isinstance(ev, EvResult):
            cost = f" ${ev.cost_usd:.4f}" if ev.cost_usd else ""
            self._log_worker(sid, "result", f"[turn done{cost}]")
        elif isinstance(ev, EvUsage):
            panel = self.query(f"#panel-{sid}").first(SessionPanel)
            if panel:
                panel.update_tokens(ev.context_tokens)
        elif isinstance(ev, EvExited):
            code = ev.code if ev.code is not None else "?"
            self._log_worker(sid, "system", f"[worker exited code {code}]")
            if ev.code == 0:
                await self.state_manager.apply_event(
                    sid, EvFinished(summary="Worker completed.")
                )
            else:
                await self.state_manager.apply_event(
                    sid, EvErrored(reason=f"exited {ev.code}")
                )
        self._refresh_panel(sid)

    async def _on_state_update(self, update: StateUpdate) -> None:
        sessions = {s.id: s for s in update.sessions}

        # Add tabs for new sessions
        for sid, s in sessions.items():
            if sid not in self._tab_ids:
                self._tab_ids.append(sid)
                await self._add_panel(s)

        # Refresh tab strip
        tab_strip = self.query_one("#tab-strip", TabStrip)
        tab_strip.refresh_tabs(update.sessions, self._active_tab, self._tab_ids)

        # Handle pending questions
        for pq in update.pending_questions:
            await self._show_question(pq)

    # -----------------------------------------------------------------------
    # Panel management
    # -----------------------------------------------------------------------

    async def _add_panel(self, session: Session) -> None:
        panel = SessionPanel(session=session, id=f"panel-{session.id}")
        panel.display = False
        await self.mount(panel, before=self.query_one(Footer))
        logs = await self.state_manager.load_events(session.id)
        for kind, payload in logs:
            panel.append_log(kind, payload)

    async def _restore_event_logs(self) -> None:
        sessions = await self.state_manager.list_sessions()
        for s in sessions:
            if s.id not in self._tab_ids:
                self._tab_ids.append(s.id)
                await self._add_panel(s)

    def _refresh_panel(self, session_id: str) -> None:
        panels = self.query(f"#panel-{session_id}")
        if panels:
            panel = panels.first()
            if panel:
                log = self.event_logs.get(session_id, [])
                panel.refresh_log(log)

    # -----------------------------------------------------------------------
    # Logging helpers
    # -----------------------------------------------------------------------

    def _log_orc(self, kind: str, text: str) -> None:
        self.event_logs[ORC_SESSION_ID].append((kind, text))
        panel = self.query_one(f"#panel-{ORC_SESSION_ID}", OrcPanel)
        panel.append_log(kind, text)

    def _log_worker(self, session_id: str, kind: str, text: str) -> None:
        if session_id not in self.event_logs:
            self.event_logs[session_id] = []
        self.event_logs[session_id].append((kind, text))
        asyncio.create_task(
            self.state_manager.append_event(session_id, kind, text)
        )
        panels = self.query(f"#panel-{session_id}")
        if panels:
            panel = panels.first(SessionPanel)
            if panel:
                panel.append_log(kind, text)

    # -----------------------------------------------------------------------
    # Tab navigation
    # -----------------------------------------------------------------------

    def _show_tab(self, index: int) -> None:
        if not self._tab_ids:
            return
        index = max(0, min(index, len(self._tab_ids) - 1))
        self._active_tab = index

        for i, tid in enumerate(self._tab_ids):
            selector = f"#panel-{tid}"
            panels = self.query(selector)
            if panels:
                panels.first().display = (i == index)

        tab_strip = self.query_one("#tab-strip", TabStrip)
        tab_strip.set_active(index)

    def action_next_tab(self) -> None:
        n = len(self._tab_ids)
        self._show_tab((self._active_tab + 1) % n)

    def action_prev_tab(self) -> None:
        n = len(self._tab_ids)
        self._show_tab((self._active_tab - 1) % n)

    def action_go_tab_1(self) -> None:
        self._show_tab(0)

    def action_go_tab_2(self) -> None:
        self._show_tab(1)

    def action_go_tab_3(self) -> None:
        self._show_tab(2)

    def action_go_tab_4(self) -> None:
        self._show_tab(3)

    def action_go_tab_5(self) -> None:
        self._show_tab(4)

    def action_go_tab_6(self) -> None:
        self._show_tab(5)

    def action_go_tab_7(self) -> None:
        self._show_tab(6)

    def action_go_tab_8(self) -> None:
        self._show_tab(7)

    def action_go_tab_9(self) -> None:
        self._show_tab(8)

    # -----------------------------------------------------------------------
    # Scrolling
    # -----------------------------------------------------------------------

    def _active_panel(self):
        if not self._tab_ids:
            return None
        tid = self._tab_ids[self._active_tab]
        panels = self.query(f"#panel-{tid}")
        return panels.first() if panels else None

    def action_scroll_down(self) -> None:
        p = self._active_panel()
        if p:
            p.scroll_down_log()

    def action_scroll_up(self) -> None:
        p = self._active_panel()
        if p:
            p.scroll_up_log()

    def action_scroll_top(self) -> None:
        p = self._active_panel()
        if p:
            p.scroll_top_log()

    def action_scroll_bottom(self) -> None:
        p = self._active_panel()
        if p:
            p.scroll_bottom_log()

    # -----------------------------------------------------------------------
    # Talk / compose
    # -----------------------------------------------------------------------

    async def action_compose_message(self) -> None:
        tid = self._tab_ids[self._active_tab] if self._tab_ids else ORC_SESSION_ID
        label = "orc" if tid == ORC_SESSION_ID else f"worker {tid[:8]}"
        result = await self.push_screen_wait(
            ComposeModal(label=label)
        )
        if result:
            await self._send_message(tid, result)

    async def _send_message(self, session_id: str, text: str) -> None:
        if session_id == ORC_SESSION_ID:
            if self.orc_process:
                await self.orc_process.send(text)
                self._log_orc("user", f"[you] {text}")
        else:
            handle = self.worker_handles.get(session_id)
            if handle:
                await handle.send(text)
                self._log_worker(session_id, "user", f"[you] {text}")

    # -----------------------------------------------------------------------
    # Questions
    # -----------------------------------------------------------------------

    async def _show_question(self, pq: PendingQuestion) -> None:
        result = await self.push_screen_wait(
            QuestionModal(question=pq.question, context=pq.context),
        )
        answer = result or ""
        await self.state_manager.answer_question(pq.id, answer, answered_by="user")

    # -----------------------------------------------------------------------
    # Interrupt
    # -----------------------------------------------------------------------

    async def action_interrupt(self) -> None:
        tid = self._tab_ids[self._active_tab] if self._tab_ids else ORC_SESSION_ID
        if tid == ORC_SESSION_ID:
            if self.orc_process:
                await self.orc_process.interrupt()
                self._log_orc("system", "[interrupted]")
        else:
            handle = self.worker_handles.get(tid)
            if handle:
                await handle.interrupt()
                self._log_worker(tid, "system", "[interrupted]")

    # -----------------------------------------------------------------------
    # Kill worker
    # -----------------------------------------------------------------------

    async def action_kill_worker(self) -> None:
        tid = self._tab_ids[self._active_tab] if self._tab_ids else None
        if not tid or tid == ORC_SESSION_ID:
            return
        confirmed = await self.push_screen_wait(
            ConfirmModal(message=f"Kill worker {tid[:8]}?"),
        )
        if not confirmed:
            return
        handle = self.worker_handles.pop(tid, None)
        if handle:
            await handle.kill()
        session = await self.state_manager.get_session(tid)
        if session:
            await remove_worktree(self.project_dir, Path(session.worktree_path))
        await self.state_manager.remove_session(tid)
        if tid in self._tab_ids:
            idx = self._tab_ids.index(tid)
            self._tab_ids.remove(tid)
            panels = self.query(f"#panel-{tid}")
            if panels:
                await panels.first().remove()
            self._show_tab(max(0, idx - 1))

    # -----------------------------------------------------------------------
    # Restart worker
    # -----------------------------------------------------------------------

    async def action_restart_worker(self) -> None:
        tid = self._tab_ids[self._active_tab] if self._tab_ids else None
        if not tid or tid == ORC_SESSION_ID:
            return
        session = await self.state_manager.get_session(tid)
        if not session:
            return
        old_handle = self.worker_handles.pop(tid, None)
        if old_handle:
            await old_handle.kill()

        self._log_worker(tid, "system", "[restarting…]")
        await self.state_manager.apply_event(tid, EvRestarted())

        mcp_cfg = write_worker_mcp_config(tid, Path(session.worktree_path))
        prompt = worker_system_prompt(tid, self._project_name)
        if session.claude_session_id:
            handle = await spawn_worker_resume(
                tid,
                Path(session.worktree_path),
                session.model,
                mcp_cfg,
                prompt,
                session.claude_session_id,
                self.worker_events_q,
            )
        else:
            handle = await spawn_worker(
                tid,
                Path(session.worktree_path),
                session.model,
                mcp_cfg,
                prompt,
                session.task,
                self.worker_events_q,
            )
        self.worker_handles[tid] = handle
        self._log_worker(tid, "system", "[restarted]")

    # -----------------------------------------------------------------------
    # Review
    # -----------------------------------------------------------------------

    async def action_review(self) -> None:
        tid = self._tab_ids[self._active_tab] if self._tab_ids else None
        if not tid or tid == ORC_SESSION_ID:
            return
        session = await self.state_manager.get_session(tid)
        if not session:
            return

        if tid not in self.reviews:
            d = await full_diff(Path(session.worktree_path), session.base_commit)
            h = diff_hash(d)
            self.reviews[tid] = ReviewSession(
                session_id=tid,
                worktree=Path(session.worktree_path),
                base_commit=session.base_commit,
                diff_hash=h,
            )
        rev = self.reviews[tid]

        result = await self.push_screen_wait(
            ReviewModal(review=rev)
        )
        if result:
            verdict, feedback = result
            if verdict == "approve":
                rev.approve(overall=feedback)
            else:
                rev.request_changes(overall=feedback)
            payload = rev.submit_payload()
            handle = self.worker_handles.get(tid)
            if handle:
                await handle.send(f"[review feedback]\n{payload}")
                self._log_worker(tid, "system", f"[review submitted: {'approved' if verdict == 'approve' else 'changes requested'}]")
            del self.reviews[tid]

    # -----------------------------------------------------------------------
    # Scratch claude (n) — fullscreen handoff
    # -----------------------------------------------------------------------

    async def action_scratch_claude(self) -> None:
        from ..worker import claude_bin
        with self.suspend():
            subprocess.run([claude_bin()], cwd=str(self.project_dir))

    # -----------------------------------------------------------------------
    # Backchannel overlay (?)
    # -----------------------------------------------------------------------

    async def action_toggle_backchannel(self) -> None:
        existing = self.query(BackchannelOverlay)
        if existing:
            existing.first().remove()
            return

        if self.backchannel is None:
            try:
                self.backchannel = await spawn_backchannel(
                    self.project_dir,
                    model="sonnet",
                    events_q=self.backchannel_q,
                )
            except Exception as e:
                self.notify(f"Could not start backchannel: {e}", severity="error")
                return
            self._backchannel_event_loop()

        overlay = BackchannelOverlay(
            channel=self.backchannel,
            events_q=self.backchannel_q,
        )
        await self.mount(overlay)

    @work(exclusive=False)
    async def _backchannel_event_loop(self) -> None:
        from ..worker import EvText
        while True:
            ev = await self.backchannel_q.get()
            if ev is None:
                break
            overlay = self.query(BackchannelOverlay)
            if overlay:
                overlay.first().on_event(ev)

    # -----------------------------------------------------------------------
    # Help
    # -----------------------------------------------------------------------

    def action_help(self) -> None:
        from .modals import HelpModal
        self.push_screen(HelpModal())

    # -----------------------------------------------------------------------
    # Quit
    # -----------------------------------------------------------------------

    async def action_quit_confirm(self) -> None:
        confirmed = await self.push_screen_wait(
            ConfirmModal(message="Quit orc?")
        )
        if confirmed:
            await self._cleanup()
            self.exit()

    async def _cleanup(self) -> None:
        if self.orc_process:
            await self.orc_process.kill()
        for handle in self.worker_handles.values():
            await handle.kill()
        if self.backchannel:
            await self.backchannel.kill()
        self.state_manager.unsubscribe(self._state_q)


def _fmt_input(inp) -> str:
    if inp is None:
        return ""
    if isinstance(inp, dict):
        parts = []
        for k, v in list(inp.items())[:3]:
            sv = str(v)[:40]
            parts.append(f"{k}: {sv}")
        return ", ".join(parts)
    return str(inp)[:80]
