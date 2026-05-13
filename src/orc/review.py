"""Review system: diff view, whole-file view, comments, approval."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .worktree import changed_files, full_diff, diff_stat


@dataclass
class LineComment:
    file: str
    line: int
    text: str


@dataclass
class ReviewDraft:
    session_id: str
    diff_hash: str
    comments: list[LineComment] = field(default_factory=list)
    overall: str = ""
    approved: bool = False

    def add_comment(self, file: str, line: int, text: str) -> None:
        self.comments.append(LineComment(file=file, line=line, text=text))

    def to_payload(self) -> dict:
        return {
            "approved": self.approved,
            "overall": self.overall,
            "comments": [
                {"file": c.file, "line": c.line, "text": c.text}
                for c in self.comments
            ],
        }

    def format_for_worker(self) -> str:
        lines = []
        verdict = "APPROVED" if self.approved else "CHANGES REQUESTED"
        lines.append(f"Review verdict: {verdict}")
        if self.overall:
            lines.append(f"Overall feedback: {self.overall}")
        if self.comments:
            lines.append("Line comments:")
            for c in self.comments:
                lines.append(f"  {c.file}:{c.line} — {c.text}")
        return "\n".join(lines)


class ReviewSession:
    """All state for an in-progress review of one worker's worktree."""

    def __init__(
        self,
        session_id: str,
        worktree: Path,
        base_commit: str,
        diff_hash: str,
    ):
        self.session_id = session_id
        self.worktree = worktree
        self.base_commit = base_commit
        self.draft = ReviewDraft(session_id=session_id, diff_hash=diff_hash)
        self._diff_cache: Optional[str] = None
        self._files_cache: Optional[list[str]] = None
        self._focused_file: Optional[str] = None
        self._focused_line: int = 0

    async def get_diff(self) -> str:
        if self._diff_cache is None:
            self._diff_cache = await full_diff(self.worktree, self.base_commit)
        return self._diff_cache

    async def get_diff_stat(self) -> str:
        return await diff_stat(self.worktree, self.base_commit)

    async def get_changed_files(self) -> list[str]:
        if self._files_cache is None:
            self._files_cache = await changed_files(self.worktree, self.base_commit)
        return self._files_cache

    def focus_file(self, path: str) -> None:
        self._focused_file = path
        self._focused_line = 0

    def focused_file(self) -> Optional[str]:
        return self._focused_file

    def read_focused_file(self) -> Optional[str]:
        if not self._focused_file:
            return None
        p = self.worktree / self._focused_file
        try:
            return p.read_text(errors="replace")
        except FileNotFoundError:
            return None

    def approve(self, overall: str = "") -> None:
        self.draft.approved = True
        self.draft.overall = overall

    def request_changes(self, overall: str = "") -> None:
        self.draft.approved = False
        self.draft.overall = overall

    def add_comment(self, file: str, line: int, text: str) -> None:
        self.draft.add_comment(file, line, text)

    def submit_payload(self) -> str:
        return self.draft.format_for_worker()
