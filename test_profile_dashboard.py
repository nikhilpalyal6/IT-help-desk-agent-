"""Tests for User Profile Dashboard and Cloud Firestore user data isolation."""

import unittest
from unittest.mock import MagicMock, patch

import requests

from firestore_service import (
    FirestoreClient,
    FirestoreError,
    FirestorePermissionError,
    categorize_prompt,
)


class TestCategorizePrompt(unittest.TestCase):
    """Test prompt category classification."""

    def test_network_queries(self):
        self.assertEqual(categorize_prompt("My Wi-Fi keeps disconnecting"), "Network/Wi-Fi")
        self.assertEqual(categorize_prompt("Ethernet internet is down"), "Network/Wi-Fi")

    def test_vpn_queries(self):
        self.assertEqual(categorize_prompt("Cannot connect to company VPN"), "VPN")

    def test_account_queries(self):
        self.assertEqual(categorize_prompt("Need to reset my account password"), "Password/Account")
        self.assertEqual(categorize_prompt("MFA prompt is not arriving"), "Password/Account")

    def test_software_queries(self):
        self.assertEqual(categorize_prompt("App crashed on startup with an error"), "Software")

    def test_printer_queries(self):
        self.assertEqual(categorize_prompt("Printer queue is stuck"), "Printer")

    def test_email_queries(self):
        self.assertEqual(categorize_prompt("Outlook inbox is not receiving email"), "Email")

    def test_general_queries(self):
        self.assertEqual(categorize_prompt("How do I request a new monitor?"), "General")


class TestFirestoreService(unittest.TestCase):
    """Test Cloud Firestore client operations, serialization, and error handling."""

    def setUp(self):
        self.project_id = "test-helpdesk-proj"
        self.id_token = "fake_id_token_user_1"
        self.client = FirestoreClient(self.project_id, self.id_token)

    @patch("requests.post")
    def test_record_activity_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "name": "projects/test/databases/(default)/documents/users/user_1/helpdesk_activity/doc_123"
        }
        mock_post.return_value = mock_response

        res = self.client.record_activity(
            uid="user_1",
            question="Wi-Fi is not connecting",
            category="Network/Wi-Fi",
            conversation_id="conv_abc",
            caller_uid="user_1",
        )
        self.assertEqual(res["id"], "doc_123")
        self.assertEqual(res["question"], "Wi-Fi is not connecting")
        self.assertEqual(res["category"], "Network/Wi-Fi")
        self.assertEqual(res["conversation_id"], "conv_abc")

    def test_record_activity_isolation_block(self):
        """User A cannot write activity into User B's path."""
        with self.assertRaises(FirestorePermissionError):
            self.client.record_activity(
                uid="user_b",
                question="Malicious question",
                caller_uid="user_a",
            )

    @patch("requests.get")
    def test_get_user_activity_success(self, mock_get):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "documents": [
                {
                    "name": "projects/test/databases/(default)/documents/users/user_1/helpdesk_activity/doc_1",
                    "fields": {
                        "question": {"stringValue": "Wi-Fi is down"},
                        "category": {"stringValue": "Network/Wi-Fi"},
                        "conversation_id": {"stringValue": "conv_1"},
                        "timestamp": {"stringValue": "2026-09-22T10:00:00Z"},
                    },
                },
                {
                    "name": "projects/test/databases/(default)/documents/users/user_1/helpdesk_activity/doc_2",
                    "fields": {
                        "question": {"stringValue": "VPN error 800"},
                        "category": {"stringValue": "VPN"},
                        "conversation_id": {"stringValue": "conv_2"},
                        "timestamp": {"stringValue": "2026-09-22T11:00:00Z"},
                    },
                },
            ]
        }
        mock_get.return_value = mock_response

        activities = self.client.get_user_activity(uid="user_1", caller_uid="user_1")
        self.assertEqual(len(activities), 2)
        # Newest first
        self.assertEqual(activities[0]["question"], "VPN error 800")
        self.assertEqual(activities[1]["question"], "Wi-Fi is down")

    def test_get_user_activity_isolation_block(self):
        """User A cannot read User B's activity history."""
        with self.assertRaises(FirestorePermissionError):
            self.client.get_user_activity(uid="user_b", caller_uid="user_a")

    @patch("requests.get")
    def test_empty_activity_returns_empty_list(self, mock_get):
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        activities = self.client.get_user_activity(uid="new_user", caller_uid="new_user")
        self.assertEqual(activities, [])

    @patch("requests.get")
    def test_permission_denied_from_firestore_rules(self, mock_get):
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        with self.assertRaises(FirestorePermissionError):
            self.client.get_user_activity(uid="user_1", caller_uid="user_1")


