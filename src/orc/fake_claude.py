"""Fake claude shim for harness/test runs.

Emits stream-json events without calling the real claude API.
Activated via ORC_CLAUDE_BIN=fake-claude (or the installed entrypoint).

Usage (same interface as real claude):
  fake-claude -p --input-format stream-json --output-format stream-json ...
"""

from __future__ import annotations

import json
import sys
import time
import uuid


_SESSION_ID = str(uuid.uuid4())
_MODEL = "claude-sonnet-fake"

_TURN = 0


def _emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def _system_init() -> None:
    _emit({
        "type": "system",
        "subtype": "init",
        "session_id": _SESSION_ID,
        "model": _MODEL,
        "tools": [],
        "mcp_servers": [],
    })


def _assistant_reply(text: str) -> None:
    _emit({
        "type": "assistant",
        "message": {
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": _MODEL,
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": len(text.split()),
            },
        },
    })


def _result(cost: float = 0.001) -> None:
    _emit({
        "type": "result",
        "subtype": "success",
        "total_cost_usd": cost,
        "duration_ms": 500,
        "result": "Task complete.",
        "session_id": _SESSION_ID,
    })


def main() -> None:
    _system_init()
    turn = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") != "user":
            continue

        content = msg.get("message", {}).get("content", "")
        if isinstance(content, list):
            text = " ".join(
                b.get("text", "") for b in content if b.get("type") == "text"
            )
        else:
            text = str(content)

        turn += 1
        reply = f"[fake-claude turn {turn}] Understood: {text[:80]}"
        _assistant_reply(reply)
        _result(cost=0.001 * turn)

    sys.exit(0)


if __name__ == "__main__":
    main()
