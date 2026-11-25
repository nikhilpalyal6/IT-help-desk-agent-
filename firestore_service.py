"""Cloud Firestore client for isolated user helpdesk activity tracking."""

from datetime import UTC, datetime
from typing import Any, Dict, List, Optional
import requests


class FirestoreError(Exception):
    """Base sanitized Firestore exception."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class FirestorePermissionError(FirestoreError):
    """Raised when a user attempts to access or mutate another user's data."""
    pass


def categorize_prompt(prompt: str) -> str:
    """Classify user questions into standard IT helpdesk categories."""
    p = (prompt or "").lower()
    if any(k in p for k in ("wi-fi", "wifi", "internet", "network", "ethernet", "dns")):
        return "Network/Wi-Fi"
    if "vpn" in p:
        return "VPN"
    if any(k in p for k in ("password", "passcode", "account", "login", "sign in", "mfa", "2fa")):
        return "Password/Account"
    if any(k in p for k in ("crash", "software", "app", "application", "install", "freeze", "error")):
        return "Software"
    if any(k in p for k in ("printer", "print", "scanner", "paper")):
        return "Printer"
    if any(k in p for k in ("email", "outlook", "mail", "inbox")):
        return "Email"
    return "General"


def _to_firestore_value(val: Any) -> Dict[str, Any]:
    """Encode a Python primitive to Firestore REST value format."""
    if isinstance(val, bool):
        return {"booleanValue": val}
    if isinstance(val, int):
        return {"integerValue": str(val)}
    return {"stringValue": str(val)}


def _from_firestore_value(field_dict: Dict[str, Any]) -> Any:
    """Decode a Firestore REST value dictionary to Python primitive."""
    if "stringValue" in field_dict:
        return field_dict["stringValue"]
    if "integerValue" in field_dict:
        try:
            return int(field_dict["integerValue"])
        except (ValueError, TypeError):
            return field_dict["integerValue"]
    if "booleanValue" in field_dict:
        return bool(field_dict["booleanValue"])
    if "timestampValue" in field_dict:
        return field_dict["timestampValue"]
    return None


class FirestoreClient:
    """Client for interacting with Cloud Firestore REST API with strict per-user data isolation."""

    def __init__(self, project_id: str, id_token: Optional[str] = None, timeout: int = 10):
        self.project_id = project_id
        self.id_token = id_token
        self.timeout = timeout
        self.base_url = f"https://firestore.googleapis.com/v1/projects/{project_id}/databases/(default)/documents"

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.id_token:
            headers["Authorization"] = f"Bearer {self.id_token}"
        return headers

    def record_activity(
        self,
        uid: str,
        question: str,
        category: Optional[str] = None,
        conversation_id: Optional[str] = None,
        caller_uid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Record a helpdesk question activity under /users/{uid}/helpdesk_activity.
        Strictly prevents writing to another user's collection if caller_uid is provided.
        """
        if not uid or not str(uid).strip():
            raise FirestoreError("User identifier is required.")

        clean_uid = str(uid).strip()

        # Security check: caller can only record their own activity
        if caller_uid and str(caller_uid).strip() != clean_uid:
            raise FirestorePermissionError("Access denied: You can only record activity for your own user account.")

        if not category:
            category = categorize_prompt(question)

        timestamp = datetime.now(UTC).isoformat()
        fields = {
            "question": _to_firestore_value(question.strip()),
            "category": _to_firestore_value(category),
            "conversation_id": _to_firestore_value(conversation_id or ""),
            "timestamp": _to_firestore_value(timestamp),
        }

        url = f"{self.base_url}/users/{clean_uid}/helpdesk_activity"
        payload = {"fields": fields}

        try:
            response = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        except requests.Timeout:
            raise FirestoreError("Connection to Firestore timed out.", 408)
        except requests.RequestException:
            raise FirestoreError("Unable to connect to database. Please check your network.", 503)

        if response.status_code == 403:
            raise FirestorePermissionError("Access denied by Firestore security rules.", 403)
        if response.status_code == 401:
            raise FirestoreError("Authentication token expired. Please log in again.", 401)
        if not response.ok:
            raise FirestoreError("Failed to record activity in Firestore.", response.status_code)

        doc = response.json()
        doc_id = doc.get("name", "").split("/")[-1]
        return {
            "id": doc_id,
            "question": question,
            "category": category,
            "conversation_id": conversation_id,
            "timestamp": timestamp,
        }

    def get_user_activity(self, uid: str, caller_uid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Fetch all helpdesk activities for a user under /users/{uid}/helpdesk_activity.
        Strictly prevents viewing another user's collection if caller_uid is provided.
        """
        if not uid or not str(uid).strip():
            raise FirestoreError("User identifier is required.")

        clean_uid = str(uid).strip()

        # Security check: caller can only read their own activity
        if caller_uid and str(caller_uid).strip() != clean_uid:
            raise FirestorePermissionError("Access denied: You cannot view another user's helpdesk history.")

        url = f"{self.base_url}/users/{clean_uid}/helpdesk_activity"

        try:
            response = requests.get(url, headers=self._headers(), timeout=self.timeout)
        except requests.Timeout:
            raise FirestoreError("Connection to Firestore timed out.", 408)
        except requests.RequestException:
            raise FirestoreError("Unable to connect to database. Please check your network.", 503)

        if response.status_code == 403:
            raise FirestorePermissionError("Access denied by Firestore security rules.", 403)
        if response.status_code == 401:
            raise FirestoreError("Authentication token expired. Please log in again.", 401)
        if response.status_code == 404:
            # Collection or user path does not exist yet (new user)
            return []
        if not response.ok:
            raise FirestoreError("Failed to fetch activity from Firestore.", response.status_code)

        data = response.json()
        raw_documents = data.get("documents", [])
        activities = []

        for doc in raw_documents:
            doc_fields = doc.get("fields", {})
            doc_id = doc.get("name", "").split("/")[-1]
            question = _from_firestore_value(doc_fields.get("question", {})) or ""
            category = _from_firestore_value(doc_fields.get("category", {})) or "General"
            conv_id = _from_firestore_value(doc_fields.get("conversation_id", {})) or ""
            ts = _from_firestore_value(doc_fields.get("timestamp", {})) or doc.get("createTime", "")

            activities.append({
                "id": doc_id,
                "question": question,
                "category": category,
                "conversation_id": conv_id,
                "timestamp": ts,
            })

        # Sort newest first
        activities.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return activities
