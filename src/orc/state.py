"""Central state manager: single writer, broadcast to UI."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .db import Database
from .session import (
    Session, SessionEvent, SessionMode, SessionState,
    StateRunning, transition, InvalidTransition,
)


# ---------------------------------------------------------------------------
# Pending questions (worker → user/orc)
# ---------------------------------------------------------------------------

@dataclass
class PendingQuestion:
    id: str
    session_id: str
    question: str
    context: Optional[str]
    future: asyncio.Future  # resolved with the answer string


class AnsweredBy(str):
    User = "user"
    Orc = "orc"


# ---------------------------------------------------------------------------
# State update broadcast payload
# ---------------------------------------------------------------------------

@dataclass
class StateUpdate:
    """Broadcast to all UI listeners whenever state changes."""
    sessions: list[Session]
    pending_questions: list[PendingQuestion]


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------

class StateManager:
    def __init__(self, db: Database):
        self._db = db
        self._sessions: dict[str, Session] = {}
        self._questions: dict[str, PendingQuestion] = {}
        self._listeners: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    # --- Listener subscription ---

    def subscribe(self) -> asyncio.Queue:
        """Return a queue that receives StateUpdate on every change."""
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._listeners.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._listeners.remove(q)
        except ValueError:
            pass

    async def _broadcast(self) -> None:
        update = StateUpdate(
            sessions=list(self._sessions.values()),
            pending_questions=list(self._questions.values()),
        )
        for q in list(self._listeners):
            try:
                q.put_nowait(update)
            except asyncio.QueueFull:
                pass  # slow consumer; drop the update

    # --- Session CRUD ---

    async def load_from_db(self) -> None:
        sessions = await self._db.list_sessions()
        for s in sessions:
            self._sessions[s.id] = s
        await self._broadcast()

    async def create_session(
        self,
        name: str,
        task: str,
        worktree_path: str,
        branch: str,
        base_commit: str,
        model: str = "sonnet",
    ) -> Session:
        async with self._lock:
            s = Session(
                name=name,
                task=task,
                worktree_path=worktree_path,
                branch=branch,
                base_commit=base_commit,
                model=model,
            )
            self._sessions[s.id] = s
            await self._db.create_session(s)
        await self._broadcast()
        return s

    async def apply_event(self, session_id: str, event: SessionEvent) -> SessionState:
        async with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                raise KeyError(f"Session {session_id} not found")
            old_name = type(s.state).__name__.removeprefix("State")
            try:
                new_state = transition(s.state, event)
            except InvalidTransition:
                return s.state
            s.state = new_state
            await self._db.update_session_state(session_id, new_state, from_state_name=old_name)
        await self._broadcast()
        return new_state

    async def set_mode(self, session_id: str, mode: SessionMode) -> None:
        async with self._lock:
            s = self._sessions.get(session_id)
            if s:
                s.mode = mode
        await self._broadcast()

    async def set_claude_session_id(self, session_id: str, csid: str) -> None:
        async with self._lock:
            s = self._sessions.get(session_id)
            if s:
                s.claude_session_id = csid
                await self._db.set_claude_session_id(session_id, csid)

    async def set_summary(self, session_id: str, summary: str) -> None:
        async with self._lock:
            s = self._sessions.get(session_id)
            if s:
                s.summary = summary

    async def remove_session(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)
            await self._db.remove_session(session_id)
        await self._broadcast()

    async def list_sessions(self) -> list[Session]:
        return list(self._sessions.values())

    async def get_session(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    # --- Permission decisions ---

    async def record_permission(
        self,
        session_id: str,
        request: str,
        decision: str,
        decided_by: str,
        reason: str,
    ) -> None:
        await self._db.record_permission(session_id, request, decision, decided_by, reason)

    # --- Q&A (worker → user/orc) ---

    async def ask_user(
        self,
        session_id: str,
        question: str,
        context: Optional[str] = None,
    ) -> str:
        """Block until a human or orc answers. Returns the answer."""
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        qid = str(uuid.uuid4())
        pq = PendingQuestion(
            id=qid,
            session_id=session_id,
            question=question,
            context=context,
            future=future,
        )
        async with self._lock:
            self._questions[qid] = pq
        await self._broadcast()
        return await future

    async def answer_question(
        self,
        question_id: str,
        answer: str,
        answered_by: str = AnsweredBy.User,
    ) -> None:
        async with self._lock:
            pq = self._questions.pop(question_id, None)
        if pq and not pq.future.done():
            pq.future.set_result(answer)
        await self._broadcast()

    async def get_pending_questions(self) -> list[PendingQuestion]:
        return list(self._questions.values())

    # --- Event log ---

    async def append_event(self, session_id: str, kind: str, payload: str) -> None:
        await self._db.append_event(session_id, kind, payload)

    async def load_events(self, session_id: str) -> list[tuple[str, str]]:
        return await self._db.load_events(session_id)

    # --- Task graph ---

    async def update_task_graph(
        self, run_id: str, user_prompt: str, graph_json: str
    ) -> None:
        await self._db.upsert_task_graph(run_id, user_prompt, graph_json)
