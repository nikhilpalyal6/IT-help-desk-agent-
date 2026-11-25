"""SQLite-backed telemetry for actual IT support requests."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from uuid import uuid4


class TelemetryStore:
    """Store request outcomes without retaining prompts or response content."""

    MAX_FEEDBACK_TEXT_LENGTH = 500
    VALID_CATEGORIES = frozenset({"network", "vpn", "account", "general"})

    def __init__(self, database_path: Path | str):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS support_request_events (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
                    outcome TEXT NOT NULL CHECK(outcome IN ('agent_success', 'fallback')),
                    error_stage TEXT,
                    error_type TEXT,
                    category TEXT CHECK(
                        category IS NULL OR category IN ('network', 'vpn', 'account', 'general')
                    )
                )
                """
            )
            request_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(support_request_events)").fetchall()
            }
            if "category" not in request_columns:
                connection.execute(
                    """
                    ALTER TABLE support_request_events ADD COLUMN category TEXT CHECK(
                        category IS NULL OR category IN ('network', 'vpn', 'account', 'general')
                    )
                    """
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_support_request_events_requested_at "
                "ON support_request_events(requested_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS support_feedback (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE REFERENCES support_request_events(id),
                    resolution_status TEXT NOT NULL
                        CHECK(resolution_status IN ('resolved', 'not_resolved')),
                    feedback_text TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            feedback_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(support_feedback)").fetchall()
            }
            if "feedback_text" not in feedback_columns:
                connection.execute("ALTER TABLE support_feedback ADD COLUMN feedback_text TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_support_feedback_created_at "
                "ON support_feedback(created_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    category TEXT,
                    description TEXT,
                    priority TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tickets_created_at "
                "ON tickets(created_at)"
            )

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat()

    def record_request(
        self,
        conversation_id: str,
        requested_at: str,
        latency_ms: int,
        outcome: str,
        error_stage: str | None = None,
        error_type: str | None = None,
        category: str | None = None,
    ) -> str:
        if outcome not in {"agent_success", "fallback"}:
            raise ValueError("outcome must be 'agent_success' or 'fallback'")
        if not isinstance(latency_ms, int) or isinstance(latency_ms, bool):
            raise TypeError("latency_ms must be an integer")
        if latency_ms < 0:
            raise ValueError("latency_ms cannot be negative")
        if category is not None and category not in self.VALID_CATEGORIES:
            raise ValueError("category must be a supported fallback category or None")
        if outcome != "fallback" and category is not None:
            raise ValueError("only fallback requests can have a category")

        event_id = str(uuid4())
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO support_request_events (
                    id, conversation_id, requested_at, completed_at, latency_ms,
                    outcome, error_stage, error_type, category
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, conversation_id, requested_at, self._timestamp(), latency_ms,
                 outcome, error_stage, error_type, category),
            )
        return event_id

    def latest_request_id(self, conversation_id: str) -> str | None:
        """Return the newest request event for a loaded conversation."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM support_request_events
                WHERE conversation_id = ?
                ORDER BY requested_at DESC, completed_at DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return None if row is None else row["id"]

    @classmethod
    def _normalize_feedback_text(cls, feedback_text: str | None) -> str | None:
        if feedback_text is None:
            return None
        if not isinstance(feedback_text, str):
            raise TypeError("feedback_text must be a string or None")
        normalized = feedback_text.strip()
        if not normalized:
            return None
        if len(normalized) > cls.MAX_FEEDBACK_TEXT_LENGTH:
            raise ValueError(f"feedback_text cannot exceed {cls.MAX_FEEDBACK_TEXT_LENGTH} characters")
        return normalized

    def record_feedback(
        self,
        request_id: str,
        resolution_status: str,
        feedback_text: str | None = None,
    ) -> str:
        """Persist one explicit user resolution response for a request event."""
        if resolution_status not in {"resolved", "not_resolved"}:
            raise ValueError("resolution_status must be 'resolved' or 'not_resolved'")
        feedback_text = self._normalize_feedback_text(feedback_text)

        feedback_id = str(uuid4())
        with self._connection() as connection:
            request_exists = connection.execute(
                "SELECT 1 FROM support_request_events WHERE id = ?", (request_id,)
            ).fetchone()
            if request_exists is None:
                raise ValueError("request_id does not identify a support request")
            existing_feedback = connection.execute(
                "SELECT 1 FROM support_feedback WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing_feedback is not None:
                raise ValueError("feedback has already been recorded for this request")
            try:
                connection.execute(
                    """
                    INSERT INTO support_feedback (
                        id, request_id, resolution_status, feedback_text, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (feedback_id, request_id, resolution_status, feedback_text, self._timestamp()),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("feedback has already been recorded for this request") from error
        return feedback_id

    def feedback_for_request(self, request_id: str) -> dict[str, str] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, request_id, resolution_status, feedback_text, created_at
                FROM support_feedback WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        return None if row is None else dict(row)

    @staticmethod
    def _filter_clause(start_at: str | None) -> tuple[str, tuple[str, ...]]:
        if start_at is None:
            return "", ()
        return " WHERE requested_at >= ?", (start_at,)

    def summary(self, start_at: str | None = None) -> dict[str, int | float | None]:
        where_clause, parameters = self._filter_clause(start_at)
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total_requests,
                    COALESCE(SUM(outcome = 'agent_success'), 0) AS successful_requests,
                    COALESCE(SUM(outcome = 'fallback'), 0) AS failed_requests,
                    AVG(latency_ms) AS average_latency_ms
                FROM support_request_events{where_clause}
                """,
                parameters,
            ).fetchone()
        summary = dict(row)
        total = summary["total_requests"]
        summary["agent_success_rate"] = (
            None if not total else summary["successful_requests"] / total * 100
        )
        summary["p95_latency_ms"] = self.p95_latency_ms(start_at)
        return summary

    def p95_latency_ms(self, start_at: str | None = None) -> int | None:
        """Return nearest-rank P95 latency without requiring an analytics dependency."""
        where_clause, parameters = self._filter_clause(start_at)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT latency_ms FROM support_request_events{where_clause} ORDER BY latency_ms ASC",
                parameters,
            ).fetchall()
        if not rows:
            return None
        return rows[ceil(len(rows) * 0.95) - 1]["latency_ms"]

    def volume_by_day(self, start_at: str | None = None) -> list[dict[str, int | str]]:
        where_clause, parameters = self._filter_clause(start_at)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT substr(requested_at, 1, 10) AS day, COUNT(*) AS requests
                FROM support_request_events{where_clause} GROUP BY day ORDER BY day ASC
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def failure_breakdown(self, start_at: str | None = None) -> list[dict[str, int | float | str | None]]:
        where_clause, parameters = self._filter_clause(start_at)
        failure_clause = " WHERE outcome = 'fallback'"
        if where_clause:
            failure_clause += " AND requested_at >= ?"
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT error_stage, error_type, COUNT(*) AS errors,
                    AVG(latency_ms) AS average_response_latency_ms
                FROM support_request_events{failure_clause}
                GROUP BY error_stage, error_type ORDER BY errors DESC, error_type ASC
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def fallback_category_distribution(self, start_at: str | None = None) -> list[dict[str, int | str]]:
        """Return categories selected by local fallback troubleshooting only."""
        if start_at is None:
            where_clause, parameters = " WHERE outcome = 'fallback' AND category IS NOT NULL", ()
        else:
            where_clause = " WHERE outcome = 'fallback' AND category IS NOT NULL AND requested_at >= ?"
            parameters = (start_at,)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT category, COUNT(*) AS requests
                FROM support_request_events{where_clause}
                GROUP BY category ORDER BY requests DESC, category ASC
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def feedback_summary(self, start_at: str | None = None) -> dict[str, int | float | None]:
        """Summarize explicit feedback submitted during the selected period."""
        if start_at is None:
            where_clause, parameters = "", ()
        else:
            where_clause, parameters = " WHERE created_at >= ?", (start_at,)
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS feedback_responses,
                    COALESCE(SUM(resolution_status = 'resolved'), 0) AS resolved_requests,
                    COALESCE(SUM(resolution_status = 'not_resolved'), 0) AS not_resolved_requests
                FROM support_feedback{where_clause}
                """,
                parameters,
            ).fetchone()
        summary = dict(row)
        total = summary["feedback_responses"]
        summary["user_confirmed_resolution_rate"] = (
            None if not total else summary["resolved_requests"] / total * 100
        )
        return summary

    CATEGORY_LABELS = {
        "network": "Network / Wi-Fi",
        "vpn": "VPN",
        "account": "Password / Account",
        "general": "General / Other",
        "printer": "Printer",
        "email": "Email",
        "software": "Software Installation",
    }

    def conversation_count(self, start_at: str | None = None) -> int:
        """Count distinct support conversations active or created in the selected period."""
        with self._connection() as connection:
            has_conv = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'conversations'"
            ).fetchone() is not None

            if has_conv:
                if start_at is None:
                    query = """
                        SELECT COUNT(DISTINCT id) AS count FROM (
                            SELECT conversation_id AS id FROM support_request_events
                            UNION
                            SELECT id FROM conversations
                        )
                    """
                    row = connection.execute(query).fetchone()
                else:
                    query = """
                        SELECT COUNT(DISTINCT id) AS count FROM (
                            SELECT conversation_id AS id FROM support_request_events WHERE requested_at >= ?
                            UNION
                            SELECT id FROM conversations WHERE created_at >= ?
                        )
                    """
                    row = connection.execute(query, (start_at, start_at)).fetchone()
                return row["count"] if row else 0

            where_clause, parameters = self._filter_clause(start_at)
            row = connection.execute(
                f"SELECT COUNT(DISTINCT conversation_id) AS count FROM support_request_events{where_clause}",
                parameters,
            ).fetchone()
            return row["count"] if row else 0

    def ticket_count(self, start_at: str | None = None) -> int:
        """Return total count of support tickets in the selected period."""
        with self._connection() as connection:
            has_tickets = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tickets'"
            ).fetchone() is not None
            if not has_tickets:
                return 0
            if start_at is None:
                row = connection.execute("SELECT COUNT(*) AS count FROM tickets").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM tickets WHERE created_at >= ?", (start_at,)
                ).fetchone()
            return row["count"] if row else 0

    def ticket_summary(self, start_at: str | None = None) -> dict[str, int]:
        """Return ticket counts grouped by status in the selected period."""
        result = {"total_tickets": 0, "open_tickets": 0, "resolved_tickets": 0, "escalated_tickets": 0}
        with self._connection() as connection:
            has_tickets = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tickets'"
            ).fetchone() is not None
            if not has_tickets:
                return result
            where_clause = "" if start_at is None else " WHERE created_at >= ?"
            params = () if start_at is None else (start_at,)
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total_tickets,
                    COALESCE(SUM(status = 'open'), 0) AS open_tickets,
                    COALESCE(SUM(status = 'resolved'), 0) AS resolved_tickets,
                    COALESCE(SUM(status = 'escalated'), 0) AS escalated_tickets
                FROM tickets{where_clause}
                """,
                params,
            ).fetchone()
            if row:
                result = dict(row)
        return result

    def resolution_vs_escalation(self, start_at: str | None = None) -> dict[str, int | float | None]:
        """Compare resolved versus escalated support cases."""
        feedback = self.feedback_summary(start_at)
        resolved = feedback["resolved_requests"]
        escalated = feedback["not_resolved_requests"]

        tickets = self.ticket_summary(start_at)
        resolved += tickets.get("resolved_tickets", 0)
        escalated += tickets.get("escalated_tickets", 0)

        total = resolved + escalated
        res_rate = None if total == 0 else round(resolved / total * 100, 1)
        esc_rate = None if total == 0 else round(escalated / total * 100, 1)

        return {
            "resolved_count": resolved,
            "escalated_count": escalated,
            "total_cases": total,
            "resolution_rate": res_rate,
            "escalation_rate": esc_rate,
        }

    def category_distribution(self, start_at: str | None = None) -> list[dict[str, int | str]]:
        """Return distribution of issue categories across recorded requests and tickets."""
        where_clause, parameters = self._filter_clause(start_at)
        event_filter = " WHERE category IS NOT NULL"
        if where_clause:
            event_filter += " AND requested_at >= ?"

        with self._connection() as connection:
            has_tickets = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tickets'"
            ).fetchone() is not None

            if has_tickets:
                ticket_filter = " WHERE category IS NOT NULL"
                ticket_params = ()
                if start_at is not None:
                    ticket_filter += " AND created_at >= ?"
                    ticket_params = (start_at,)
                query = f"""
                    SELECT category, COUNT(*) AS requests
                    FROM (
                        SELECT category FROM support_request_events{event_filter}
                        UNION ALL
                        SELECT category FROM tickets{ticket_filter}
                    )
                    GROUP BY category ORDER BY requests DESC, category ASC
                """
                rows = connection.execute(query, parameters + ticket_params).fetchall()
            else:
                rows = connection.execute(
                    f"""
                    SELECT category, COUNT(*) AS requests
                    FROM support_request_events{event_filter}
                    GROUP BY category ORDER BY requests DESC, category ASC
                    """,
                    parameters,
                ).fetchall()

        result = []
        for row in rows:
            cat = row["category"]
            label = self.CATEGORY_LABELS.get(cat, cat.replace("_", " ").title())
            result.append({"category": cat, "label": label, "requests": row["requests"]})
        return result

    def most_common_category(self, start_at: str | None = None) -> dict[str, str | int | float] | None:
        """Return the most common IT issue category, or None if no category data exists."""
        dist = self.category_distribution(start_at)
        if not dist:
            return None
        total = sum(item["requests"] for item in dist)
        top = dist[0]
        pct = round(top["requests"] / total * 100, 1) if total > 0 else 0.0
        return {
            "category": top["category"],
            "label": top["label"],
            "requests": top["requests"],
            "percentage": pct,
        }

    def usage_trends(self, start_at: str | None = None) -> list[dict[str, int | str]]:
        """Return daily usage trends over time including requests and conversations."""
        where_clause, parameters = self._filter_clause(start_at)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT substr(requested_at, 1, 10) AS day,
                    COUNT(*) AS requests,
                    COUNT(DISTINCT conversation_id) AS conversations
                FROM support_request_events{where_clause}
                GROUP BY day ORDER BY day ASC
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]
