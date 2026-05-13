"""Tab strip widget."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from ..session import Session, StateRunning, StateBlocked, StateAwaitingReview, StateDone, StateFailed

ORC_SESSION_ID = "__orc__"


def _badge(session: Session) -> str:
    s = session.state
    if isinstance(s, StateRunning):
        return "◐"
    elif isinstance(s, StateBlocked):
        return "?" if s.kind.value == "Permission" else "!"
    elif isinstance(s, StateAwaitingReview):
        return "◑"
    elif isinstance(s, StateDone):
        return "✓"
    elif isinstance(s, StateFailed):
        return "✗"
    return " "


class TabStrip(Widget):
    DEFAULT_CSS = """
    TabStrip {
        height: 1;
        background: $panel;
        layout: horizontal;
        overflow: hidden;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._sessions: list[Session] = []
        self._active: int = 0
        self._tab_ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static(self._render_tabs(), id="tab-content")

    def refresh_tabs(
        self,
        sessions: list[Session],
        active_index: int,
        tab_ids: list[str],
    ) -> None:
        self._sessions = sessions
        self._active = active_index
        self._tab_ids = tab_ids
        content = self.query_one("#tab-content", Static)
        content.update(self._render_tabs())

    def set_active(self, index: int) -> None:
        self._active = index
        content = self.query_one("#tab-content", Static)
        content.update(self._render_tabs())

    def _render_tabs(self) -> str:
        parts = []
        sessions_by_id = {s.id: s for s in self._sessions}

        for i, tid in enumerate(self._tab_ids):
            is_active = i == self._active
            num = str(i + 1)

            if tid == ORC_SESSION_ID:
                label = "orc"
                badge = "◐"
            else:
                s = sessions_by_id.get(tid)
                if s:
                    label = s.name[:10]
                    badge = _badge(s)
                else:
                    label = tid[:8]
                    badge = " "

            tab = f" {num}:{label} {badge} "
            if is_active:
                parts.append(f"[bold reverse]{tab}[/bold reverse]")
            else:
                parts.append(tab)

        return "".join(parts)
