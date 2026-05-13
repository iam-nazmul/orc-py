"""Permission policy engine: decide whether a tool call is allowed."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class Decision(str, Enum):
    Allow = "allow"
    Deny = "deny"
    AskUser = "ask_user"


@dataclass
class PolicyResult:
    decision: Decision
    reason: str


# Patterns that are always denied regardless of worktree
_ALWAYS_DENY = [
    re.compile(r"rm\s+-rf\s+/"),
    re.compile(r":\(\)\{.*\}"),        # fork bomb
    re.compile(r"curl.*\|.*sh"),        # curl | sh
    re.compile(r"wget.*\|.*sh"),
]

# File patterns that require user approval even inside the worktree
_SENSITIVE_PATHS = [
    re.compile(r"\.env"),
    re.compile(r"credentials"),
    re.compile(r"\.ssh/"),
    re.compile(r"\.gnupg/"),
    re.compile(r"id_rsa"),
    re.compile(r"\.pem$"),
]


def _is_sensitive_path(path: str) -> bool:
    return any(p.search(path) for p in _SENSITIVE_PATHS)


def evaluate(
    tool_name: str,
    tool_input: dict,
    worktree_path: Optional[str],
    allowed_tools: Optional[list[str]] = None,
) -> PolicyResult:
    """Evaluate whether a tool call should be allowed."""

    # Explicit allowlist overrides
    if allowed_tools and tool_name in allowed_tools:
        return PolicyResult(Decision.Allow, "explicitly allowed")

    # Read-only tools are always fine
    if tool_name in ("Read", "Glob", "LS", "Grep", "WebSearch", "WebFetch"):
        return PolicyResult(Decision.Allow, "read-only tool")

    # Check path-based tools
    path = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("file")
        or ""
    )
    if path and _is_sensitive_path(path):
        return PolicyResult(Decision.AskUser, f"sensitive path: {path}")

    # Bash/shell: check for dangerous patterns
    if tool_name in ("Bash", "Shell", "Execute"):
        command = tool_input.get("command", "")
        for pat in _ALWAYS_DENY:
            if pat.search(command):
                return PolicyResult(Decision.Deny, f"dangerous pattern in command")
        # Bash is allowed in worktree context
        return PolicyResult(Decision.Allow, "bash in worktree")

    # Write/Edit tools: ensure they stay inside worktree
    if tool_name in ("Write", "Edit", "NotebookEdit", "MultiEdit") and worktree_path:
        abs_path = Path(path).resolve() if path else None
        wt = Path(worktree_path).resolve()
        if abs_path and not str(abs_path).startswith(str(wt)):
            return PolicyResult(
                Decision.Deny,
                f"write outside worktree: {path}",
            )
        return PolicyResult(Decision.Allow, "write inside worktree")

    # Default: allow
    return PolicyResult(Decision.Allow, "default allow")
