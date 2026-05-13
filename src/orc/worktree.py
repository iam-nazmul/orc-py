"""Git worktree management."""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Optional


class WorktreeError(Exception):
    pass


async def _run(cmd: list[str], cwd: Optional[Path] = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise WorktreeError(f"{' '.join(cmd)} failed: {err.decode().strip()}")
    return out.decode().strip()


async def git_root(project_dir: Path) -> Path:
    result = await _run(["git", "rev-parse", "--show-toplevel"], cwd=project_dir)
    return Path(result)


async def current_commit(project_dir: Path) -> str:
    return await _run(["git", "rev-parse", "HEAD"], cwd=project_dir)


async def create_worktree(
    project_dir: Path,
    worktrees_dir: Path,
    branch: str,
) -> Path:
    """Add a git worktree at <worktrees_dir>/<branch> on a new branch."""
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    # Sanitise branch name for use as directory name
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", branch)
    wt_path = worktrees_dir / safe
    await _run(
        ["git", "worktree", "add", "-b", branch, str(wt_path)],
        cwd=project_dir,
    )
    return wt_path


async def remove_worktree(project_dir: Path, wt_path: Path) -> None:
    try:
        await _run(["git", "worktree", "remove", "--force", str(wt_path)], cwd=project_dir)
    except WorktreeError:
        pass
    try:
        await _run(
            ["git", "branch", "-D", wt_path.name],
            cwd=project_dir,
        )
    except WorktreeError:
        pass


async def list_worktrees(project_dir: Path) -> list[dict]:
    out = await _run(["git", "worktree", "list", "--porcelain"], cwd=project_dir)
    worktrees = []
    current: dict = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            current = {"path": line[9:]}
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
        elif line.startswith("branch "):
            current["branch"] = line[7:]
        elif line == "bare":
            current["bare"] = True
    if current:
        worktrees.append(current)
    return worktrees


async def prune_worktrees(project_dir: Path) -> None:
    await _run(["git", "worktree", "prune"], cwd=project_dir)


async def diff_stat(worktree: Path, base_commit: str) -> str:
    """Return the diff --stat between base_commit and HEAD in the worktree."""
    try:
        return await _run(
            ["git", "diff", "--stat", base_commit, "HEAD"],
            cwd=worktree,
        )
    except WorktreeError:
        return ""


async def full_diff(worktree: Path, base_commit: str) -> str:
    """Return the full unified diff between base_commit and HEAD."""
    try:
        return await _run(
            ["git", "diff", base_commit, "HEAD"],
            cwd=worktree,
        )
    except WorktreeError:
        return ""


def diff_hash(diff_text: str) -> str:
    return hashlib.sha256(diff_text.encode()).hexdigest()[:16]


async def changed_files(worktree: Path, base_commit: str) -> list[str]:
    """Return list of files changed since base_commit."""
    try:
        out = await _run(
            ["git", "diff", "--name-only", base_commit, "HEAD"],
            cwd=worktree,
        )
        return [f for f in out.splitlines() if f]
    except WorktreeError:
        return []
