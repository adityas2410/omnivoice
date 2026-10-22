"""Local, explicitly retained conversations; no provider traces or UI snapshots."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from pydantic_ai.messages import (
    ModelMessage, ModelMessagesTypeAdapter, ModelRequest, ModelResponse,
    TextPart, UserPromptPart,
)

from omnivoice.actions import ActionPlan, format_action_plan


class HistoryError(RuntimeError):
    """Safe user-facing session error."""


class HistoryUnavailable(HistoryError):
    """Storage cannot currently guarantee persistence."""


def default_history_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "OmniVoice" / "history.sqlite3"


def safe_text(value: str, *, multiline: bool = True) -> str:
    """Keep line breaks but escape terminal controls, including bidi controls."""
    return "".join(
        c if (c == "\n" and multiline) or not unicodedata.category(c).startswith("C")
        else c.encode("unicode_escape").decode("ascii")
        for c in value
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ChatSession:
    id: str
    name: str
    created_at: str
    updated_at: str
    preview: str = ""


def turn_messages(turn: dict[str, Any]) -> list[ModelMessage]:
    """Construct provider-neutral history, never the captured automatic context."""
    prompt = json.dumps({
        "request": turn["request"], "selected_text": turn["selected_text"],
        "target_context": None, "ui_context": None,
    }, ensure_ascii=False)
    messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(prompt)])]
    if turn["plan_json"] is not None:
        messages.append(ModelResponse(parts=[TextPart(turn["plan_json"])]))
    result = json.dumps({"execution_result": {
        "status": turn["status"], "failure_category": turn["failure_category"],
        "completed_actions": turn["completed_actions"],
        "possibly_partial": bool(turn["possibly_partial"]),
    }})
    messages.append(ModelRequest(parts=[UserPromptPart(result)]))
    return messages


def render_turn(turn: dict[str, Any]) -> str:
    """One renderer for live results and full transcript replay."""
    lines = [f"\nYou:\n{safe_text(turn['request'])}"]
    if turn["selected_text"] is not None:
        lines.append(f"Selected context:\n{safe_text(turn['selected_text'])}")
    if turn["plan_json"] is not None:
        plan = ActionPlan.model_validate_json(turn["plan_json"])
        lines.append("AI:")
        for action in plan.actions:
            if action.type == "shortcut":
                lines.append("Shortcut: " + safe_text("+".join(action.keys)))
            else:
                label = "Replace selection" if action.type == "replace_selection" else "Insert text"
                lines.append(f"{label}:\n{safe_text(action.text)}")
        if plan.spoken_summary:
            lines.append("Summary: " + safe_text(plan.spoken_summary))
        if not plan.actions:
            lines.append("No permitted actions proposed.")
    result = turn["status"]
    if turn["possibly_partial"]:
        result += " (possibly partial input)"
    if turn["failure_category"]:
        result += f" [{turn['failure_category']}]"
    lines.append(f"Result: {safe_text(result)}; completed actions: {safe_text(str(turn['completed_actions']))}\n")
    return "\n".join(lines)


class ChatHistory:
    """Serialize all SQLite and process-lock access on one owned thread."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_history_path()
        self.active: ChatSession | None = None
        self.healthy = False
        self.busy = False
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="omnivoice-history")
        self._connection: sqlite3.Connection | None = None
        self._lock_file: Any = None
        self._pending: tuple[int, str, str | None, int, bool, str | None] | None = None
        self._closed = False

    def describe_status(self) -> str:
        active = self.active
        return (f"chat={safe_text(active.name, multiline=False)} ({active.id})" if active else "chat=unavailable") + (
            f", history={'ready' if self.healthy else 'unavailable'}, history_path={self.path}"
        )

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return await asyncio.get_running_loop().run_in_executor(self._worker, fn, *args)
        except HistoryUnavailable:
            self.healthy = False
            raise
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
            self.healthy = False
            raise HistoryUnavailable("Chat storage unavailable; AI actions paused. Use /chat retry.") from exc

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise HistoryUnavailable("Chat storage unavailable; use /chat retry.")
        return self._connection

    def _lock(self) -> None:
        if self._lock_file is not None:
            return
        handle = self.path.with_suffix(".lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise HistoryUnavailable("Another OmniVoice instance owns chat storage; AI actions paused.") from exc
        self._lock_file = handle

    def _initialize(self) -> ChatSession:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock()
        if self._connection is None:
            db = sqlite3.connect(self.path, timeout=0.5)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA foreign_keys=ON")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise HistoryUnavailable("Unsupported chat database version; AI actions paused.")
                with db:
                    if version == 0:
                        db.execute("BEGIN IMMEDIATE")
                        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
                        db.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                        db.execute("""CREATE TABLE turns (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                            request TEXT NOT NULL, selected_text TEXT, model TEXT NOT NULL,
                            plan_json TEXT, messages_json TEXT NOT NULL,
                            status TEXT NOT NULL, failure_category TEXT,
                            completed_actions INTEGER NOT NULL DEFAULT 0,
                            possibly_partial INTEGER NOT NULL DEFAULT 0,
                            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
                        db.execute("CREATE INDEX turns_session ON turns(session_id, id)")
                        db.execute("PRAGMA user_version=1")
                    # Also validate existing schema before recovery or hotkey use.
                    db.execute("SELECT id, name, created_at, updated_at FROM sessions LIMIT 1")
                    db.execute("SELECT id,session_id,request,selected_text,model,plan_json,messages_json,status,failure_category,completed_actions,possibly_partial,created_at,updated_at FROM turns LIMIT 1")
                    for row in db.execute("SELECT * FROM turns WHERE status IN ('planning','executing')").fetchall():
                        turn = dict(row)
                        turn.update(status="interrupted", failure_category=(
                            "execution_unknown" if row["status"] == "executing" else "interrupted_before_execution"
                        ), possibly_partial=int(row["status"] == "executing"))
                        db.execute("UPDATE turns SET status=?, failure_category=?, possibly_partial=?, messages_json=?, updated_at=? WHERE id=?", (
                            turn["status"], turn["failure_category"], turn["possibly_partial"],
                            ModelMessagesTypeAdapter.dump_json(turn_messages(turn)).decode(), _now(), row["id"],
                        ))
                self._connection = db
            except BaseException:
                db.close()
                raise
        db = self._db()
        db.execute("SELECT id FROM turns LIMIT 1")
        row = db.execute("SELECT s.* FROM sessions s JOIN app_state a ON a.value=s.id WHERE a.key='active_session'").fetchone()
        if row is None:
            row = db.execute("SELECT * FROM sessions ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
        if row is None:
            return self._create("New chat")
        return self._activate(row["id"])

    async def start(self) -> None:
        try:
            self.active = await self._call(self._initialize)
            self.healthy = True
        except HistoryError:
            self.healthy = False
            raise

    async def retry(self) -> None:
        await self.start()
        if self._pending is not None:
            await self._call(self._finish, *self._pending)
            self._pending = None
        self.healthy = True

    @staticmethod
    def _name(name: str) -> str:
        name = name.strip()
        if not name or len(name) > 120 or any(unicodedata.category(c).startswith("C") for c in name):
            raise HistoryError("Chat names must contain 1–120 characters without control characters.")
        return name

    def _create(self, name: str) -> ChatSession:
        name = self._name(name)
        ident, now = str(uuid4()), _now()
        db = self._db()
        with db:
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (ident, name, now, now))
            db.execute("INSERT OR REPLACE INTO app_state VALUES ('active_session',?)", (ident,))
        return ChatSession(ident, name, now, now)

    async def create(self, name: str = "New chat") -> ChatSession:
        self.active = await self._call(self._create, name)
        return self.active

    async def list_sessions(self) -> list[ChatSession]:
        def read() -> list[ChatSession]:
            return [ChatSession(**dict(row)) for row in self._db().execute("""SELECT s.*,
                COALESCE((SELECT substr(request,1,120) FROM turns WHERE session_id=s.id ORDER BY id DESC LIMIT 1),'') AS preview
                FROM sessions s ORDER BY updated_at DESC, id DESC""")]
        return await self._call(read)

    def _resolve(self, prefix: str) -> sqlite3.Row:
        # Match in Python so SQL wildcard input cannot select unrelated sessions.
        rows = [r for r in self._db().execute("SELECT * FROM sessions") if r["id"].startswith(prefix)] if len(prefix) >= 8 else []
        if len(rows) != 1:
            raise HistoryError("Unknown or ambiguous chat ID; use /chats and at least eight ID characters.")
        return rows[0]

    async def resolve(self, prefix: str) -> ChatSession:
        return await self._call(lambda: ChatSession(**dict(self._resolve(prefix))))

    def _activate(self, prefix: str) -> ChatSession:
        row = self._resolve(prefix)
        db = self._db()
        with db:
            db.execute("INSERT OR REPLACE INTO app_state VALUES ('active_session',?)", (row["id"],))
        return ChatSession(**dict(row))

    async def activate(self, prefix: str) -> ChatSession:
        self.active = await self._call(self._activate, prefix)
        return self.active

    async def rename(self, name: str) -> None:
        name = self._name(name)
        if self.active is None:
            raise HistoryError("No active chat.")
        ident = self.active.id
        def update() -> ChatSession:
            db = self._db()
            with db:
                db.execute("UPDATE sessions SET name=? WHERE id=?", (name, ident))
            return ChatSession(**dict(self._resolve(ident)))
        self.active = await self._call(update)

    async def delete(self, prefix: str) -> bool:
        def remove() -> tuple[bool, ChatSession | None]:
            row = self._resolve(prefix)
            active_deleted = self.active is not None and self.active.id == row["id"]
            db = self._db()
            with db:
                db.execute("DELETE FROM sessions WHERE id=?", (row["id"],))
                if not active_deleted:
                    return False, self.active
                remaining = db.execute("SELECT id FROM sessions ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
                if remaining:
                    db.execute("INSERT OR REPLACE INTO app_state VALUES ('active_session',?)", (remaining["id"],))
                else:
                    ident, now = str(uuid4()), _now()
                    db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (ident, "New chat", now, now))
                    db.execute("INSERT OR REPLACE INTO app_state VALUES ('active_session',?)", (ident,))
            return True, ChatSession(**dict(self._resolve(remaining["id"] if remaining else ident)))
        changed, self.active = await self._call(remove)
        return changed

    async def turns(self, session_id: str, *, after: int = 0, before: int | None = None, limit: int = 50, newest: bool = False) -> list[dict[str, Any]]:
        def read() -> list[dict[str, Any]]:
            sql = "SELECT * FROM turns WHERE session_id=? AND id>?"
            params: list[Any] = [session_id, after]
            if before is not None:
                sql += " AND id<?"
                params.append(before)
            sql += " ORDER BY id " + ("DESC" if newest else "ASC") + " LIMIT ?"
            params.append(limit)
            rows = [dict(row) for row in self._db().execute(sql, params)]
            for row in rows:
                ModelMessagesTypeAdapter.validate_json(row["messages_json"])
                if row["plan_json"] is not None:
                    ActionPlan.model_validate_json(row["plan_json"])
            return rows
        return await self._call(read)

    async def begin(self, session_id: str, request: str, selected_text: str | None, model: str) -> int:
        def insert() -> int:
            now, db = _now(), self._db()
            with db:
                cursor = db.execute("""INSERT INTO turns
                    (session_id,request,selected_text,model,messages_json,status,created_at,updated_at)
                    VALUES (?,?,?,?,'[]','planning',?,?)""", (session_id, request, selected_text, model, now, now))
                db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            return int(cursor.lastrowid)
        return await self._call(insert)

    async def save_plan(self, turn_id: int, plan: ActionPlan) -> None:
        def update() -> None:
            db = self._db()
            with db:
                db.execute("UPDATE turns SET plan_json=?,status=?,updated_at=? WHERE id=?", (
                    format_action_plan(plan), "executing" if plan.actions else "planning", _now(), turn_id,
                ))
        await self._call(update)

    def _finish(self, turn_id: int, status: str, category: str | None, completed: int, partial: bool, plan_json: str | None = None) -> dict[str, Any]:
        db = self._db()
        turn = dict(db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone())
        if plan_json is not None:
            turn["plan_json"] = plan_json
        turn.update(status=status, failure_category=category, completed_actions=completed, possibly_partial=int(partial), updated_at=_now())
        turn["messages_json"] = ModelMessagesTypeAdapter.dump_json(turn_messages(turn)).decode()
        with db:
            db.execute("""UPDATE turns SET status=?,failure_category=?,completed_actions=?,possibly_partial=?,messages_json=?,plan_json=?,updated_at=? WHERE id=?""", (
                status, category, completed, int(partial), turn["messages_json"], turn["plan_json"], turn["updated_at"], turn_id,
            ))
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (turn["updated_at"], turn["session_id"]))
        return turn

    async def finish(self, turn_id: int, status: str, category: str | None, completed: int, partial: bool, plan: ActionPlan | None = None) -> dict[str, Any]:
        self._pending = (turn_id, status, category, completed, partial, format_action_plan(plan) if plan else None)
        turn = await self._call(self._finish, *self._pending)
        self._pending = None
        return turn

    async def message_history(self, session_id: str, before: int, token_budget: int) -> tuple[list[ModelMessage], bool]:
        """Read backward in bounded batches, keeping complete turns only."""
        groups: list[list[ModelMessage]] = []
        remaining, cursor = token_budget, before
        while True:
            rows = await self.turns(session_id, before=cursor, newest=True)
            if not rows:
                return [m for group in reversed(groups) for m in group], False
            for row in rows:
                messages = ModelMessagesTypeAdapter.validate_json(row["messages_json"])
                cost = (len(ModelMessagesTypeAdapter.dump_json(messages)) + 2) // 3
                if cost > remaining:
                    return [m for group in reversed(groups) for m in group], True
                groups.append(messages)
                remaining -= cost
                cursor = row["id"]

    def _close(self) -> None:
        try:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
        finally:
            if self._lock_file is not None:
                if os.name == "nt":
                    import msvcrt
                    self._lock_file.seek(0)
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                self._lock_file.close()
                self._lock_file = None

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(self._worker, self._close)
        finally:
            self.healthy = False
            self._closed = True
            self._worker.shutdown(wait=True)
