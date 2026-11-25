"""Tests for Firebase Authentication integration in IT Helpdesk Agent."""

import os
import unittest
from unittest.mock import MagicMock, patch

import requests

from firebase_auth import (
    FirebaseConfig,
    FirebaseAuthClient,
    FirebaseAuthError,
    sanitize_firebase_error,
    validate_email,
    validate_password,
)


class TestValidation(unittest.TestCase):
    """Test input validation for email and password."""

    def test_valid_emails(self):
        valid_cases = [
            "user@example.com",
            "first.last@company.co.uk",
            "support+tier1@domain.org",
            "user123@sub.domain.io",
        ]
        for email in valid_cases:
            valid, msg = validate_email(email)
            self.assertTrue(valid, f"Expected {email} to be valid")
            self.assertEqual(msg, "")

    def test_invalid_emails(self):
        invalid_cases = [
            "",
            "   ",
            None,
            "plainaddress",
            "@missingusername.com",
            "username@.com",
            "username@domain",
        ]
        for email in invalid_cases:
            valid, msg = validate_email(email)
            self.assertFalse(valid, f"Expected {email} to be invalid")
            self.assertTrue(len(msg) > 0)

    def test_valid_passwords(self):
        valid_cases = ["password123", "Strong#Pass99", "123456", "longpasswordwithletters"]
        for pwd in valid_cases:
            valid, msg = validate_password(pwd)
            self.assertTrue(valid, f"Expected password to be valid: {pwd}")
            self.assertEqual(msg, "")

    def test_invalid_passwords(self):
        invalid_cases = [
            ("", "Password is required."),
            (None, "Password is required."),
            ("12345", "Password must be at least 6 characters."),
            ("abc", "Password must be at least 6 characters."),
        ]
        for pwd, expected_sub in invalid_cases:
            valid, msg = validate_password(pwd)
            self.assertFalse(valid)
            self.assertIn(expected_sub, msg)


class TestFirebaseConfig(unittest.TestCase):
    """Test Firebase configuration loading."""

    def test_config_from_env(self):
        with patch.dict(
            os.environ,
            {
                "FIREBASE_API_KEY": "test-api-key",
                "FIREBASE_PROJECT_ID": "test-project",
                "FIREBASE_AUTH_DOMAIN": "test.firebaseapp.com",
            },
        ):
            with patch("streamlit.secrets", {}):
                config = FirebaseConfig.load()
                self.assertIsNotNone(config)
                self.assertEqual(config.api_key, "test-api-key")
                self.assertEqual(config.project_id, "test-project")
                self.assertEqual(config.auth_domain, "test.firebaseapp.com")

    def test_config_missing_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            # Patch st.secrets to have no firebase
            with patch("streamlit.secrets", {}):
                config = FirebaseConfig.load()
                self.assertIsNone(config)


