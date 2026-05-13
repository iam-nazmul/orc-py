"""Modal screens: compose, question, confirm, review, help."""

from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog, Static, TextArea

from ..review import ReviewSession


# ---------------------------------------------------------------------------
# ComposeModal — type a message to send
# ---------------------------------------------------------------------------

class ComposeModal(ModalScreen[Optional[str]]):
    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("ctrl+s", "submit", show=False),
    ]

    DEFAULT_CSS = """
    ComposeModal {
        align: center middle;
    }
    #compose-box {
        width: 80;
        min-height: 8;
        max-height: 20;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #compose-title { margin-bottom: 1; color: $primary; text-style: bold; }
    #compose-area { height: 6; border: solid $panel; }
    #compose-hint { margin-top: 1; color: $text-muted; }
    """

    def __init__(self, label: str = "orc", **kwargs):
        super().__init__(**kwargs)
        self.label = label

    def compose(self) -> ComposeResult:
        with Static(id="compose-box"):
            yield Label(f"Talk to {self.label}", id="compose-title")
            yield TextArea(id="compose-area")
            yield Label("Enter to send · Esc to cancel", id="compose-hint")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
        elif event.key == "enter" and not event.is_printable:
            self._submit()
        elif event.key == "ctrl+s":
            self._submit()

    def _submit(self) -> None:
        ta = self.query_one("#compose-area", TextArea)
        text = ta.text.strip()
        self.dismiss(text if text else None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        self._submit()


# ---------------------------------------------------------------------------
# QuestionModal — worker asks user a question
# ---------------------------------------------------------------------------

class QuestionModal(ModalScreen[str]):
    BINDINGS = [Binding("escape", "skip", show=False)]

    DEFAULT_CSS = """
    QuestionModal { align: center middle; }
    #question-box {
        width: 80;
        min-height: 10;
        max-height: 24;
        background: $surface;
        border: solid $warning;
        padding: 1 2;
    }
    #q-title { color: $warning; text-style: bold; margin-bottom: 1; }
    #q-body { margin-bottom: 1; }
    #q-context { color: $text-muted; margin-bottom: 1; }
    #q-area { height: 4; border: solid $panel; }
    #q-hint { margin-top: 1; color: $text-muted; }
    """

    def __init__(self, question: str, context: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.question = question
        self.context = context

    def compose(self) -> ComposeResult:
        with Static(id="question-box"):
            yield Label("Question from worker", id="q-title")
            yield Label(self.question, id="q-body")
            if self.context:
                yield Label(f"Context: {self.context}", id="q-context")
            yield TextArea(id="q-area")
            yield Label("Enter to answer · Esc to skip", id="q-hint")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss("")
        elif event.key == "enter" and not event.is_printable:
            ta = self.query_one("#q-area", TextArea)
            self.dismiss(ta.text.strip())

    def action_skip(self) -> None:
        self.dismiss("")


# ---------------------------------------------------------------------------
# ConfirmModal — yes/no confirmation
# ---------------------------------------------------------------------------

class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "yes", show=False),
        Binding("n,escape", "no", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    #confirm-box {
        width: 50;
        height: 7;
        background: $surface;
        border: solid $error;
        padding: 1 2;
        align: center middle;
    }
    #confirm-msg { text-align: center; margin-bottom: 1; }
    #confirm-hint { text-align: center; color: $text-muted; }
    """

    def __init__(self, message: str, **kwargs):
        super().__init__(**kwargs)
        self.message = message

    def compose(self) -> ComposeResult:
        with Static(id="confirm-box"):
            yield Label(self.message, id="confirm-msg")
            yield Label("[y] yes  [n/Esc] no", id="confirm-hint")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# ReviewModal — diff view, file selection, approve/request changes
# ---------------------------------------------------------------------------

class ReviewModal(ModalScreen[Optional[tuple[str, str]]]):
    BINDINGS = [
        Binding("escape,q", "cancel", show=False),
        Binding("a", "approve", "Approve", show=True),
        Binding("r", "request_changes", "Request changes", show=True),
        Binding("j,down", "next_file", show=False),
        Binding("k,up", "prev_file", show=False),
    ]

    DEFAULT_CSS = """
    ReviewModal { align: center middle; }
    #review-box {
        width: 95%;
        height: 85%;
        background: $surface;
        border: solid $success;
        layout: vertical;
    }
    #review-header {
        height: 1;
        background: $panel;
        padding: 0 1;
        color: $success;
        text-style: bold;
    }
    #review-body {
        height: 1fr;
        layout: horizontal;
    }
    #file-list {
        width: 25%;
        height: 1fr;
        border-right: solid $panel;
        overflow-y: scroll;
        padding: 0 1;
    }
    #diff-view {
        width: 75%;
        height: 1fr;
        overflow: scroll;
        padding: 0 1;
    }
    #review-footer {
        height: 2;
        background: $panel;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self, review: ReviewSession, **kwargs):
        super().__init__(**kwargs)
        self.review = review
        self._files: list[str] = []
        self._file_index: int = 0

    def compose(self) -> ComposeResult:
        with Static(id="review-box"):
            yield Static("Review  [a] approve  [r] request changes  [q] cancel", id="review-header")
            with Static(id="review-body"):
                yield RichLog(id="file-list", highlight=False, markup=True)
                yield RichLog(id="diff-view", highlight=False, markup=True)
            yield Static("Loading diff…", id="review-footer")

    async def on_mount(self) -> None:
        self._files = await self.review.get_changed_files()
        diff = await self.review.get_diff()
        self._render_files()
        self._render_diff(diff)
        footer = self.query_one("#review-footer", Static)
        stat = await self.review.get_diff_stat()
        footer.update(stat[:200] if stat else "No changes")

    def _render_files(self) -> None:
        fl = self.query_one("#file-list", RichLog)
        fl.clear()
        for i, f in enumerate(self._files):
            if i == self._file_index:
                fl.write(f"[bold reverse] {f} [/bold reverse]")
            else:
                fl.write(f" {f}")

    def _render_diff(self, diff: str) -> None:
        dv = self.query_one("#diff-view", RichLog)
        dv.clear()
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                dv.write(f"[green]{line}[/green]")
            elif line.startswith("-") and not line.startswith("---"):
                dv.write(f"[red]{line}[/red]")
            elif line.startswith("@@"):
                dv.write(f"[bold cyan]{line}[/bold cyan]")
            elif line.startswith("diff ") or line.startswith("index "):
                dv.write(f"[bold]{line}[/bold]")
            else:
                dv.write(line)

    def action_next_file(self) -> None:
        if self._files:
            self._file_index = (self._file_index + 1) % len(self._files)
            self._render_files()

    def action_prev_file(self) -> None:
        if self._files:
            self._file_index = (self._file_index - 1) % len(self._files)
            self._render_files()

    def action_approve(self) -> None:
        self.dismiss(("approve", ""))

    def action_request_changes(self) -> None:
        self.dismiss(("request_changes", ""))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# HelpModal
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
[bold]orc keybindings[/bold]

