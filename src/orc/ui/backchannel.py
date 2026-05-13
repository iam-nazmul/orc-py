"""Backchannel overlay widget (the ? scratch overlay)."""

from __future__ import annotations

import asyncio
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import RichLog, Static, TextArea

from ..backchannel import BackchannelProcess
from ..worker import EvText


class BackchannelOverlay(Widget):
    """Floating overlay with a persistent backchannel conversation.

    Keys while open:
      Esc         — close (channel keeps running)
      ?           — also closes when input is empty
      Ctrl-X      — kill channel and drop state
      Ctrl-B      — promote to opus
      Ctrl-N      — promote content to orc (send summary)
      Enter       — send message
      Shift-Enter — newline in input
    """

    DEFAULT_CSS = """
    BackchannelOverlay {
        align: center middle;
        layer: overlay;
        background: rgba(0,0,0,0.5);
        width: 100%;
        height: 100%;
    }
    #bc-dialog {
        width: 90;
        height: 40;
        background: $surface;
        border: solid $accent;
        layout: vertical;
    }
    #bc-header {
        height: 1;
        background: $panel;
        padding: 0 1;
        color: $accent;
        text-style: bold;
    }
    #bc-transcript {
        height: 1fr;
        overflow-y: scroll;
        padding: 0 1;
    }
    #bc-input-area {
        height: 5;
        border-top: solid $panel;
        padding: 0 1;
    }
    #bc-input {
        height: 3;
        border: solid $panel;
    }
    #bc-hint {
        height: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("escape", "close", show=False),
        Binding("ctrl+x", "kill_channel", show=False),
        Binding("ctrl+b", "promote_opus", show=False),
        Binding("ctrl+n", "promote_orc", show=False),
    ]

    def __init__(
        self,
        channel: BackchannelProcess,
        events_q: asyncio.Queue,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.channel = channel
        self.events_q = events_q

    def compose(self) -> ComposeResult:
        with Static(id="bc-dialog"):
            yield Static(
                f"Scratch claude [{self.channel.model}]  "
                "Esc:close  Ctrl-X:kill  Ctrl-B:opus  Ctrl-N:→orc",
                id="bc-header",
            )
            yield RichLog(id="bc-transcript", highlight=False, markup=True)
            with Static(id="bc-input-area"):
                yield TextArea(id="bc-input")
                yield Static("Enter: send  Shift-Enter: newline", id="bc-hint")

    def on_mount(self) -> None:
        self._render_transcript()

    def _render_transcript(self) -> None:
        log = self.query_one("#bc-transcript", RichLog)
        log.clear()
        for msg in self.channel.transcript:
            if msg.role == "user":
                log.write(f"[bold cyan]you:[/bold cyan] {msg.text}")
            else:
                log.write(f"[bold]{self.channel.model}:[/bold] {msg.text}")

    def on_event(self, ev) -> None:
        if isinstance(ev, EvText):
            log = self.query_one("#bc-transcript", RichLog)
            # Update last assistant message in log
            log.write(f"  {ev.text}")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.action_close()
        elif event.key == "enter":
            ta = self.query_one("#bc-input", TextArea)
            text = ta.text.strip()
            if not text:
                self.action_close()
                return
            self.run_worker(self._send(text), exclusive=False)
            ta.clear()

    async def _send(self, text: str) -> None:
        await self.channel.send(text)
        log = self.query_one("#bc-transcript", RichLog)
        log.write(f"[bold cyan]you:[/bold cyan] {text}")

    def action_close(self) -> None:
        self.remove()

    async def action_kill_channel(self) -> None:
        await self.channel.kill()
        self.app.backchannel = None  # type: ignore
        self.remove()

    async def action_promote_opus(self) -> None:
        from ..backchannel import spawn_backchannel
        summary = ""
        if self.channel.transcript:
            try:
                summary = await self.channel.summarise()
            except Exception:
                pass
        await self.channel.kill()
        new_ch = await spawn_backchannel(
            self.app.project_dir,  # type: ignore
            model="opus",
            events_q=self.events_q,
        )
        if summary:
            await new_ch.send(f"[context from prior conversation] {summary}")
        self.app.backchannel = new_ch  # type: ignore
        self.channel = new_ch
        self._render_transcript()
        header = self.query_one("#bc-header", Static)
        header.update(
            f"Scratch claude [opus]  Esc:close  Ctrl-X:kill  Ctrl-B:opus  Ctrl-N:→orc"
        )

    async def action_promote_orc(self) -> None:
        summary = ""
        try:
            summary = await self.channel.summarise()
        except Exception:
            pass
        await self.channel.kill()
        self.app.backchannel = None  # type: ignore
        if summary and self.app.orc_process:  # type: ignore
            msg = f"[from scratch claude] {summary}"
            await self.app.orc_process.send(msg)  # type: ignore
            self.app._log_orc("user", msg)  # type: ignore
        self.remove()