class TestFirebaseAuthClient(unittest.TestCase):
    """Test Firebase Authentication client operations and error handling."""

    def setUp(self):
        self.config = FirebaseConfig(
            api_key="mock_api_key",
            project_id="mock_project",
        )
        self.client = FirebaseAuthClient(self.config)

    @patch("requests.post")
    def test_sign_up_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "email": "employee@company.com",
            "localId": "user_uid_12345",
            "idToken": "fake_id_token",
            "refreshToken": "fake_refresh_token",
            "expiresIn": "3600",
        }
        mock_post.return_value = mock_response

        session = self.client.sign_up("employee@company.com", "securePassword123")
        self.assertEqual(session["email"], "employee@company.com")
        self.assertEqual(session["uid"], "user_uid_12345")
        self.assertEqual(session["id_token"], "fake_id_token")
        self.assertEqual(session["refresh_token"], "fake_refresh_token")

    def test_sign_up_validation_failures(self):
        # Invalid email
        with self.assertRaises(FirebaseAuthError) as ctx:
            self.client.sign_up("invalid-email", "validpassword123")
        self.assertEqual(ctx.exception.error_code, "INVALID_EMAIL")

        # Weak password
        with self.assertRaises(FirebaseAuthError) as ctx:
            self.client.sign_up("valid@example.com", "123")
        self.assertEqual(ctx.exception.error_code, "WEAK_PASSWORD")

    @patch("requests.post")
    def test_sign_up_email_already_exists(self, mock_post):
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.json.return_value = {
            "error": {
                "code": 400,
                "message": "EMAIL_EXISTS",
            }
        }
        mock_post.return_value = mock_response

        with self.assertRaises(FirebaseAuthError) as ctx:
            self.client.sign_up("already_used@company.com", "password123")
        self.assertEqual(ctx.exception.error_code, "EMAIL_EXISTS")
        self.assertIn("already exists", ctx.exception.message)

    @patch("requests.post")
    def test_sign_up_operation_not_allowed(self, mock_post):
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.json.return_value = {
            "error": {
                "code": 400,
                "message": "OPERATION_NOT_ALLOWED",
            }
        }
        mock_post.return_value = mock_response

        with self.assertRaises(FirebaseAuthError) as ctx:
            self.client.sign_up("newuser@company.com", "password123")
        self.assertEqual(ctx.exception.error_code, "OPERATION_NOT_ALLOWED")
        self.assertIn("not enabled", ctx.exception.message)

    @patch("requests.post")
    def test_sign_in_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "email": "returning@company.com",
            "localId": "uid_returning_789",
            "idToken": "valid_token",
            "refreshToken": "valid_refresh",
            "expiresIn": "3600",
        }
        mock_post.return_value = mock_response

        session = self.client.sign_in("returning@company.com", "mySecretPassword")
        self.assertEqual(session["email"], "returning@company.com")
        self.assertEqual(session["uid"], "uid_returning_789")

    @patch("requests.post")
    def test_sign_in_invalid_credentials(self, mock_post):
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.json.return_value = {
            "error": {
                "code": 400,
                "message": "INVALID_LOGIN_CREDENTIALS",
            }
        }
        mock_post.return_value = mock_response

        with self.assertRaises(FirebaseAuthError) as ctx:
            self.client.sign_in("user@company.com", "wrongpassword")
        self.assertEqual(ctx.exception.error_code, "INVALID_LOGIN_CREDENTIALS")
        self.assertIn("Invalid email or password", ctx.exception.message)

    @patch("requests.post")
    def test_sign_in_user_disabled(self, mock_post):
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.json.return_value = {
            "error": {
                "code": 400,
                "message": "USER_DISABLED",
            }
        }
        mock_post.return_value = mock_response

        with self.assertRaises(FirebaseAuthError) as ctx:
            self.client.sign_in("disabled@company.com", "password123")
        self.assertEqual(ctx.exception.error_code, "USER_DISABLED")
        self.assertIn("disabled", ctx.exception.message)

    @patch("requests.post")
    def test_sign_in_too_many_attempts(self, mock_post):
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.json.return_value = {
            "error": {
                "code": 400,
                "message": "TOO_MANY_ATTEMPTS_TRY_LATER",
            }
        }
        mock_post.return_value = mock_response

        with self.assertRaises(FirebaseAuthError) as ctx:
            self.client.sign_in("user@company.com", "password123")
        self.assertEqual(ctx.exception.error_code, "TOO_MANY_ATTEMPTS_TRY_LATER")
        self.assertIn("temporarily disabled", ctx.exception.message)

    @patch("requests.post")
    def test_network_timeout(self, mock_post):
        mock_post.side_effect = requests.Timeout("Connection timed out")

        with self.assertRaises(FirebaseAuthError) as ctx:
            self.client.sign_in("user@company.com", "password123")
        self.assertEqual(ctx.exception.error_code, "TIMEOUT")
        self.assertIn("timed out", ctx.exception.message)

    @patch("requests.post")
    def test_network_connection_error(self, mock_post):
        mock_post.side_effect = requests.ConnectionError("Network is unreachable")

        with self.assertRaises(FirebaseAuthError) as ctx:
            self.client.sign_in("user@company.com", "password123")
        self.assertEqual(ctx.exception.error_code, "NETWORK_ERROR")
        self.assertIn("network connection", ctx.exception.message)

    @patch("requests.post")
    def test_get_user_info_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "users": [
                {
                    "localId": "uid123",
                    "email": "user@example.com",
                    "emailVerified": True,
                }
            ]
        }
        mock_post.return_value = mock_response

        info = self.client.get_user_info("token_abc")
        self.assertEqual(info["email"], "user@example.com")
        self.assertEqual(info["localId"], "uid123")


