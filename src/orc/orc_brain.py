"""Orchestrator brain: spawn and manage the planning agent."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .worker import claude_bin


# ---------------------------------------------------------------------------
# Typed orc events
# ---------------------------------------------------------------------------

@dataclass
class OrcText:
    text: str

@dataclass
class OrcToolUse:
    id: str
    name: str
    input: Any

@dataclass
class OrcResult:
    is_error: bool
    result: str
    cost_usd: Optional[float]
    duration_ms: Optional[int]

@dataclass
class OrcSystem:
    model: Optional[str]
    session_id: Optional[str]

@dataclass
class OrcThinking:
    text: str

@dataclass
class OrcUsage:
    context_tokens: int


OrcEvent = OrcText | OrcToolUse | OrcResult | OrcSystem | OrcThinking | OrcUsage


def parse_orc_events(raw: dict) -> list[OrcEvent]:
    events: list[OrcEvent] = []
    event_type = raw.get("type", "")

    if event_type == "system":
        events.append(OrcSystem(
            model=raw.get("model"),
            session_id=raw.get("session_id"),
        ))

    elif event_type == "assistant":
        msg = raw.get("message", {})
        usage = msg.get("usage", {})
        total = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        if total > 0:
            events.append(OrcUsage(context_tokens=total))

        for block in msg.get("content", []):
            bt = block.get("type", "")
            if bt == "text":
                events.append(OrcText(text=block.get("text", "")))
            elif bt == "tool_use":
                events.append(OrcToolUse(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    input=block.get("input"),
                ))
            elif bt == "thinking":
                events.append(OrcThinking(text=block.get("thinking", "")))

    elif event_type == "result":
        events.append(OrcResult(
            is_error=raw.get("subtype") == "error",
            result=raw.get("result", ""),
            cost_usd=raw.get("total_cost_usd"),
            duration_ms=raw.get("duration_ms"),
        ))

    return events


# ---------------------------------------------------------------------------
# OrcUsageStats
# ---------------------------------------------------------------------------

@dataclass
class OrcUsageStats:
    total_cost: float = 0.0
    turns: int = 0
    last_context_tokens: int = 0


# ---------------------------------------------------------------------------
# OrcProcess
# ---------------------------------------------------------------------------

class OrcProcess:
    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        events_q: asyncio.Queue,
    ):
        self._proc = proc
        self._events_q = events_q
        self._stdin_lock = asyncio.Lock()
        self.usage = OrcUsageStats()
        self.claude_session_id: Optional[str] = None
        self.model: Optional[str] = None

    async def send(self, message: str) -> None:
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": message},
        })
        async with self._stdin_lock:
            assert self._proc.stdin is not None
            self._proc.stdin.write((msg + "\n").encode())
            await self._proc.stdin.drain()

    async def interrupt(self) -> None:
        import signal
        try:
            os.kill(self._proc.pid, signal.SIGINT)
        except ProcessLookupError:
            pass

    async def kill(self) -> None:
        try:
            self._proc.kill()
            await self._proc.wait()
        except ProcessLookupError:
            pass

    @property
    def pid(self) -> int:
        return self._proc.pid


async def spawn_orc(
    project_dir: Path,
    mcp_config_path: Path,
    model: str,
    system_prompt: str,
    events_q: asyncio.Queue,
    resume_id: Optional[str] = None,
) -> OrcProcess:
    cmd = [
        claude_bin(),
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--mcp-config", str(mcp_config_path),
        "--strict-mcp-config",
        "--dangerously-skip-permissions",
        "--append-system-prompt", system_prompt,
    ]
    if resume_id:
        cmd.extend(["--resume", resume_id])

    env = {**os.environ, "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(project_dir),
        env=env,
    )

    orc_proc = OrcProcess(proc=proc, events_q=events_q)

    async def _read_loop() -> None:
        assert proc.stdout is not None
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
            for ev in parse_orc_events(raw):
                # Track usage inline
                if isinstance(ev, OrcUsage):
                    orc_proc.usage.last_context_tokens = ev.context_tokens
                elif isinstance(ev, OrcResult):
                    orc_proc.usage.turns += 1
                    if ev.cost_usd is not None:
                        orc_proc.usage.total_cost = ev.cost_usd
                elif isinstance(ev, OrcSystem):
                    if ev.session_id:
                        orc_proc.claude_session_id = ev.session_id
                    if ev.model:
                        orc_proc.model = ev.model
                await events_q.put(ev)
        await events_q.put(None)  # sentinel: orc exited

    asyncio.create_task(_read_loop())
    return orc_proc
