"""Session panel widget: header + scrollable event log + action bar."""

from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog, Static

from ..session import Session, StateRunning, StateBlocked, StateAwaitingReview, StateDone, StateFailed

ORC_SESSION_ID = "__orc__"

_KIND_STYLE = {
    "text": "",
    "tool_use": "dim italic",
    "tool_result": "dim italic",
    "thinking": "dim",
    "user": "bold cyan",
    "system": "dim yellow",
    "result": "dim green",
}


def _format_entry(kind: str, text: str) -> str:
    style = _KIND_STYLE.get(kind, "")
    if style:
        return f"[{style}]{text}[/{style}]"
    return text


class _PanelBase(Widget):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._log_entries: list[tuple[str, str]] = []
        self._context_tokens: Optional[int] = None
        self._model: str = "sonnet"

    def compose(self) -> ComposeResult:
        yield Static("", id="panel-header", classes="panel-header")
        yield RichLog(id="event-log", classes="event-log", highlight=False, markup=True)

    def on_mount(self) -> None:
        self._refresh_header()

    def _refresh_header(self) -> None:
        header = self.query_one("#panel-header", Static)
        header.update(self._header_text())

    def _header_text(self) -> str:
        return ""

    def update_tokens(self, tokens: int) -> None:
        self._context_tokens = tokens
        self._refresh_header()

    def append_log(self, kind: str, text: str) -> None:
        self._log_entries.append((kind, text))
        log = self.query_one("#event-log", RichLog)
        log.write(_format_entry(kind, text))

    def refresh_log(self, entries: list[tuple[str, str]]) -> None:
        log = self.query_one("#event-log", RichLog)
        log.clear()
        for kind, text in entries:
            log.write(_format_entry(kind, text))

    def scroll_down_log(self) -> None:
        log = self.query_one("#event-log", RichLog)
        log.scroll_down()

    def scroll_up_log(self) -> None:
        log = self.query_one("#event-log", RichLog)
        log.scroll_up()

    def scroll_top_log(self) -> None:
        log = self.query_one("#event-log", RichLog)
        log.scroll_home()

    def scroll_bottom_log(self) -> None:
        log = self.query_one("#event-log", RichLog)
        log.scroll_end()

    def _tokens_label(self) -> str:
        if not self._context_tokens:
            return ""
        t = self._context_tokens
        if t >= 1_000_000:
            return f"{t / 1_000_000:.1f}M"
        if t >= 1_000:
            return f"{t // 1_000}k"
        return str(t)


class OrcPanel(_PanelBase):
    def _header_text(self) -> str:
        tok = self._tokens_label()
        ctx = f" · {tok}" if tok else ""
        return f"[bold]orc[/bold] · {self._model}{ctx}  [dim](1)[/dim]"


class SessionPanel(_PanelBase):
    def __init__(self, session: Session, **kwargs):
        super().__init__(**kwargs)
        self.session = session
        self._model = session.model

    def _header_text(self) -> str:
        s = self.session
        state = s.state
        badge = state.badge() if hasattr(state, "badge") else "?"
        tok = self._tokens_label()
        ctx = f" · {tok}" if tok else ""
        name = s.name[:20]
        return f"[bold]{name}[/bold] {badge} · {s.model}{ctx}"

    def update_session(self, session: Session) -> None:
        self.session = session
        self._refresh_header()