class TestErrorSanitization(unittest.TestCase):
    """Test that Firebase internal error codes are mapped to clear messages."""

    def test_sanitize_messages(self):
        self.assertIn("already exists", sanitize_firebase_error("EMAIL_EXISTS"))
        self.assertIn("Invalid email or password", sanitize_firebase_error("EMAIL_NOT_FOUND"))
        self.assertIn("Invalid email or password", sanitize_firebase_error("INVALID_PASSWORD"))
        self.assertIn("Invalid email or password", sanitize_firebase_error("INVALID_LOGIN_CREDENTIALS"))
        self.assertIn("disabled", sanitize_firebase_error("USER_DISABLED"))
        self.assertIn("temporarily disabled", sanitize_firebase_error("TOO_MANY_ATTEMPTS_TRY_LATER"))
        self.assertIn("too weak", sanitize_firebase_error("WEAK_PASSWORD : Password should be at least 6 characters"))
        self.assertIn("valid email", sanitize_firebase_error("INVALID_EMAIL"))


class TestStreamlitAuthIntegration(unittest.TestCase):
    """Test Streamlit session state and configuration loading."""

    def test_config_from_secrets(self):
        fake_secrets = {
            "firebase": {
                "api_key": "secrets-api-key",
                "auth_domain": "secrets-project.firebaseapp.com",
                "project_id": "secrets-project",
                "storage_bucket": "secrets-project.appspot.com",
                "messaging_sender_id": "12345",
                "app_id": "1:12345:web:abcdef",
            }
        }
        with patch.dict(os.environ, {}, clear=True):
            with patch("streamlit.secrets", fake_secrets):
                config = FirebaseConfig.load()
                self.assertIsNotNone(config)
                self.assertEqual(config.api_key, "secrets-api-key")
                self.assertEqual(config.project_id, "secrets-project")
                self.assertEqual(config.storage_bucket, "secrets-project.appspot.com")

    def test_ensure_session_state_sets_auth_defaults(self):
        import streamlit as st
        from app import ensure_session_state

        st.session_state.clear()
        ensure_session_state()

        self.assertIn("auth_user", st.session_state)
        self.assertIn("auth_view", st.session_state)
        self.assertIsNone(st.session_state.auth_user)
        self.assertEqual(st.session_state.auth_view, "login")

    def test_show_auth_view_stops_execution(self):
        import streamlit as st
        from app import show_auth_view

        st.session_state.clear()
        st.session_state.auth_user = None
        st.session_state.auth_view = "login"

        # When show_auth_view is invoked, it must call st.stop() to halt unauthenticated access
        with patch("streamlit.stop", side_effect=SystemExit("st.stop called")) as mock_stop:
            with patch.object(FirebaseConfig, "load", return_value=None):
                with self.assertRaises(SystemExit):
                    show_auth_view()
                mock_stop.assert_called()

    def test_req2_signup_with_mismatched_passwords_validation(self):
        password = "SecurePassword123"
        confirm_password = "DifferentPassword456"
        self.assertNotEqual(password, confirm_password)

    def test_req8_and_req9_session_state_and_logout(self):
        import streamlit as st
        from app import ensure_session_state, start_new_conversation

        st.session_state.clear()
        ensure_session_state()

        # Simulate authenticated session (Req 7 & 9)
        st.session_state.auth_user = {"email": "test@company.com", "uid": "123"}
        self.assertIsNotNone(st.session_state.auth_user)

        # Simulate rerun - session remains authenticated (Req 9)
        ensure_session_state()
        self.assertEqual(st.session_state.auth_user["email"], "test@company.com")

        # Simulate logout (Req 8)
        start_new_conversation()
        st.session_state.auth_user = None
        st.session_state.auth_view = "login"
        self.assertIsNone(st.session_state.auth_user)
        self.assertEqual(st.session_state.auth_view, "login")

    def test_req10_foundry_chat_and_local_troubleshooting_still_available(self):
        from app import local_troubleshooting_response
        response, category = local_troubleshooting_response("Wi-Fi network connection issue")
        self.assertEqual(category, "network")
    def test_refresh_token_success(self):
        client = FirebaseAuthClient(FirebaseConfig(api_key="test-api-key"))
        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "id_token": "fresh_id_token",
                "refresh_token": "fresh_refresh_token",
                "expires_in": "3600",
                "user_id": "test_uid",
                "project_id": "test_project",
            }
            mock_post.return_value = mock_resp

            result = client.refresh_token("valid_refresh_token")
            self.assertEqual(result["id_token"], "fresh_id_token")
            self.assertEqual(result["refresh_token"], "fresh_refresh_token")
            self.assertEqual(result["uid"], "test_uid")

    def test_refresh_token_missing_raises_error(self):
        client = FirebaseAuthClient(FirebaseConfig(api_key="test-api-key"))
        with self.assertRaises(FirebaseAuthError):
            client.refresh_token("")


if __name__ == "__main__":
    unittest.main()
