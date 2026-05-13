"""CLI entry point: `orc` command."""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

from .state import StateManager
from .db import Database
from .worker import claude_bin
from .worktree import git_root, current_commit, prune_worktrees, list_worktrees


CONFIG_DIR = Path.home() / ".config" / "orc"
DB_PATH = CONFIG_DIR / "state.db"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="orc",
        description="TUI for parallel Claude conversations with an orchestrator.",
    )
    p.add_argument(
        "-p", "--project",
        metavar="PATH",
        help="Project directory (default: current directory)",
        default=".",
    )
    sub = p.add_subparsers(dest="command")
    sub.add_parser("doctor", help="Environment health check")
    sub.add_parser("mcp-server", help="Run the MCP server (internal use)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def _check(label: str, ok: bool, detail: str = "") -> bool:
    status = "[OK]" if ok else "[FAIL]"
    line = f"  {status} {label}"
    if detail:
        line += f": {detail}"
    print(line)
    return ok


def run_doctor(project_dir: Path) -> int:
    print("orc doctor\n")
    ok = True

    # 1. Claude CLI available
    try:
        out = subprocess.check_output(
            [claude_bin(), "--version"], stderr=subprocess.STDOUT, text=True
        ).strip()
        ok &= _check("claude CLI", True, out[:60])
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        ok &= _check("claude CLI", False, str(e))

    # 2. Git repo
    try:
        root = asyncio.run(git_root(project_dir))
        ok &= _check("git repo", True, str(root))
    except Exception as e:
        ok &= _check("git repo", False, str(e))

    # 3. DB accessible
    try:
        db_path = DB_PATH
        db_path.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.close()
        ok &= _check("SQLite DB", True, str(db_path))
    except Exception as e:
        ok &= _check("SQLite DB", False, str(e))

    # 4. Orphaned worktrees
    try:
        wts = asyncio.run(list_worktrees(project_dir))
        orc_wts = [w for w in wts if "orc/" in w.get("branch", "")]
        if orc_wts:
            ok &= _check("orphaned worktrees", False, f"{len(orc_wts)} orc/ branch(es) found")
        else:
            ok &= _check("orphaned worktrees", True, "none")
    except Exception as e:
        ok &= _check("orphaned worktrees", False, str(e))

    # 5. Orphaned processes
    try:
        result = subprocess.run(
            ["pgrep", "-lf", "orc"], capture_output=True, text=True
        )
        procs = [l for l in result.stdout.splitlines() if "pgrep" not in l]
        if procs:
            ok &= _check("orphaned orc processes", False, f"{len(procs)} found")
        else:
            ok &= _check("orphaned orc processes", True, "none")
    except Exception:
        pass  # pgrep not available on all platforms

    print()
    print("All checks passed." if ok else "Some checks failed.")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Main TUI startup
# ---------------------------------------------------------------------------

async def _run_tui(project_dir: Path) -> None:
    from .db import Database
    from .state import StateManager
    from .orc_brain import spawn_orc
    from .mcp import write_worker_mcp_config, orc_system_prompt
    from .worktree import git_root, current_commit
    from .ui.app import OrcApp, ORC_SESSION_ID, DEFAULT_ORC_MODEL
    import json
    import os

    # Resolve project
    git_dir = await git_root(project_dir)
    worktrees_dir = git_dir.parent / ".orc-worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    # DB + state
    db = await Database.open(DB_PATH)
    state = StateManager(db=db)
    await state.load_from_db()

    # Worker events queue (shared by all workers)
    worker_events_q: asyncio.Queue = asyncio.Queue(maxsize=1024)
    orc_events_q: asyncio.Queue = asyncio.Queue(maxsize=1024)

    # Write a minimal MCP config for orc itself
    orc_mcp_path = git_dir / ".orc-mcp.json"
    if not orc_mcp_path.exists():
        orc_mcp_path.write_text(json.dumps({"mcpServers": {}}, indent=2))

    project_name = git_dir.name
    sys_prompt = orc_system_prompt(project_name)

    # Try to spawn orc brain (may fail if claude not available)
    orc_process = None
    try:
        orc_process = await spawn_orc(
            project_dir=git_dir,
            mcp_config_path=orc_mcp_path,
            model=DEFAULT_ORC_MODEL,
            system_prompt=sys_prompt,
            events_q=orc_events_q,
        )
    except Exception as e:
        print(f"Warning: could not spawn orc brain: {e}", file=sys.stderr)

    # Launch TUI
    app = OrcApp(
        project_dir=git_dir,
        state_manager=state,
        orc_process=orc_process,
        orc_events_q=orc_events_q,
        worker_handles={},
        worker_events_q=worker_events_q,
        worktrees_dir=worktrees_dir,
    )
    await app.run_async()

    # Cleanup
    await db.close()


def main() -> None:
    args = _parse_args()
    project_dir = Path(args.project).resolve()

    if args.command == "doctor":
        sys.exit(run_doctor(project_dir))
    elif args.command == "mcp-server":
        # Placeholder for future MCP server implementation
        print("mcp-server: not yet implemented")
        sys.exit(1)
    else:
        try:
            asyncio.run(_run_tui(project_dir))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
