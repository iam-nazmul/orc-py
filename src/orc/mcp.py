"""MCP server configuration for workers."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Optional


_ORC_MCP_SERVER = {
    "type": "stdio",
    "command": "orc",
    "args": ["mcp-server"],
    "env": {},
}


def write_worker_mcp_config(
    session_id: str,
    worktree: Path,
    extra_servers: Optional[dict] = None,
) -> Path:
    """Write an MCP config JSON for a worker and return the path."""
    servers: dict = {}
    if extra_servers:
        servers.update(extra_servers)
    config = {"mcpServers": servers}

    config_path = worktree / ".orc-mcp.json"
    config_path.write_text(json.dumps(config, indent=2))
    return config_path


def worker_system_prompt(session_id: str, project_name: str) -> str:
    return (
        f"You are a worker agent in project '{project_name}'. "
        f"Your session ID is {session_id}. "
        "Complete the task delegated to you. When done, summarise your work clearly. "
        "If you need a human decision, use the ask_user tool. "
        "Stay focused on your assigned scope and worktree."
    )


def orc_system_prompt(project_name: str) -> str:
    return (
        f"You are the orchestrator (orc) for project '{project_name}'. "
        "Plan and delegate subtasks to worker agents. Each worker runs in an isolated git worktree. "
        "Spawn workers with the spawn_worker tool. Supervise their progress. "
        "Answer worker questions when you have context; escalate to the user when you don't. "
        "When all work is reviewed and complete, summarise the result for the user."
    )