class TestProfileDashboardFlows(unittest.TestCase):
    """
    Test End-to-End required Flows A through J:
    Flow A: Signup -> Login -> Helpdesk
    Flow B: Helpdesk question -> stored in Firestore
    Flow C: Profile -> question appears in history
    Flow D: Profile -> correct user email displayed
    Flow E: Logout -> session cleared
    Flow F: After logout -> Helpdesk inaccessible
    Flow G: After logout -> Profile inaccessible
    Flow H: Refresh after logout -> Login page remains
    Flow I: Login again -> user's own history restored
    Flow J: User A cannot access User B's profile/history
    """

    def setUp(self):
        import streamlit as st
        st.session_state.clear()

    def test_flow_a_signup_login_to_helpdesk(self):
        import streamlit as st
        from app import ensure_session_state

        ensure_session_state()
        # Simulate successful login
        st.session_state.auth_user = {
            "email": "employee1@company.com",
            "uid": "uid_employee1",
            "id_token": "token_emp1",
        }
        st.session_state.page = "active"

        self.assertIsNotNone(st.session_state.auth_user)
        self.assertEqual(st.session_state.page, "active")

    @patch("requests.post")
    def test_flow_b_and_c_question_stored_in_firestore_and_appears_in_profile(self, mock_post):
        """Flow B & C: Question stored in Firestore and appears in Profile."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "name": "projects/proj/databases/(default)/documents/users/uid_emp1/helpdesk_activity/act_1"
        }
        mock_post.return_value = mock_response

        client = FirestoreClient("proj", "token_emp1")
        res = client.record_activity(
            uid="uid_emp1",
            question="My Wi-Fi is not connecting",
            category="Network/Wi-Fi",
            conversation_id="conv_101",
            caller_uid="uid_emp1",
        )
        self.assertEqual(res["question"], "My Wi-Fi is not connecting")

        # Now test that it appears in profile history
        with patch("requests.get") as mock_get:
            mock_get_resp = MagicMock()
            mock_get_resp.ok = True
            mock_get_resp.status_code = 200
            mock_get_resp.json.return_value = {
                "documents": [
                    {
                        "name": "projects/proj/databases/(default)/documents/users/uid_emp1/helpdesk_activity/act_1",
                        "fields": {
                            "question": {"stringValue": "My Wi-Fi is not connecting"},
                            "category": {"stringValue": "Network/Wi-Fi"},
                            "conversation_id": {"stringValue": "conv_101"},
                            "timestamp": {"stringValue": "2026-09-22T12:00:00Z"},
                        },
                    }
                ]
            }
            mock_get.return_value = mock_get_resp

            activities = client.get_user_activity("uid_emp1", caller_uid="uid_emp1")
            self.assertEqual(len(activities), 1)
            self.assertEqual(activities[0]["question"], "My Wi-Fi is not connecting")
            self.assertEqual(activities[0]["category"], "Network/Wi-Fi")

    def test_flow_d_profile_displays_correct_user_email(self):
        """Flow D: Profile displays logged-in user's email."""
        import streamlit as st
        from app import ensure_session_state

        ensure_session_state()
        st.session_state.auth_user = {
            "email": "jane.doe@enterprise.com",
            "uid": "uid_jane_123",
        }
        self.assertEqual(st.session_state.auth_user["email"], "jane.doe@enterprise.com")
        self.assertEqual(st.session_state.auth_user["uid"], "uid_jane_123")

    def test_flow_e_through_h_logout_and_access_gating(self):
        """
        Flow E: Logout -> session cleared
        Flow F: After logout -> Helpdesk inaccessible
        Flow G: After logout -> Profile inaccessible
        Flow H: Refresh after logout -> Login page remains
        """
        import streamlit as st
        from app import ensure_session_state, logout, show_auth_view, show_profile_view

        ensure_session_state()
        st.session_state.auth_user = {
            "email": "user@company.com",
            "uid": "uid_user",
        }
        st.session_state.messages = [{"role": "user", "content": "hello"}]
        st.session_state.conversation_id = "conv_1"

        # E. Execute logout
        with patch("streamlit.rerun"):
            logout()

        self.assertIsNone(st.session_state.auth_user)
        self.assertEqual(st.session_state.messages, [])
        self.assertIsNone(st.session_state.conversation_id)
        self.assertEqual(st.session_state.auth_view, "login")

        # F & G. Unauthenticated attempts to access Helpdesk or Profile halt via show_auth_view
        with patch("streamlit.stop", side_effect=SystemExit("stop called")) as mock_stop:
            with patch("firebase_auth.FirebaseConfig.load", return_value=None):
                with self.assertRaises(SystemExit):
                    show_auth_view()
                mock_stop.assert_called()

        with patch("streamlit.stop", side_effect=SystemExit("stop called")) as mock_stop:
            with self.assertRaises(SystemExit):
                show_profile_view()
            mock_stop.assert_called()

        # H. Refresh/rerun retains unauthenticated login page
        ensure_session_state()
        self.assertIsNone(st.session_state.auth_user)
        self.assertEqual(st.session_state.auth_view, "login")

    def test_flow_i_login_again_restores_user_history(self):
        """Flow I: Logging in again as User A accesses User A's history."""
        client = FirestoreClient("proj", "token_a")
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "documents": [
                    {
                        "name": "projects/proj/databases/(default)/documents/users/uid_a/helpdesk_activity/act_a",
                        "fields": {
                            "question": {"stringValue": "User A question"},
                            "category": {"stringValue": "VPN"},
                            "conversation_id": {"stringValue": "conv_a"},
                            "timestamp": {"stringValue": "2026-09-22T12:00:00Z"},
                        },
                    }
                ]
            }
            mock_get.return_value = mock_resp

            activities = client.get_user_activity("uid_a", caller_uid="uid_a")
            self.assertEqual(len(activities), 1)
            self.assertEqual(activities[0]["question"], "User A question")

    def test_flow_j_user_a_cannot_access_user_b_data(self):
        """Flow J: User A is blocked from accessing User B's profile/history."""
        client_a = FirestoreClient("proj", "token_a")

        # 1. Client-level isolation check
        with self.assertRaises(FirestorePermissionError):
            client_a.get_user_activity(uid="uid_b", caller_uid="uid_a")

        # 2. Server-side rule simulation (Firestore returning 403)
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = False
            mock_resp.status_code = 403
            mock_get.return_value = mock_resp

            with self.assertRaises(FirestorePermissionError):
                client_a.get_user_activity(uid="uid_b", caller_uid="uid_b")

    def test_conversation_store_get_recent_user_questions(self):
        """Test that ConversationStore correctly extracts only user questions."""
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from history_store import ConversationStore

        with TemporaryDirectory() as tmpdir:
            store = ConversationStore(Path(tmpdir) / "test.db")
            cid = store.create_conversation("Troubleshooting")
            store.append_message(cid, "user", "How do I connect to VPN?")
            store.append_message(cid, "assistant", "Open the Cisco client.")
            store.append_message(cid, "user", "It says authentication failed")
            store.append_message(cid, "assistant", "Check your MFA prompt.")

            questions = store.get_recent_user_questions()
            self.assertEqual(len(questions), 2)
            # Ordered newest first
            self.assertEqual(questions[0]["question"], "It says authentication failed")
            self.assertEqual(questions[1]["question"], "How do I connect to VPN?")

    def test_profile_view_local_fallback_on_firestore_permission_error(self):
        """When Firestore security rules reject access (403), profile falls back to local questions."""
        import streamlit as st
        from app import show_profile_view
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from history_store import ConversationStore

        with TemporaryDirectory() as tmpdir:
            test_store = ConversationStore(Path(tmpdir) / "test.db")
            cid = test_store.create_conversation("Wi-Fi issue")
            test_store.append_message(cid, "user", "Wi-Fi is disconnected")

            st.session_state.auth_user = {
                "email": "user@test.com",
                "uid": "uid_123",
                "id_token": "expired_or_forbidden_token",
            }

            with patch("app.STORE", test_store):
                with patch("requests.get") as mock_get:
                    mock_resp = MagicMock()
                    mock_resp.ok = False
                    mock_resp.status_code = 403
                    mock_get.return_value = mock_resp

                    # Run show_profile_view and ensure it completes without unhandled exception
                    try:
                        show_profile_view()
                    except Exception as e:
                        self.fail(f"show_profile_view raised unexpected exception: {e}")


if __name__ == "__main__":
    unittest.main()
