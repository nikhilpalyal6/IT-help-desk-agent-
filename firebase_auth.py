"""Firebase Authentication client and helpers for Streamlit IT Helpdesk."""

from dataclasses import dataclass
import os
import re
from typing import Any, Dict, Optional, Tuple

import requests
import streamlit as st

# Regular expression for basic email format validation
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
MIN_PASSWORD_LENGTH = 6

FIREBASE_SIGNUP_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signUp"
FIREBASE_SIGNIN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
FIREBASE_LOOKUP_URL = "https://identitytoolkit.googleapis.com/v1/accounts:lookup"
FIREBASE_REFRESH_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"


class FirebaseAuthError(Exception):
    """User-friendly authentication error that sanitizes internal details."""

    def __init__(self, message: str, error_code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.error_code = error_code


@dataclass(frozen=True)
class FirebaseConfig:
    """Configuration settings for Firebase Authentication."""

    api_key: str
    auth_domain: Optional[str] = None
    project_id: Optional[str] = None
    storage_bucket: Optional[str] = None
    messaging_sender_id: Optional[str] = None
    app_id: Optional[str] = None

    @classmethod
    def load(cls) -> Optional["FirebaseConfig"]:
        """Load Firebase configuration from Streamlit secrets or environment variables."""
        api_key = None
        auth_domain = None
        project_id = None
        storage_bucket = None
        messaging_sender_id = None
        app_id = None

        # 1. Attempt loading from Streamlit secrets
        try:
            if hasattr(st, "secrets") and "firebase" in st.secrets:
                fb_secrets = st.secrets["firebase"]
                api_key = fb_secrets.get("api_key")
                auth_domain = fb_secrets.get("auth_domain")
                project_id = fb_secrets.get("project_id")
                storage_bucket = fb_secrets.get("storage_bucket")
                messaging_sender_id = fb_secrets.get("messaging_sender_id")
                app_id = fb_secrets.get("app_id")
        except Exception:
            # Secrets file may not exist or cannot be parsed
            pass

        # 2. Fall back to environment variables if any are set
        if not api_key:
            api_key = os.getenv("FIREBASE_API_KEY")
        if not auth_domain:
            auth_domain = os.getenv("FIREBASE_AUTH_DOMAIN")
        if not project_id:
            project_id = os.getenv("FIREBASE_PROJECT_ID")
        if not storage_bucket:
            storage_bucket = os.getenv("FIREBASE_STORAGE_BUCKET")
        if not messaging_sender_id:
            messaging_sender_id = os.getenv("FIREBASE_MESSAGING_SENDER_ID")
        if not app_id:
            app_id = os.getenv("FIREBASE_APP_ID")

        if not api_key:
            return None

        return cls(
            api_key=str(api_key).strip(),
            auth_domain=str(auth_domain).strip() if auth_domain else None,
            project_id=str(project_id).strip() if project_id else None,
            storage_bucket=str(storage_bucket).strip() if storage_bucket else None,
            messaging_sender_id=str(messaging_sender_id).strip() if messaging_sender_id else None,
            app_id=str(app_id).strip() if app_id else None,
        )


def validate_email(email: Optional[str]) -> Tuple[bool, str]:
    """Validate email address format."""
    if not email or not email.strip():
        return False, "Email address is required."
    clean_email = email.strip()
    if not EMAIL_REGEX.match(clean_email):
        return False, "Please enter a valid email address (e.g., user@example.com)."
    return True, ""


def validate_password(password: Optional[str]) -> Tuple[bool, str]:
    """Validate password requirements."""
    if not password:
        return False, "Password is required."
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return True, ""


def sanitize_firebase_error(raw_message: str) -> str:
    """Map Firebase error messages to clear, user-friendly strings."""
    code = raw_message.split(":")[0].strip() if raw_message else ""

    mapping = {
        "EMAIL_EXISTS": "An account with this email address already exists. Please log in instead.",
        "OPERATION_NOT_ALLOWED": "Email/password sign-in is not enabled for this project. Please check Firebase console.",
        "TOO_MANY_ATTEMPTS_TRY_LATER": "Access temporarily disabled due to unusual activity or multiple failed attempts. Please try again later.",
        "EMAIL_NOT_FOUND": "Invalid email or password. Please verify your credentials and try again.",
        "INVALID_PASSWORD": "Invalid email or password. Please verify your credentials and try again.",
        "INVALID_LOGIN_CREDENTIALS": "Invalid email or password. Please verify your credentials and try again.",
        "USER_DISABLED": "This user account has been disabled by an administrator.",
        "WEAK_PASSWORD": f"Password is too weak. It must be at least {MIN_PASSWORD_LENGTH} characters.",
        "INVALID_EMAIL": "Please enter a valid email address.",
        "MISSING_PASSWORD": "Password is required.",
        "MISSING_EMAIL": "Email address is required.",
        "API_KEY_INVALID": "Firebase configuration error: Invalid API key.",
    }

    return mapping.get(code, "Authentication failed. Please verify your details and try again.")


class FirebaseAuthClient:
    """Client for Firebase Authentication REST API."""

    def __init__(self, config: FirebaseConfig, timeout: int = 10):
        self.config = config
        self.timeout = timeout

    def sign_up(self, email: str, password: str) -> Dict[str, Any]:
        """Create a new account using email and password via Firebase Authentication."""
        email_valid, email_err = validate_email(email)
        if not email_valid:
            raise FirebaseAuthError(email_err, "INVALID_EMAIL")

        pw_valid, pw_err = validate_password(password)
        if not pw_valid:
            raise FirebaseAuthError(pw_err, "WEAK_PASSWORD")

        params = {"key": self.config.api_key}
        payload = {
            "email": email.strip(),
            "password": password,
            "returnSecureToken": True,
        }

        data = self._send_request(FIREBASE_SIGNUP_URL, params, payload)
        return {
            "email": data.get("email"),
            "uid": data.get("localId"),
            "id_token": data.get("idToken"),
            "refresh_token": data.get("refreshToken"),
            "expires_in": data.get("expiresIn"),
        }

    def sign_in(self, email: str, password: str) -> Dict[str, Any]:
        """Sign in an existing user using email and password via Firebase Authentication."""
        email_valid, email_err = validate_email(email)
        if not email_valid:
            raise FirebaseAuthError(email_err, "INVALID_EMAIL")

        if not password:
            raise FirebaseAuthError("Password is required.", "MISSING_PASSWORD")

        params = {"key": self.config.api_key}
        payload = {
            "email": email.strip(),
            "password": password,
            "returnSecureToken": True,
        }

        data = self._send_request(FIREBASE_SIGNIN_URL, params, payload)
        return {
            "email": data.get("email"),
            "uid": data.get("localId"),
            "id_token": data.get("idToken"),
            "refresh_token": data.get("refreshToken"),
            "expires_in": data.get("expiresIn"),
        }

    def get_user_info(self, id_token: str) -> Dict[str, Any]:
        """Fetch user profile details using an active ID token."""
        if not id_token:
            raise FirebaseAuthError("Authentication token is missing.", "MISSING_TOKEN")

        params = {"key": self.config.api_key}
        payload = {"idToken": id_token}
        data = self._send_request(FIREBASE_LOOKUP_URL, params, payload)
        users = data.get("users", [])
        if not users:
            raise FirebaseAuthError("User profile not found.", "USER_NOT_FOUND")
        return users[0]

    def refresh_token(self, refresh_token: str) -> Dict[str, Any]:
        """Exchange a refresh token for a fresh Firebase ID token."""
        if not refresh_token or not str(refresh_token).strip():
            raise FirebaseAuthError("Refresh token is required.", "MISSING_REFRESH_TOKEN")

        params = {"key": self.config.api_key}
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": str(refresh_token).strip(),
        }

        try:
            response = requests.post(
                FIREBASE_REFRESH_TOKEN_URL,
                params=params,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
        except requests.Timeout:
            raise FirebaseAuthError(
                "Authentication service timed out. Please check your connection and try again.",
                "TIMEOUT",
            )
        except requests.RequestException:
            raise FirebaseAuthError(
                "Unable to connect to authentication service. Please check your network connection.",
                "NETWORK_ERROR",
            )

        try:
            data = response.json()
        except ValueError:
            raise FirebaseAuthError(
                "Received an invalid response from authentication service.",
                "INVALID_RESPONSE",
            )

        if not response.ok or "error" in data:
            error_data = data.get("error", {})
            raw_message = error_data.get("message", "TOKEN_EXPIRED")
            user_message = sanitize_firebase_error(raw_message)
            raise FirebaseAuthError(user_message, raw_message)

        return {
            "id_token": data.get("id_token"),
            "refresh_token": data.get("refresh_token"),
            "expires_in": data.get("expires_in"),
            "uid": data.get("user_id"),
            "project_id": data.get("project_id"),
        }

    def _send_request(self, url: str, params: dict, payload: dict) -> Dict[str, Any]:
        """Internal helper to dispatch requests to Firebase REST API with robust error handling."""
        try:
            response = requests.post(
                url,
                params=params,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except requests.Timeout:
            raise FirebaseAuthError(
                "Authentication service timed out. Please check your connection and try again.",
                "TIMEOUT",
            )
        except requests.RequestException:
            raise FirebaseAuthError(
                "Unable to connect to authentication service. Please check your network connection.",
                "NETWORK_ERROR",
            )

        try:
            data = response.json()
        except ValueError:
            raise FirebaseAuthError(
                "Received an invalid response from authentication service.",
                "INVALID_RESPONSE",
            )

        if not response.ok or "error" in data:
            error_data = data.get("error", {})
            raw_message = error_data.get("message", "UNKNOWN_ERROR")
            error_code = raw_message.split(":")[0].strip() if raw_message else "UNKNOWN_ERROR"
            user_message = sanitize_firebase_error(raw_message)
            raise FirebaseAuthError(user_message, error_code)

        return data
