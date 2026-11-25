"""SQLite-backed storage for Helpdesk chat conversations."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class ConversationStore:
    """Persist conversations independently from Streamlit's session state."""

    def __init__(self, database_path: Path | str):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    UNIQUE(conversation_id, position)
                );
                """
            )

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat()

    def create_conversation(self, title: str = "New conversation") -> str:
        conversation_id = str(uuid4())
        timestamp = self._timestamp()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (conversation_id, title, timestamp, timestamp),
            )
        return conversation_id

    def list_conversations(self) -> list[dict[str, str]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, title, created_at, updated_at FROM conversations "
                "WHERE EXISTS (SELECT 1 FROM messages WHERE messages.conversation_id = conversations.id) "
                "ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def load_messages(self, conversation_id: str) -> list[dict[str, str]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT role, content, created_at FROM messages WHERE conversation_id = ? "
                "ORDER BY position ASC",
                (conversation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def append_message(self, conversation_id: str, role: str, content: str) -> dict[str, str]:
        timestamp = self._timestamp()
        with self._connection() as connection:
            position = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation_id,)
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at, position) "
                "VALUES (?, ?, ?, ?, ?)",
                (conversation_id, role, content, timestamp, position),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (timestamp, conversation_id)
            )
            if role == "user" and position == 0:
                connection.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?", (content[:60], conversation_id)
                )
        return {"role": role, "content": content, "created_at": timestamp}

    def get_recent_user_questions(self, limit: int = 50) -> list[dict[str, str]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, conversation_id, content AS question, created_at AS timestamp "
                "FROM messages WHERE role = 'user' "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
