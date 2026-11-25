import ast
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from history_store import ConversationStore
from telemetry_store import TelemetryStore


def fallback_response_for_test(prompt):
    """Execute the local fallback function without initializing the Streamlit app."""
    source = Path("app.py").read_text(encoding="utf-8")
    function = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "local_troubleshooting_response"
    )
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = {}
    exec(compile(module, "app.py", "exec"), namespace)
    return namespace["local_troubleshooting_response"](prompt)


class AppSmokeTest(unittest.TestCase):
    def test_app_parses_and_uses_existing_agent(self):
        source = Path("app.py").read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn('AGENT_NAME = "IT-Helpdesk-Agent"', source)
        self.assertIn("DefaultAzureCredential()", source)
        self.assertIn("FoundryAgent(", source)
        self.assertIn("agent_version=AGENT_VERSION", source)
        self.assertIn("prompt_with_saved_context", source)
        self.assertIn("Continue the following IT helpdesk conversation", source)
        self.assertIn("local_troubleshooting_response", source)
        self.assertIn("IT support ready", source)
        self.assertNotIn('st.error(f"Could not reach the helpdesk agent: {error}")', source)
        self.assertNotIn("create_agent", source)
        self.assertIn("TelemetryStore", source)
        self.assertIn("record_request(", source)
        self.assertIn("IT Support Analytics Dashboard", source)
        self.assertIn("Resolved vs Escalated Issues", source)
        self.assertIn("Issue Categories", source)
        self.assertIn("Usage Trends Over Time", source)

    def test_conversations_survive_reopening_and_preserve_message_order(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "history.db"
            first_store = ConversationStore(database)
            first = first_store.create_conversation()
            second = first_store.create_conversation()
            first_store.append_message(first, "user", "Wi-Fi is unavailable")
            first_store.append_message(first, "assistant", "Check airplane mode first.")
            first_store.append_message(second, "user", "VPN is disconnected")

            reopened_store = ConversationStore(database)
            self.assertEqual(
                [message["content"] for message in reopened_store.load_messages(first)],
                ["Wi-Fi is unavailable", "Check airplane mode first."],
            )
            self.assertEqual(len(reopened_store.list_conversations()), 2)
            self.assertEqual(reopened_store.load_messages(second)[0]["content"], "VPN is disconnected")

    def test_telemetry_records_real_outcomes_and_aggregates_them(self):
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")
            telemetry.record_request("conversation-1", "2026-09-20T09:00:00+00:00", 120, "agent_success")
            telemetry.record_request(
                "conversation-1",
                "2026-09-21T09:00:00+00:00",
                360,
                "fallback",
                error_stage="foundry_agent_run",
                error_type="TimeoutError",
            )

            summary = telemetry.summary()
            self.assertEqual(summary["total_requests"], 2)
            self.assertEqual(summary["successful_requests"], 1)
            self.assertEqual(summary["failed_requests"], 1)
            self.assertEqual(summary["average_latency_ms"], 240)
            self.assertEqual(summary["p95_latency_ms"], 360)
            self.assertEqual(summary["agent_success_rate"], 50.0)
            self.assertEqual(
                telemetry.volume_by_day(),
                [{"day": "2026-09-20", "requests": 1}, {"day": "2026-09-21", "requests": 1}],
            )
            self.assertEqual(
                telemetry.failure_breakdown(),
                [{
                    "error_stage": "foundry_agent_run",
                    "error_type": "TimeoutError",
                    "errors": 1,
                    "average_response_latency_ms": 360.0,
                }],
            )

    def test_telemetry_calculates_nearest_rank_p95_latency(self):
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")
            for latency in range(1, 21):
                telemetry.record_request(
                    "conversation-1",
                    "2026-09-20T09:00:00+00:00",
                    latency,
                    "agent_success",
                )

            self.assertEqual(telemetry.p95_latency_ms(), 19)

    def test_telemetry_filters_all_analytics_by_start_time(self):
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")
            telemetry.record_request("older", "2026-09-20T09:00:00+00:00", 100, "agent_success")
            telemetry.record_request(
                "recent",
                "2026-09-21T09:00:00+00:00",
                300,
                "fallback",
                error_stage="foundry_agent_run",
                error_type="TimeoutError",
            )

            summary = telemetry.summary("2026-09-21T00:00:00+00:00")
            self.assertEqual(summary["total_requests"], 1)
            self.assertEqual(summary["successful_requests"], 0)
            self.assertEqual(summary["failed_requests"], 1)
            self.assertEqual(summary["average_latency_ms"], 300)
            self.assertEqual(summary["p95_latency_ms"], 300)
            self.assertEqual(summary["agent_success_rate"], 0.0)
            self.assertEqual(
                telemetry.volume_by_day("2026-09-21T00:00:00+00:00"),
                [{"day": "2026-09-21", "requests": 1}],
            )
            self.assertEqual(
                telemetry.failure_breakdown("2026-09-21T00:00:00+00:00")[0]["errors"],
                1,
            )

    def test_empty_telemetry_has_safe_analytics_values(self):
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")

            self.assertEqual(
                telemetry.summary(),
                {
                    "total_requests": 0,
                    "successful_requests": 0,
                    "failed_requests": 0,
                    "average_latency_ms": None,
                    "agent_success_rate": None,
                    "p95_latency_ms": None,
                },
            )
            self.assertEqual(telemetry.volume_by_day(), [])
            self.assertEqual(telemetry.failure_breakdown(), [])

    def test_telemetry_rejects_invalid_outcomes_and_latencies(self):
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")
            with self.assertRaises(ValueError):
                telemetry.record_request("conversation-1", "2026-09-20T09:00:00+00:00", 1, "resolved")
            with self.assertRaises(ValueError):
                telemetry.record_request("conversation-1", "2026-09-20T09:00:00+00:00", -1, "agent_success")

    def test_local_fallback_categories_are_stored_for_fallback_requests(self):
        expected_categories = {
            "Wi-Fi has no internet connection": "network",
            "My VPN cannot connect": "vpn",
            "I need to reset my password": "account",
            "My application is frozen": "general",
        }
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")
            for prompt, expected_category in expected_categories.items():
                response, category = fallback_response_for_test(prompt)
                self.assertIn("Microsoft Foundry is currently unavailable", response)
                self.assertEqual(category, expected_category)
                request_id = telemetry.record_request(
                    "conversation-1",
                    "2026-09-20T09:00:00+00:00",
                    100,
                    "fallback",
                    category=category,
                )
                with telemetry._connection() as connection:
                    stored_category = connection.execute(
                        "SELECT category FROM support_request_events WHERE id = ?", (request_id,)
                    ).fetchone()["category"]
                self.assertEqual(stored_category, expected_category)

    def test_successful_requests_have_no_category_and_invalid_categories_are_rejected(self):
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")
            request_id = telemetry.record_request(
                "conversation-1", "2026-09-20T09:00:00+00:00", 100, "agent_success"
            )
            with telemetry._connection() as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT category FROM support_request_events WHERE id = ?", (request_id,)
                    ).fetchone()["category"]
                )
            with self.assertRaises(ValueError):
                telemetry.record_request(
                    "conversation-1", "2026-09-20T09:00:00+00:00", 100, "fallback", category="email"
                )
            with self.assertRaises(ValueError):
                telemetry.record_request(
                    "conversation-1", "2026-09-20T09:00:00+00:00", 100, "agent_success", category="network"
                )

    def test_fallback_category_analytics_aggregate_and_filter_by_time(self):
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")
            telemetry.record_request(
                "conversation-1", "2026-09-20T09:00:00+00:00", 100, "fallback", category="network"
            )
            telemetry.record_request(
                "conversation-1", "2026-09-21T09:00:00+00:00", 100, "fallback", category="network"
            )
            telemetry.record_request(
                "conversation-1", "2026-09-21T09:01:00+00:00", 100, "fallback", category="vpn"
            )
            telemetry.record_request(
                "conversation-1", "2026-09-21T09:02:00+00:00", 100, "agent_success"
            )

            self.assertEqual(
                telemetry.fallback_category_distribution(),
                [{"category": "network", "requests": 2}, {"category": "vpn", "requests": 1}],
            )
            self.assertEqual(
                telemetry.fallback_category_distribution("2026-09-21T00:00:00+00:00"),
                [{"category": "network", "requests": 1}, {"category": "vpn", "requests": 1}],
            )

    def test_resolved_feedback_is_linked_to_the_exact_request(self):
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")
            first_request = telemetry.record_request(
                "conversation-1", "2026-09-20T09:00:00+00:00", 100, "agent_success"
            )
            second_request = telemetry.record_request(
                "conversation-1", "2026-09-20T09:01:00+00:00", 100, "agent_success"
            )

            telemetry.record_feedback(first_request, "resolved")

            self.assertEqual(
                telemetry.feedback_for_request(first_request)["resolution_status"], "resolved"
            )
            self.assertIsNone(telemetry.feedback_for_request(first_request)["feedback_text"])
            self.assertIsNone(telemetry.feedback_for_request(second_request))

    def test_not_resolved_feedback_is_recorded(self):
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")
            request_id = telemetry.record_request(
                "conversation-1", "2026-09-20T09:00:00+00:00", 100, "agent_success"
            )

            telemetry.record_feedback(request_id, "not_resolved")

            self.assertEqual(
                telemetry.feedback_for_request(request_id)["resolution_status"], "not_resolved"
            )
            self.assertIsNone(telemetry.feedback_for_request(request_id)["feedback_text"])

    def test_feedback_comments_are_stored_for_both_resolution_states(self):
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")
            resolved_request = telemetry.record_request(
                "conversation-1", "2026-09-20T09:00:00+00:00", 100, "agent_success"
            )
            unresolved_request = telemetry.record_request(
                "conversation-1", "2026-09-20T09:01:00+00:00", 100, "agent_success"
            )

            telemetry.record_feedback(resolved_request, "resolved", "  Fixed after reconnecting.  ")
            telemetry.record_feedback(unresolved_request, "not_resolved", "VPN still disconnects.")

            self.assertEqual(
                telemetry.feedback_for_request(resolved_request)["feedback_text"],
                "Fixed after reconnecting.",
            )
            self.assertEqual(
                telemetry.feedback_for_request(unresolved_request)["feedback_text"],
                "VPN still disconnects.",
            )

    def test_blank_feedback_comments_are_omitted_and_long_comments_are_rejected(self):
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")
            blank_request = telemetry.record_request(
                "conversation-1", "2026-09-20T09:00:00+00:00", 100, "agent_success"
            )
            long_request = telemetry.record_request(
                "conversation-1", "2026-09-20T09:01:00+00:00", 100, "agent_success"
            )

            telemetry.record_feedback(blank_request, "resolved", "   \n\t  ")
            self.assertIsNone(telemetry.feedback_for_request(blank_request)["feedback_text"])
            with self.assertRaises(ValueError):
                telemetry.record_feedback(
                    long_request,
                    "not_resolved",
                    "x" * (TelemetryStore.MAX_FEEDBACK_TEXT_LENGTH + 1),
                )

    def test_feedback_rejects_invalid_values_and_duplicates(self):
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")
            request_id = telemetry.record_request(
                "conversation-1", "2026-09-20T09:00:00+00:00", 100, "agent_success"
            )

            with self.assertRaises(ValueError):
                telemetry.record_feedback(request_id, "maybe_resolved")
            telemetry.record_feedback(request_id, "resolved", "It worked.")
            with self.assertRaises(ValueError):
                telemetry.record_feedback(request_id, "not_resolved")

    def test_feedback_summary_calculates_resolution_rate_and_filters_by_feedback_time(self):
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")
            resolved_request = telemetry.record_request(
                "conversation-1", "2026-09-20T09:00:00+00:00", 100, "agent_success"
            )
            unresolved_request = telemetry.record_request(
                "conversation-1", "2026-09-20T09:01:00+00:00", 100, "agent_success"
            )
            telemetry.record_feedback(resolved_request, "resolved", "Resolved quickly.")
            telemetry.record_feedback(unresolved_request, "not_resolved")

            self.assertEqual(
                telemetry.feedback_summary(),
                {
                    "feedback_responses": 2,
                    "resolved_requests": 1,
                    "not_resolved_requests": 1,
                    "user_confirmed_resolution_rate": 50.0,
                },
            )
            self.assertEqual(
                telemetry.feedback_summary("9999-01-01T00:00:00+00:00"),
                {
                    "feedback_responses": 0,
                    "resolved_requests": 0,
                    "not_resolved_requests": 0,
                    "user_confirmed_resolution_rate": None,
                },
            )

    def test_empty_feedback_summary_is_safe(self):
        with TemporaryDirectory() as directory:
            telemetry = TelemetryStore(Path(directory) / "history.db")

            self.assertEqual(
                telemetry.feedback_summary(),
                {
                    "feedback_responses": 0,
                    "resolved_requests": 0,
                    "not_resolved_requests": 0,
                    "user_confirmed_resolution_rate": None,
                },
            )

    def test_existing_feedback_rows_are_upgraded_without_comments(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "history.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE support_request_events (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    error_stage TEXT,
                    error_type TEXT
                );
                CREATE TABLE support_feedback (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE REFERENCES support_request_events(id),
                    resolution_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO support_request_events VALUES (
                    'request-1', 'conversation-1', '2026-09-20T09:00:00+00:00',
                    '2026-09-20T09:00:01+00:00', 100, 'agent_success', NULL, NULL
                );
                INSERT INTO support_feedback VALUES (
                    'feedback-1', 'request-1', 'resolved', '2026-09-20T09:00:02+00:00'
                );
                """
            )
            connection.close()

            telemetry = TelemetryStore(database)

            self.assertIsNone(telemetry.feedback_for_request("request-1")["feedback_text"])
            with telemetry._connection() as upgraded_connection:
                self.assertIsNone(
                    upgraded_connection.execute(
                        "SELECT category FROM support_request_events WHERE id = 'request-1'"
                    ).fetchone()["category"]
                )

    def test_analytics_conversation_count(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "history.db"
            conv_store = ConversationStore(database)
            telemetry = TelemetryStore(database)

            c1 = conv_store.create_conversation("First issue")
            c2 = conv_store.create_conversation("Second issue")

            with telemetry._connection() as connection:
                connection.execute("UPDATE conversations SET created_at = '2026-09-20T10:00:00+00:00' WHERE id = ?", (c1,))
                connection.execute("UPDATE conversations SET created_at = '2026-09-21T10:00:00+00:00' WHERE id = ?", (c2,))

            telemetry.record_request(c1, "2026-09-20T10:00:00+00:00", 150, "agent_success")
            telemetry.record_request(c2, "2026-09-21T10:00:00+00:00", 200, "agent_success")

            self.assertEqual(telemetry.conversation_count(), 2)
            self.assertEqual(telemetry.conversation_count("2026-09-21T00:00:00+00:00"), 1)

    def test_analytics_conversation_count_without_conversations_table(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "history.db"
            telemetry = TelemetryStore(database)
            telemetry.record_request("c1", "2026-09-20T10:00:00+00:00", 150, "agent_success")
            telemetry.record_request("c1", "2026-09-20T11:00:00+00:00", 150, "agent_success")
            telemetry.record_request("c2", "2026-09-21T10:00:00+00:00", 200, "agent_success")

            self.assertEqual(telemetry.conversation_count(), 2)
            self.assertEqual(telemetry.conversation_count("2026-09-21T00:00:00+00:00"), 1)

    def test_analytics_ticket_count_and_summary(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "history.db"
            telemetry = TelemetryStore(database)

            self.assertEqual(telemetry.ticket_count(), 0)
            self.assertEqual(
                telemetry.ticket_summary(),
                {"total_tickets": 0, "open_tickets": 0, "resolved_tickets": 0, "escalated_tickets": 0},
            )

            with telemetry._connection() as connection:
                connection.execute(
                    "INSERT INTO tickets (id, conversation_id, category, description, priority, status, created_at) "
                    "VALUES ('t1', 'c1', 'network', 'No wifi', 'high', 'open', '2026-09-20T10:00:00+00:00')"
                )
                connection.execute(
                    "INSERT INTO tickets (id, conversation_id, category, description, priority, status, created_at) "
                    "VALUES ('t2', 'c1', 'vpn', 'VPN dropped', 'medium', 'escalated', '2026-09-21T11:00:00+00:00')"
                )
                connection.execute(
                    "INSERT INTO tickets (id, conversation_id, category, description, priority, status, created_at) "
                    "VALUES ('t3', 'c2', 'account', 'Password lock', 'low', 'resolved', '2026-09-21T12:00:00+00:00')"
                )

            self.assertEqual(telemetry.ticket_count(), 3)
            self.assertEqual(telemetry.ticket_count("2026-09-21T00:00:00+00:00"), 2)

            summary = telemetry.ticket_summary()
            self.assertEqual(summary["total_tickets"], 3)
            self.assertEqual(summary["open_tickets"], 1)
            self.assertEqual(summary["escalated_tickets"], 1)
            self.assertEqual(summary["resolved_tickets"], 1)

    def test_analytics_resolved_vs_escalated_comparison(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "history.db"
            telemetry = TelemetryStore(database)

            req1 = telemetry.record_request("c1", "2026-09-20T10:00:00+00:00", 100, "agent_success")
            req2 = telemetry.record_request("c1", "2026-09-21T10:00:00+00:00", 100, "agent_success")
            telemetry.record_feedback(req1, "resolved")
            telemetry.record_feedback(req2, "not_resolved")

            comp = telemetry.resolution_vs_escalation()
            self.assertEqual(comp["resolved_count"], 1)
            self.assertEqual(comp["escalated_count"], 1)
            self.assertEqual(comp["total_cases"], 2)
            self.assertEqual(comp["resolution_rate"], 50.0)
            self.assertEqual(comp["escalation_rate"], 50.0)

            with telemetry._connection() as connection:
                connection.execute(
                    "INSERT INTO tickets (id, conversation_id, category, description, priority, status, created_at) "
                    "VALUES ('t1', 'c1', 'network', 'Escalated issue', 'high', 'escalated', '2026-09-21T11:00:00+00:00')"
                )

            comp2 = telemetry.resolution_vs_escalation()
            self.assertEqual(comp2["resolved_count"], 1)
            self.assertEqual(comp2["escalated_count"], 2)
            self.assertEqual(comp2["total_cases"], 3)
            self.assertAlmostEqual(comp2["resolution_rate"], 33.3, places=1)
            self.assertAlmostEqual(comp2["escalation_rate"], 66.7, places=1)

    def test_analytics_category_distribution_and_most_common(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "history.db"
            telemetry = TelemetryStore(database)

            telemetry.record_request("c1", "2026-09-20T10:00:00+00:00", 100, "fallback", category="network")
            telemetry.record_request("c1", "2026-09-20T11:00:00+00:00", 100, "fallback", category="network")
            telemetry.record_request("c1", "2026-09-20T12:00:00+00:00", 100, "fallback", category="vpn")

            dist = telemetry.category_distribution()
            self.assertEqual(len(dist), 2)
            self.assertEqual(dist[0]["category"], "network")
            self.assertEqual(dist[0]["requests"], 2)
            self.assertEqual(dist[0]["label"], "Network / Wi-Fi")
            self.assertEqual(dist[1]["category"], "vpn")
            self.assertEqual(dist[1]["requests"], 1)

            top = telemetry.most_common_category()
            self.assertIsNotNone(top)
            self.assertEqual(top["category"], "network")
            self.assertEqual(top["label"], "Network / Wi-Fi")
            self.assertEqual(top["requests"], 2)
            self.assertAlmostEqual(top["percentage"], 66.7, places=1)

    def test_analytics_usage_trends_over_time(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "history.db"
            telemetry = TelemetryStore(database)

            telemetry.record_request("c1", "2026-09-20T10:00:00+00:00", 100, "agent_success")
            telemetry.record_request("c1", "2026-09-20T11:00:00+00:00", 100, "agent_success")
            telemetry.record_request("c2", "2026-09-21T09:00:00+00:00", 100, "agent_success")

            trends = telemetry.usage_trends()
            self.assertEqual(len(trends), 2)
            self.assertEqual(trends[0]["day"], "2026-09-20")
            self.assertEqual(trends[0]["requests"], 2)
            self.assertEqual(trends[0]["conversations"], 1)
            self.assertEqual(trends[1]["day"], "2026-09-21")
            self.assertEqual(trends[1]["requests"], 1)
            self.assertEqual(trends[1]["conversations"], 1)

    def test_analytics_usable_with_empty_data(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "history.db"
            telemetry = TelemetryStore(database)

            self.assertEqual(telemetry.conversation_count(), 0)
            self.assertEqual(telemetry.ticket_count(), 0)
            self.assertEqual(
                telemetry.ticket_summary(),
                {"total_tickets": 0, "open_tickets": 0, "resolved_tickets": 0, "escalated_tickets": 0},
            )
            self.assertEqual(
                telemetry.resolution_vs_escalation(),
                {
                    "resolved_count": 0,
                    "escalated_count": 0,
                    "total_cases": 0,
                    "resolution_rate": None,
                    "escalation_rate": None,
                },
            )
            self.assertEqual(telemetry.category_distribution(), [])
            self.assertIsNone(telemetry.most_common_category())
            self.assertEqual(telemetry.usage_trends(), [])

    def test_analytics_privacy_guarantee(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "history.db"
            telemetry = TelemetryStore(database)

            req = telemetry.record_request("c1", "2026-09-20T10:00:00+00:00", 100, "agent_success")
            secret_comment = "SECRET_PASSWORD_12345"
            telemetry.record_feedback(req, "resolved", secret_comment)

            dist = telemetry.category_distribution()
            trends = telemetry.usage_trends()
            comp = telemetry.resolution_vs_escalation()
            summary = telemetry.summary()
            vol = telemetry.volume_by_day()
            failures = telemetry.failure_breakdown()

            for payload in (dist, trends, comp, summary, vol, failures):
                payload_str = str(payload)
                self.assertNotIn(secret_comment, payload_str)

if __name__ == "__main__":
    unittest.main()
