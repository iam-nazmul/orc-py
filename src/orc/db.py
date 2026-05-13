"""SQLite persistence: schema, migrations, query helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from .session import Session, SessionMode, SessionState, state_from_dict, state_to_dict


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    task        TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    branch      TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'Running',
    state_data  TEXT,
    mode        TEXT NOT NULL DEFAULT 'Watch',
    model       TEXT NOT NULL DEFAULT 'sonnet',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    ended_at    TEXT,
    claude_session_id TEXT
);

CREATE TABLE IF NOT EXISTS state_transitions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    from_state   TEXT NOT NULL,
    to_state     TEXT NOT NULL,
    reason       TEXT,
    triggered_by TEXT,
    at           TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS permission_decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    request     TEXT NOT NULL,
    decision    TEXT NOT NULL,
    decided_by  TEXT,
    reason      TEXT,
    at          TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS task_graphs (
    run_id      TEXT PRIMARY KEY,
    user_prompt TEXT NOT NULL,
    graph       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_nodes (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    description TEXT NOT NULL,
    depends_on  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    session_id  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_graphs(run_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    diff_hash    TEXT NOT NULL,
    comments     TEXT,
    overall      TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    submitted_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS event_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    at         TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_name(state: SessionState) -> str:
    return type(state).__name__.removeprefix("State")


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    return datetime.fromisoformat(s)


def _row_to_session(row: aiosqlite.Row) -> Session:
    (id_, name, task, wt, branch, base, state_name, state_data,
     mode_str, model, created, updated, ended, csid) = row

    state_dict = {"name": state_name}
    if state_data:
        try:
            extra = json.loads(state_data)
            state_dict.update(extra)
        except json.JSONDecodeError:
            pass

    return Session(
        id=id_,
        name=name,
        task=task,
        worktree_path=wt,
        branch=branch,
        base_commit=base,
        state=state_from_dict(state_dict),
        mode=SessionMode(mode_str),
        model=model,
        created_at=datetime.fromisoformat(created),
        updated_at=datetime.fromisoformat(updated),
        ended_at=_parse_dt(ended),
        claude_session_id=csid,
    )


class Database:
    def __init__(self, db: aiosqlite.Connection):
        self._db = db
        self._db.row_factory = aiosqlite.Row

    @classmethod
    async def open(cls, path: Path) -> "Database":
        path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(str(path))
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    # --- Sessions ---

    async def create_session(self, session: Session) -> None:
        state = state_to_dict(session.state)
        state_name = state.pop("name")
        state_data = json.dumps(state) if state else None
        await self._db.execute(
            """INSERT INTO sessions
               (id, name, task, worktree_path, branch, base_commit,
                state, state_data, mode, model, created_at, updated_at,
                ended_at, claude_session_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session.id, session.name, session.task, session.worktree_path,
                session.branch, session.base_commit,
                state_name, state_data,
                session.mode.value, session.model,
                session.created_at.isoformat(), session.updated_at.isoformat(),
                session.ended_at.isoformat() if session.ended_at else None,
                session.claude_session_id,
            ),
        )
        await self._db.commit()

    async def update_session_state(
        self,
        session_id: str,
        state: SessionState,
        from_state_name: Optional[str] = None,
    ) -> None:
        now = _now()
        state_dict = state_to_dict(state)
        state_name = state_dict.pop("name")
        state_data = json.dumps(state_dict) if state_dict else None

        ended_at = now if state_name in ("Done", "Failed") else None
        await self._db.execute(
            """UPDATE sessions SET state=?, state_data=?, updated_at=?, ended_at=COALESCE(ended_at, ?)
               WHERE id=?""",
            (state_name, state_data, now, ended_at, session_id),
        )
        if from_state_name is not None:
            await self._db.execute(
                "INSERT INTO state_transitions (session_id, from_state, to_state, at) VALUES (?,?,?,?)",
                (session_id, from_state_name, state_name, now),
            )
        await self._db.commit()

    async def set_claude_session_id(self, session_id: str, csid: str) -> None:
        await self._db.execute(
            "UPDATE sessions SET claude_session_id=? WHERE id=?", (csid, session_id)
        )
        await self._db.commit()

    async def set_session_summary(self, session_id: str, summary: str) -> None:
        await self._db.execute(
            "UPDATE sessions SET state_data=json_set(COALESCE(state_data,'{}'), '$.summary', ?) WHERE id=?",
            (summary, session_id),
        )
        await self._db.commit()

    async def remove_session(self, session_id: str) -> None:
        await self._db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        await self._db.commit()

    async def list_sessions(self) -> list[Session]:
        async with self._db.execute(
            """SELECT id, name, task, worktree_path, branch, base_commit,
                      state, state_data, mode, model, created_at, updated_at,
                      ended_at, claude_session_id
               FROM sessions ORDER BY created_at ASC"""
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_session(tuple(r)) for r in rows]

    async def get_session(self, session_id: str) -> Optional[Session]:
        async with self._db.execute(
            """SELECT id, name, task, worktree_path, branch, base_commit,
                      state, state_data, mode, model, created_at, updated_at,
                      ended_at, claude_session_id
               FROM sessions WHERE id=?""",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_session(tuple(row))

    # --- Permission decisions ---

    async def record_permission(
        self,
        session_id: str,
        request: str,
        decision: str,
        decided_by: str,
        reason: str,
    ) -> None:
        await self._db.execute(
            """INSERT INTO permission_decisions
               (session_id, request, decision, decided_by, reason, at)
               VALUES (?,?,?,?,?,?)""",
            (session_id, request, decision, decided_by, reason, _now()),
        )
        await self._db.commit()

    # --- Reviews ---

    async def create_review(self, session_id: str, diff_hash: str) -> int:
        cursor = await self._db.execute(
            "INSERT INTO reviews (session_id, diff_hash, status, created_at) VALUES (?,?,'pending',?)",
            (session_id, diff_hash, _now()),
        )
        await self._db.commit()
        return cursor.lastrowid  # type: ignore

    async def update_review(
        self,
        review_id: int,
        comments: Optional[str],
        overall: Optional[str],
        status: str,
    ) -> None:
        submitted_at = _now() if status == "submitted" else None
        await self._db.execute(
            "UPDATE reviews SET comments=?, overall=?, status=?, submitted_at=? WHERE id=?",
            (comments, overall, status, submitted_at, review_id),
        )
        await self._db.commit()

    async def get_latest_review(self, session_id: str) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM reviews WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    # --- Task graph ---

    async def upsert_task_graph(self, run_id: str, user_prompt: str, graph_json: str) -> None:
        now = _now()
        await self._db.execute(
            """INSERT INTO task_graphs (run_id, user_prompt, graph, created_at, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET graph=excluded.graph, updated_at=excluded.updated_at""",
            (run_id, user_prompt, graph_json, now, now),
        )
        await self._db.commit()

    # --- Event log ---

    async def append_event(self, session_id: str, kind: str, payload: str) -> None:
        await self._db.execute(
            "INSERT INTO event_log (session_id, kind, payload, at) VALUES (?,?,?,?)",
            (session_id, kind, payload, _now()),
        )
        await self._db.commit()

    async def load_events(self, session_id: str) -> list[tuple[str, str]]:
        async with self._db.execute(
            "SELECT kind, payload FROM event_log WHERE session_id=? ORDER BY id ASC",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(r[0], r[1]) for r in rows]