[bold cyan]Navigation[/bold cyan]
  Tab / Shift-Tab   Next / prev tab
  1-9               Jump to tab
  j / k             Scroll down / up
  g / G             Top / bottom of log

[bold cyan]Speaking[/bold cyan]
  t / c             Talk to current session
  Ctrl-C            Interrupt current session

[bold cyan]Workers[/bold cyan]
  r                 Open review for current worker
  R                 Restart current worker
  K                 Kill current worker (with confirm)

[bold cyan]Scratch[/bold cyan]
  n                 Open scratch claude (full handoff)
  ?                 Toggle scratch overlay (backchannel)

[bold cyan]App[/bold cyan]
  h                 This help screen
  q                 Quit (with confirm)
"""


class HelpModal(ModalScreen):
    BINDINGS = [Binding("escape,h,q", "close", show=False)]

    DEFAULT_CSS = """
    HelpModal { align: center middle; }
    #help-box {
        width: 70;
        height: 32;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
        overflow-y: scroll;
    }
    """

    def compose(self) -> ComposeResult:
        yield RichLog(id="help-box", highlight=False, markup=True)

    def on_mount(self) -> None:
        log = self.query_one("#help-box", RichLog)
        for line in _HELP_TEXT.splitlines():
            log.write(line)

    def action_close(self) -> None:
        self.dismiss()
