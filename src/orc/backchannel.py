"""Backchannel claude: a persistent side-channel conversation."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .worker import claude_bin, parse_worker_events, WorkerEvent


@dataclass
class BackchannelMessage:
    role: str   # "user" or "assistant"
    text: str


class BackchannelProcess:
    """A long-running claude child for the scratch overlay (?).

    Sonnet by default; can be promoted to opus via Ctrl-B.
    The transcript never enters orc's main event stream.
    """

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        model: str,
        events_q: asyncio.Queue,
    ):
        self._proc = proc
        self.model = model
        self._events_q = events_q
        self._stdin_lock = asyncio.Lock()
        self.transcript: list[BackchannelMessage] = []

    async def send(self, message: str) -> None:
        self.transcript.append(BackchannelMessage(role="user", text=message))
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": message},
        })
        async with self._stdin_lock:
            assert self._proc.stdin is not None
            self._proc.stdin.write((msg + "\n").encode())
            await self._proc.stdin.drain()

    def append_assistant(self, text: str) -> None:
        if self.transcript and self.transcript[-1].role == "assistant":
            self.transcript[-1].text += text
        else:
            self.transcript.append(BackchannelMessage(role="assistant", text=text))

    async def kill(self) -> None:
        try:
            self._proc.kill()
            await self._proc.wait()
        except ProcessLookupError:
            pass

    async def interrupt(self) -> None:
        try:
            os.kill(self._proc.pid, signal.SIGINT)
        except ProcessLookupError:
            pass

    async def summarise(self) -> str:
        """Ask the backchannel for a 1–3 sentence summary and return it."""
        result_future: asyncio.Future = asyncio.get_event_loop().create_future()

        async def _collect() -> None:
            acc = []
            async for ev in _drain(self._events_q):
                if hasattr(ev, "text"):
                    acc.append(ev.text)
                elif ev is None or (hasattr(ev, "is_error") and ev.is_error is not None):
                    break
            result_future.set_result("".join(acc))

        await self.send(
            "Please summarise our conversation so far in 1–3 sentences."
        )
        asyncio.create_task(_collect())
        return await asyncio.wait_for(result_future, timeout=30)


async def _drain(q: asyncio.Queue):
    while True:
        item = await q.get()
        yield item
        if item is None:
            break


async def spawn_backchannel(
    project_dir: Path,
    model: str = "sonnet",
    events_q: Optional[asyncio.Queue] = None,
) -> BackchannelProcess:
    if events_q is None:
        events_q = asyncio.Queue(maxsize=256)

    cmd = [
        claude_bin(),
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--dangerously-skip-permissions",
    ]
    env = {**os.environ}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(project_dir),
        env=env,
    )

    channel = BackchannelProcess(proc=proc, model=model, events_q=events_q)

    async def _read_loop() -> None:
        assert proc.stdout is not None
        _session_id = "backchannel"
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            trimmed = line.decode().strip()
            if not trimmed:
                continue
            try:
                raw = json.loads(trimmed)
            except json.JSONDecodeError:
                continue
            for ev in parse_worker_events(_session_id, raw):
                if hasattr(ev, "text"):
                    channel.append_assistant(ev.text)
                await events_q.put(ev)
        await events_q.put(None)

    asyncio.create_task(_read_loop())
    return channel
