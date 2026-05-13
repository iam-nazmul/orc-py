"""Worker process: a stream-json claude child, owned by orc."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def claude_bin() -> str:
    """Path to the claude binary. Override with ORC_CLAUDE_BIN for testing."""
    return os.environ.get("ORC_CLAUDE_BIN", "claude")


def context_cap_for(model: str) -> int:
    m = model.lower()
    if "opus" in m or "sonnet-4-6" in m or m == "sonnet":
        return 1_000_000
    return 200_000


# ---------------------------------------------------------------------------
# Typed worker events
# ---------------------------------------------------------------------------

@dataclass
class EvText:
    session_id: str
    text: str

@dataclass
class EvToolUse:
    session_id: str
    name: str
    input: Any

@dataclass
class EvToolResult:
    session_id: str
    text: str
    is_error: bool

@dataclass
class EvThinking:
    session_id: str
    text: str

@dataclass
class EvResult:
    session_id: str
    is_error: bool
    cost_usd: Optional[float]

@dataclass
class EvClaudeSessionId:
    session_id: str
    claude_session_id: str

@dataclass
class EvExited:
    session_id: str
    code: Optional[int]

@dataclass
class EvOrcInstruction:
    session_id: str
    text: str

@dataclass
class EvUsage:
    session_id: str
    context_tokens: int


WorkerEvent = (
    EvText | EvToolUse | EvToolResult | EvThinking |
    EvResult | EvClaudeSessionId | EvExited | EvOrcInstruction | EvUsage
)


# ---------------------------------------------------------------------------
# Stream-JSON parsing
# ---------------------------------------------------------------------------

def parse_worker_events(session_id: str, raw: dict) -> list[WorkerEvent]:
    out: list[WorkerEvent] = []
    event_type = raw.get("type", "")

    if event_type == "system":
        csid = raw.get("session_id")
        if csid:
            out.append(EvClaudeSessionId(session_id=session_id, claude_session_id=csid))

    elif event_type == "assistant":
        msg = raw.get("message", {})
        usage = msg.get("usage", {})
        input_t = usage.get("input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_create = usage.get("cache_creation_input_tokens", 0)
        total = input_t + cache_read + cache_create
        if total > 0:
            out.append(EvUsage(session_id=session_id, context_tokens=total))

        for block in msg.get("content", []):
            bt = block.get("type", "")
            if bt == "text":
                text = block.get("text", "")
                out.append(EvText(session_id=session_id, text=text))
            elif bt == "tool_use":
                out.append(EvToolUse(
                    session_id=session_id,
                    name=block.get("name", ""),
                    input=block.get("input"),
                ))
            elif bt == "thinking":
                out.append(EvThinking(session_id=session_id, text=block.get("thinking", "")))

    elif event_type == "user":
        for block in raw.get("message", {}).get("content", []):
            if block.get("type") == "tool_result":
                is_error = block.get("is_error", False)
                content = block.get("content", "")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "\n".join(
                        b.get("text", "") for b in content if b.get("type") == "text"
                    )
                else:
                    text = ""
                out.append(EvToolResult(session_id=session_id, text=text, is_error=is_error))

    elif event_type == "result":
        is_error = raw.get("subtype") == "error"
        cost = raw.get("total_cost_usd")
        out.append(EvResult(session_id=session_id, is_error=is_error, cost_usd=cost))

    return out


# ---------------------------------------------------------------------------
# WorkerHandle
# ---------------------------------------------------------------------------

class WorkerHandle:
    def __init__(
        self,
        session_id: str,
        worktree: Path,
        model: str,
        proc: asyncio.subprocess.Process,
    ):
        self.session_id = session_id
        self.worktree = worktree
        self.model = model
        self._proc = proc
        self._stdin_lock = asyncio.Lock()

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
        pid = self._proc.pid
        if pid:
            try:
                os.kill(pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    async def kill(self) -> None:
        try:
            self._proc.kill()
            await self._proc.wait()
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------------------
# Spawn helpers
# ---------------------------------------------------------------------------

def _build_cmd(
    model: str,
    mcp_config_path: Path,
    system_prompt: str,
    worktree: Path,
    extra_args: list[str] | None = None,
) -> list[str]:
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
    if extra_args:
        cmd.extend(extra_args)
    return cmd


async def _finalise_spawn(
    session_id: str,
    worktree: Path,
    model: str,
    proc: asyncio.subprocess.Process,
    events_q: asyncio.Queue,
) -> WorkerHandle:
    handle = WorkerHandle(session_id=session_id, worktree=worktree, model=model, proc=proc)

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
            for ev in parse_worker_events(session_id, raw):
                await events_q.put(ev)
        code = proc.returncode
        await events_q.put(EvExited(session_id=session_id, code=code))

    asyncio.create_task(_read_loop())
    return handle


async def spawn_worker(
    session_id: str,
    worktree: Path,
    model: str,
    mcp_config_path: Path,
    system_prompt: str,
    task: str,
    events_q: asyncio.Queue,
) -> WorkerHandle:
    cmd = _build_cmd(model, mcp_config_path, system_prompt, worktree)
    env = {**os.environ, "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(worktree),
        env=env,
    )
    handle = await _finalise_spawn(session_id, worktree, model, proc, events_q)
    await handle.send(task)
    return handle


async def spawn_worker_resume(
    session_id: str,
    worktree: Path,
    model: str,
    mcp_config_path: Path,
    system_prompt: str,
    claude_session_id: str,
    events_q: asyncio.Queue,
) -> WorkerHandle:
    cmd = _build_cmd(
        model, mcp_config_path, system_prompt, worktree,
        extra_args=["--resume", claude_session_id],
    )
    env = {**os.environ, "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(worktree),
        env=env,
    )
    return await _finalise_spawn(session_id, worktree, model, proc, events_q)
