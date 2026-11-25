import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter

import streamlit as st
try:
    from agent_framework.foundry import FoundryAgent
except ImportError:
    FoundryAgent = None
from azure.identity import ClientSecretCredential
from dotenv import load_dotenv

from history_store import ConversationStore
from telemetry_store import TelemetryStore
from firebase_auth import (
    FirebaseConfig,
    FirebaseAuthClient,
    FirebaseAuthError,
    validate_email,
    validate_password,
)
from firestore_service import (
    FirestoreClient,
    FirestoreError,
    FirestorePermissionError,
    categorize_prompt,
)

load_dotenv()

PROJECT_ENDPOINT = st.secrets["FOUNDRY_PROJECT_ENDPOINT"]
AGENT_NAME = "IT-Helpdesk-Agent"
AGENT_VERSION = st.secrets.get("FOUNDRY_AGENT_VERSION") or None

AZURE_CLIENT_ID = st.secrets["AZURE_CLIENT_ID"]
AZURE_TENANT_ID = st.secrets["AZURE_TENANT_ID"]
AZURE_CLIENT_SECRET = st.secrets["AZURE_CLIENT_SECRET"]

credential = ClientSecretCredential(
    tenant_id=AZURE_TENANT_ID,
    client_id=AZURE_CLIENT_ID,
    client_secret=AZURE_CLIENT_SECRET,
)

STORE = ConversationStore(Path(__file__).with_name("chat_history.db"))
TELEMETRY = TelemetryStore(Path(__file__).with_name("chat_history.db"))

MAX_CONTEXT_MESSAGES = 12
MAX_CONTEXT_CHARS = 12000


async def get_agent_response(prompt, session):
    if FoundryAgent is None:
        raise RuntimeError("The agent-framework-foundry package is not installed in this environment.")
    async with FoundryAgent(
        project_endpoint=PROJECT_ENDPOINT,
        agent_name=AGENT_NAME,
        agent_version=AGENT_VERSION,
        credential=credential,
    ) as agent:
        if session is None:
            session = agent.create_session()

        response = await agent.run(prompt, session=session)
        return str(response), session


def start_new_conversation():
    st.session_state.conversation_id = None
    st.session_state.messages = []
    st.session_state.agent_session = None
    st.session_state.pending_feedback_request_id = None
    st.session_state.page = "active"


def show_analytics():
    """Render analytics from request events, conversations, feedback, and tickets."""
    st.markdown(
        """
        <div class="main-view-header">
          <div class="view-heading">IT Support Analytics Dashboard</div>
          <div class="status-indicator">Aggregated support metrics & observability</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    ranges = {
        "Last 24 hours": timedelta(hours=24),
        "Last 7 days": timedelta(days=7),
        "Last 30 days": timedelta(days=30),
        "All time": None,
    }
    selected_range = st.selectbox("Time range", tuple(ranges), index=2)
    duration = ranges[selected_range]
    start_at = None if duration is None else (datetime.now(UTC) - duration).isoformat()

    summary = TELEMETRY.summary(start_at)
    total_requests = summary["total_requests"]
    total_conversations = TELEMETRY.conversation_count(start_at)
    ticket_count = TELEMETRY.ticket_count(start_at)
    success_rate = summary["agent_success_rate"]
    success_rate_display = "—" if success_rate is None else f"{success_rate:.1f}%"

    st.markdown("### Support Overview")
    kpi_cols = st.columns(4)
    kpi_cols[0].metric("Support conversations", total_conversations)
    kpi_cols[1].metric("Support requests", total_requests)
    kpi_cols[2].metric("Support tickets", ticket_count)
    kpi_cols[3].metric("Agent success rate", success_rate_display)

    st.markdown("---")

    # Resolved vs Escalated issues
    st.subheader("Resolved vs Escalated Issues")
    comp = TELEMETRY.resolution_vs_escalation(start_at)
    res_rate_display = "—" if comp["resolution_rate"] is None else f"{comp['resolution_rate']:.1f}%"
    esc_rate_display = "—" if comp["escalation_rate"] is None else f"{comp['escalation_rate']:.1f}%"

    res_cols = st.columns(4)
    res_cols[0].metric("Resolved issues", comp["resolved_count"])
    res_cols[1].metric("Escalated issues", comp["escalated_count"])
    res_cols[2].metric("Resolution rate", res_rate_display)
    res_cols[3].metric("Escalation rate", esc_rate_display)

    if comp["total_cases"] > 0:
        comparison_data = [
            {"Status": "Resolved", "Cases": comp["resolved_count"]},
            {"Status": "Escalated", "Cases": comp["escalated_count"]},
        ]
        st.bar_chart(comparison_data, x="Status", y="Cases")
        st.caption("Compares user-confirmed issue resolutions against unresolved issues requiring escalation.")
    else:
        st.info("No resolution or escalation outcomes recorded for this time period.")

    st.markdown("---")

    # Issue Category Distribution & Most Common IT Categories
    st.subheader("Issue Categories")
    categories = TELEMETRY.category_distribution(start_at)
    top_category = TELEMETRY.most_common_category(start_at)

    cat_cols = st.columns(2)
    with cat_cols[0]:
        if top_category:
            top_desc = f"{top_category['label']} ({top_category['requests']} requests, {top_category['percentage']}%)"
        else:
            top_desc = "None recorded"
        st.metric("Most common issue category", top_desc)

    with cat_cols[1]:
        categorized_count = sum(c["requests"] for c in categories) if categories else 0
        st.metric("Categorized issues", categorized_count)

    if categories:
        cat_chart_data = [{"Category": row["label"], "Requests": row["requests"]} for row in categories]
        st.bar_chart(cat_chart_data, x="Category", y="Requests")
        st.dataframe(
            [{"Category": row["label"], "Requests": row["requests"]} for row in categories],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No categorized issue requests recorded in this period.")

    st.markdown("---")

    # Basic usage trends over time
    st.subheader("Usage Trends Over Time")
    trends = TELEMETRY.usage_trends(start_at)
    if trends:
        st.bar_chart(trends, x="day", y="requests")
        st.caption("Daily request activity over the selected time range.")
    else:
        st.info("No usage activity recorded for this period.")

    st.markdown("---")

    # Performance & Observability
    st.subheader("System Performance & Reliability")
    average_latency = summary["average_latency_ms"]
    latency_display = "—" if average_latency is None else f"{average_latency / 1000:.2f} s"
    p95_latency = summary["p95_latency_ms"]
    p95_display = "—" if p95_latency is None else f"{p95_latency / 1000:.2f} s"

    perf_cols = st.columns(3)
    perf_cols[0].metric("Successful agent requests", summary["successful_requests"])
    perf_cols[1].metric("Fallback requests", summary["failed_requests"])
    perf_cols[2].metric("Average response latency", latency_display)

    failures = TELEMETRY.failure_breakdown(start_at)
    if failures:
        st.write("**Failure observability**")
        st.dataframe(failures, use_container_width=True, hide_index=True)
        st.caption("Failure details include stage, sanitized exception type, and average response latency.")
    else:
        st.success("No Foundry service failures recorded.")

    st.caption(
        "Privacy guarantee: Analytics dashboard aggregates application metrics only. "
        "Prompts, user messages, free-form feedback text, and credentials are never stored in or exposed by analytics."
    )


def load_conversation(conversation_id):
    messages = STORE.load_messages(conversation_id)

    if not messages:
        start_new_conversation()
        return

    st.session_state.conversation_id = conversation_id
    st.session_state.messages = messages
    st.session_state.agent_session = None
    try:
        st.session_state.pending_feedback_request_id = TELEMETRY.latest_request_id(conversation_id)
    except Exception:
        st.session_state.pending_feedback_request_id = None
    st.session_state.page = "active"


def ensure_session_state():
    defaults = {
        "conversation_id": None,
        "messages": [],
        "agent_session": None,
        "pending_feedback_request_id": None,
        "page": "active",
        "theme": "dark",
        "auth_user": None,
        "auth_view": "login",
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def logout():
    """Thoroughly purge session state, cache, and authentication credentials."""
    st.session_state.auth_user = None
    st.session_state.conversation_id = None
    st.session_state.messages = []
    st.session_state.agent_session = None
    st.session_state.pending_feedback_request_id = None
    st.session_state.page = "active"
    st.session_state.auth_view = "login"
    try:
        st.cache_data.clear()
    except Exception:
        pass
    st.rerun()


def show_profile_view():
    """Render the User Profile Dashboard with Firestore activity history and data isolation."""
    user = st.session_state.auth_user
    if not user:
        show_auth_view()
        st.stop()

    uid = user.get("uid") or user.get("localId") or "Unknown"
    email = user.get("email") or "Unknown"

    st.markdown(
        """
        <div class="main-view-header">
          <div class="view-heading">User Profile & IT Support Dashboard</div>
          <div class="status-indicator">Authenticated Identity & History</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 1. USER PROFILE SECTION
    st.subheader("Account Identity")
    prof_col1, prof_col2 = st.columns(2)
    with prof_col1:
        st.markdown(f"**Email:** `{email}`")
        st.markdown(f"**Firebase UID:** `{uid}`")
    with prof_col2:
        st.markdown("**Account Status:** Active")
        st.markdown("**Authentication Authority:** Firebase Authentication")

    st.markdown("---")

    # 2. HELP DESK ACTIVITY SECTION
    st.subheader("My Helpdesk Activity")

    activities = []
    fetch_error = None
    cloud_synced = False
    config = FirebaseConfig.load()

    if config and config.project_id:
        try:
            client = FirestoreClient(
                project_id=config.project_id,
                id_token=user.get("id_token"),
            )
            # Enforce user data isolation: caller_uid must match uid
            activities = client.get_user_activity(uid=uid, caller_uid=uid)
            cloud_synced = True
        except (FirestorePermissionError, FirestoreError) as err:
            # Check if token expired and can be refreshed
            refreshed = False
            refresh_tok = user.get("refresh_token")
            if refresh_tok:
                try:
                    auth_client = FirebaseAuthClient(config)
                    toks = auth_client.refresh_token(refresh_tok)
                    if toks.get("id_token"):
                        user["id_token"] = toks["id_token"]
                        if toks.get("refresh_token"):
                            user["refresh_token"] = toks["refresh_token"]
                        st.session_state.auth_user = user
                        client = FirestoreClient(
                            project_id=config.project_id,
                            id_token=toks["id_token"],
                        )
                        activities = client.get_user_activity(uid=uid, caller_uid=uid)
                        cloud_synced = True
                        refreshed = True
                except Exception:
                    pass

            if not refreshed:
                if isinstance(err, FirestorePermissionError):
                    fetch_error = "Access denied: Firestore security rules prevented loading history."
                else:
                    fetch_error = f"Database notice: {err.message}"
        except Exception:
            fetch_error = "Could not connect to Cloud Firestore at this time."
    else:
        fetch_error = "Cloud Firestore project is not configured in `.streamlit/secrets.toml`."

    # Fallback to local SQLite store if Firestore returned no activity or encountered a fetch error
    is_local_fallback = False
    if not activities and STORE:
        try:
            local_questions = STORE.get_recent_user_questions(limit=50)
            if local_questions:
                activities = [
                    {
                        "id": str(q.get("id", idx)),
                        "question": q.get("question", ""),
                        "category": categorize_prompt(q.get("question", "")),
                        "conversation_id": q.get("conversation_id", ""),
                        "timestamp": q.get("timestamp", ""),
                    }
                    for idx, q in enumerate(local_questions, 1)
                ]
                is_local_fallback = True
        except Exception:
            pass

    if fetch_error:
        if is_local_fallback:
            st.info(
                "ℹ️ **Local History Active**: Displaying your questions from local storage. "
                "Cloud sync is waiting for your Firestore security rules to be published in Firebase Console."
            )
        else:
            st.caption(f"ℹ️ {fetch_error}")

        proj_id = config.project_id if config and config.project_id else "it-helpdesk-agent-4e8c8"
        with st.expander("🛠️ How to fix Firestore Security Rules in Firebase Console", expanded=not is_local_fallback):
            st.markdown(
                f"""
                **Why does this happen?**  
                By default, a new Firebase Firestore database starts in *locked mode* (`allow read, write: false;`), blocking client queries until you publish security rules.

                **Steps to fix in under 1 minute:**
                1. Open the [Firebase Console Rules](https://console.firebase.google.com/project/{proj_id}/firestore/rules).
                2. If you haven't created a database yet, click **Create database** (choose **Native mode** and standard location).
                3. In the **Rules** tab, replace the contents with:
                ```javascript
                rules_version = '2';
                service cloud.firestore {{
                  match /databases/{{database}}/documents {{
                    match /users/{{userId}}/{{document=**}} {{
                      allow read, write: if request.auth != null && request.auth.uid == userId;
                    }}
                    match /{{document=**}} {{
                      allow read, write: false;
                    }}
                  }}
                }}
                ```
                4. Click **Publish**. Then refresh this page!
                """
            )

    total_questions = len(activities)
    distinct_conversations = len(set(a["conversation_id"] for a in activities if a.get("conversation_id")))
    latest_cat = activities[0].get("category", "—") if activities else "—"

    kpi1, kpi2, kpi3 = st.columns(3)
    kpi1.metric("Total Questions", total_questions)
    kpi2.metric("Total Conversations", distinct_conversations)
    kpi3.metric("Latest Issue Category", latest_cat)

    st.markdown("#### Recent Questions")

    if not activities:
        st.info("No helpdesk activity yet.")
    else:
        for idx, act in enumerate(activities[:20], 1):
            q_text = act.get("question", "Untitled question")
            category = act.get("category", "General")
            ts = act.get("timestamp", "")
            conv_id = act.get("conversation_id", "")
            date_display = ts[:16].replace("T", " ") if ts else "Recent"

            card_col, action_col = st.columns([4, 1])
            with card_col:
                st.markdown(
                    f"""
                    <div style="padding: 10px 14px; margin-bottom: 8px; background: var(--surface2); border: 1px solid var(--border2); border-radius: 8px;">
                      <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px;">
                        <span style="font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--accent);">{category}</span>
                        <span style="font-size: 11px; color: var(--dim);">{date_display}</span>
                      </div>
                      <div style="font-size: 14px; font-weight: 500; color: var(--text);">{q_text}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            with action_col:
                if conv_id:
                    if st.button("Open Chat", key=f"prof_open_{act.get('id', idx)}", use_container_width=True):
                        load_conversation(conv_id)
                        st.session_state.page = "active"
                        st.rerun()

    st.markdown("---")

    # 3. SIGN OUT BUTTON
    st.subheader("Session Management")
    st.caption("Sign out to end your authenticated session on this browser.")
    if st.button("Sign out from IT Helpdesk", type="primary", key="profile_logout_btn"):
        logout()


def show_auth_view():
    """Render the authentication screen (Login / Signup) and stop script execution."""
    config = FirebaseConfig.load()

    st.markdown(
        """
        <div class="auth-container">
            <div class="auth-brand-icon">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M12 2L2 7l10 5 10-5-10-5z"></path>
                    <path d="M2 17l10 5 10-5"></path>
                    <path d="M2 12l10 5 10-5"></path>
                </svg>
            </div>
            <div class="auth-title">IT Helpdesk Console</div>
            <div class="auth-subtitle">AI Diagnostics & Support Portal</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not config:
        st.warning(
            "Firebase Authentication is not configured. "
            "Please configure your Firebase credentials in `.streamlit/secrets.toml` or set environment variables."
        )
        with st.expander("Configuration Instructions", expanded=False):
            st.markdown(
                """
                Create or edit `.streamlit/secrets.toml`:
                ```toml
                [firebase]
                api_key = "AIzaSy..."
                auth_domain = "your-project.firebaseapp.com"
                project_id = "your-project"
                storage_bucket = "your-project.appspot.com"
                messaging_sender_id = "..."
                app_id = "..."
                ```
                Or set the `FIREBASE_API_KEY` environment variable.
                """
            )
        st.stop()

    client = FirebaseAuthClient(config)
    is_login = st.session_state.auth_view == "login"

    _, center_col, _ = st.columns([1, 2, 1])

    with center_col:
        st.markdown(
            f"""
            <div class="auth-mode-heading">
                <h3>{"Sign In" if is_login else "Create Account"}</h3>
                <p>{"Enter your credentials to access the IT Helpdesk" if is_login else "Sign up with email and password to access the portal"}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if is_login:
            with st.form("login_form", clear_on_submit=False):
                email = st.text_input("Email address", placeholder="name@company.com", key="login_email").strip()
                show_pw = st.checkbox("Show password", key="login_show_pw")
                password = st.text_input(
                    "Password",
                    type="default" if show_pw else "password",
                    placeholder="Enter your password",
                    key="login_password",
                )
                submitted = st.form_submit_button("Sign In", use_container_width=True, type="primary")

                if submitted:
                    valid_email, email_err = validate_email(email)
                    if not valid_email:
                        st.error(email_err)
                    elif not password:
                        st.error("Password is required.")
                    else:
                        with st.spinner("Signing in..."):
                            try:
                                user = client.sign_in(email, password)
                                st.session_state.auth_user = user
                                st.rerun()
                            except FirebaseAuthError as err:
                                st.error(err.message)

            st.markdown('<div class="auth-switch-prompt">Don\'t have an account?</div>', unsafe_allow_html=True)
            if st.button("Create an account", use_container_width=True, key="btn_switch_signup"):
                st.session_state.auth_view = "signup"
                st.rerun()

        else:
            with st.form("signup_form", clear_on_submit=False):
                email = st.text_input("Email address", placeholder="name@company.com", key="signup_email").strip()
                show_pw = st.checkbox("Show password", key="signup_show_pw")
                password = st.text_input(
                    "Password",
                    type="default" if show_pw else "password",
                    placeholder="At least 6 characters",
                    key="signup_password",
                )
                confirm_password = st.text_input(
                    "Confirm Password",
                    type="default" if show_pw else "password",
                    placeholder="Re-enter your password",
                    key="signup_confirm_password",
                )
                submitted = st.form_submit_button("Create Account", use_container_width=True, type="primary")

                if submitted:
                    valid_email, email_err = validate_email(email)
                    valid_pw, pw_err = validate_password(password)
                    if not valid_email:
                        st.error(email_err)
                    elif not valid_pw:
                        st.error(pw_err)
                    elif password != confirm_password:
                        st.error("Passwords do not match. Please re-enter your password.")
                    else:
                        with st.spinner("Creating account..."):
                            try:
                                user = client.sign_up(email, password)
                                st.session_state.auth_user = user
                                st.rerun()
                            except FirebaseAuthError as err:
                                st.error(err.message)

            st.markdown('<div class="auth-switch-prompt">Already have an account?</div>', unsafe_allow_html=True)
            if st.button("Sign in instead", use_container_width=True, key="btn_switch_login"):
                st.session_state.auth_view = "login"
                st.rerun()

    st.stop()


def show_resolution_feedback():
    """Offer explicit resolution feedback for the latest exact support request."""
    request_id = st.session_state.pending_feedback_request_id
    if request_id is None:
        return

    try:
        feedback = TELEMETRY.feedback_for_request(request_id)
    except Exception:
        st.caption("Feedback is temporarily unavailable. Your support chat is unaffected.")
        return
    st.markdown("**Resolution Confirmation** &mdash; *Was this guidance helpful in resolving your issue?*")
    if feedback is not None:
        confirmation = (
            "Thanks for confirming that your issue was resolved."
            if feedback["resolution_status"] == "resolved"
            else "Thanks for letting us know you still need help."
        )
        st.success(confirmation)
        return

    feedback_text = st.text_area(
        "Optional feedback",
        key=f"feedback-comment-{request_id}",
        max_chars=TelemetryStore.MAX_FEEDBACK_TEXT_LENGTH,
        placeholder="Add a short comment if you would like to share more context.",
        height=80,
    )
    resolved_column, unresolved_column = st.columns(2)
    selected_status = None
    with resolved_column:
        if st.button("Mark as Resolved", key=f"feedback-resolved-{request_id}"):
            selected_status = "resolved"
    with unresolved_column:
        if st.button("Request Escalation", key=f"feedback-unresolved-{request_id}"):
            selected_status = "not_resolved"

    if selected_status is None:
        return
    try:
        TELEMETRY.record_feedback(request_id, selected_status, feedback_text)
    except ValueError:
        st.info("Feedback was already recorded for this support request.")
    except Exception:
        st.warning("Feedback could not be saved. Your support chat is unaffected; please try again.")
    else:
        st.success("Thanks for your feedback.")


def prompt_with_saved_context(messages, prompt):
    if not messages:
        return prompt

    selected = messages[-MAX_CONTEXT_MESSAGES:]
    parts = []
    total = 0

    for message in reversed(selected):
        part = f"{message['role'].title()}: {message['content'].strip()}"

        if total + len(part) > MAX_CONTEXT_CHARS:
            remaining = MAX_CONTEXT_CHARS - total
            if remaining > 80:
                parts.append(part[:remaining] + "...")
            break

        parts.append(part)
        total += len(part)

    newline = chr(10)
    transcript = newline.join(reversed(parts))

    return (
        "Continue the following IT helpdesk conversation. "
        "Use it only as context and answer the latest request. "
        "Do not mention this context block."
        + newline + newline
        + "Previous conversation:" + newline
        + transcript
        + newline + newline
        + "Latest user request: " + prompt
    )


def local_troubleshooting_response(prompt):
    request = prompt.lower()

    if any(x in request for x in ("internet", "wi-fi", "wifi", "network")):
        category = "network"
        steps = [
            "Check that airplane mode is off and reconnect to the Wi-Fi network.",
            "Check whether other devices are also offline.",
            "Restart your computer and run the network troubleshooter.",
            "Record the exact error and network name if the issue continues.",
        ]
    elif "vpn" in request:
        category = "vpn"
        steps = [
            "Confirm that your normal internet connection works.",
            "Disconnect and reconnect the VPN once.",
            "Verify your approved sign-in details.",
            "Record the exact VPN error and contact IT if it continues.",
        ]
    elif any(x in request for x in ("password", "login", "sign in", "account")):
        category = "account"
        steps = [
            "Use the approved password-reset process and never share your password.",
            "Check Caps Lock and confirm the correct work account.",
            "Verify your internet connection if MFA approval does not arrive.",
            "Contact IT if the account remains inaccessible.",
        ]
    else:
        category = "general"
        steps = [
            "Restart the affected application or device.",
            "Check whether the issue affects other applications or devices.",
            "Record the exact error message and when the issue started.",
            "Contact IT with those details if the issue continues.",
        ]

    response = (
        "Microsoft Foundry is currently unavailable. "
        "Here is first-line troubleshooting guidance:\n\n"
        + "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
    )
    return response, category


USER_AVATAR = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%2394a3b8'>"
    "<path d='M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z'/>"
    "</svg>"
)

ASSISTANT_AVATAR = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%2310b981'>"
    "<path d='M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5'/>"
    "</svg>"
)


st.set_page_config(
    page_title="IT Helpdesk | AI Diagnostics",
    layout="wide",
)

ensure_session_state()

is_dark = st.session_state.theme == "dark"

if is_dark:
    colors = {
        "bg": "#0e1015",
        "surface": "#161922",
        "surface2": "#1e222e",
        "surface_card": "#181c26",
        "hover": "#252b3b",
        "border": "rgba(255, 255, 255, 0.08)",
        "border2": "rgba(255, 255, 255, 0.16)",
        "text": "#f3f4f6",
        "muted": "#9ca3af",
        "dim": "#6b7280",
        "accent": "#10b981",
        "accent_hover": "#059669",
        "user_bubble": "#1e222e",
        "shadow": "rgba(0, 0, 0, 0.45)",
    }
else:
    colors = {
        "bg": "#f8fafc",
        "surface": "#ffffff",
        "surface2": "#f1f5f9",
        "surface_card": "#ffffff",
        "hover": "#e2e8f0",
        "border": "#e2e8f0",
        "border2": "#cbd5e1",
        "text": "#0f172a",
        "muted": "#475569",
        "dim": "#94a3b8",
        "accent": "#059669",
        "accent_hover": "#047857",
        "user_bubble": "#f1f5f9",
        "shadow": "rgba(0, 0, 0, 0.06)",
    }

st.markdown(
f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

:root {{
  --bg: {colors["bg"]};
  --surface: {colors["surface"]};
  --surface2: {colors["surface2"]};
  --surface-card: {colors["surface_card"]};
  --hover: {colors["hover"]};
  --border: {colors["border"]};
  --border2: {colors["border2"]};
  --text: {colors["text"]};
  --muted: {colors["muted"]};
  --dim: {colors["dim"]};
  --accent: {colors["accent"]};
  --accent-hover: {colors["accent_hover"]};
  --user-bubble: {colors["user_bubble"]};
  --shadow: {colors["shadow"]};
}}

html, body, .stApp, [class*="css"] {{
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
}}

.stApp {{
  background-color: var(--bg) !important;
  color: var(--text) !important;
}}

/* REFINED HEADER & FIXED TOGGLE ICON */
[data-testid="stHeader"] {{
  background: transparent !important;
  border: none !important;
  height: 0px !important;
  z-index: 1000 !important;
}}

[data-testid="collapsedControl"] {{
  position: fixed !important;
  top: 14px !important;
  left: 16px !important;
  background-color: var(--surface2) !important;
  border: 1px solid var(--border2) !important;
  border-radius: 8px !important;
  box-shadow: 0 4px 12px var(--shadow) !important;
  z-index: 9999 !important;
  padding: 6px !important;
  margin: 0 !important;
  transition: background-color 0.2s ease !important;
}}

[data-testid="collapsedControl"]:hover {{
  background-color: var(--hover) !important;
}}

[data-testid="collapsedControl"] svg {{
  fill: #ffffff !important; 
  color: #ffffff !important;
}}

.top-header-main {{
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 24px 16px 64px;
  border-bottom: 1px solid var(--border);
  background-color: var(--bg);
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 998;
  height: 64px;
}}

#MainMenu, footer {{
  visibility: hidden !important;
}}

[data-testid="stSidebar"] {{
  background-color: var(--surface) !important;
  border-right: 1px solid var(--border) !important;
  min-width: 260px !important;
  max-width: 290px !important;
  z-index: 9999 !important;
}}

[data-testid="stSidebar"] > div:first-child {{
  padding: 16px 12px 14px !important;
}}

.brand-wrapper {{
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 4px 8px 16px 8px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 14px;
}}

.brand-icon {{
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: var(--surface2);
  border: 1px solid var(--border2);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}}

.brand-title {{
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  letter-spacing: -0.01em;
  line-height: 1.2;
}}

.brand-tag {{
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--dim);
  font-weight: 600;
}}

.sidebar-category {{
  margin: 18px 8px 6px 8px;
  font-size: 10px;
  font-weight: 700;
  color: var(--dim);
  text-transform: uppercase;
  letter-spacing: 0.08em;
}}

[data-testid="stSidebar"] .stButton {{
  margin-bottom: 3px !important;
}}

[data-testid="stSidebar"] .stButton > button {{
  width: 100% !important;
  height: 38px !important;
  min-height: 38px !important;
  margin: 0 !important;
  padding: 8px 12px !important;
  display: flex !important;
  align-items: center !important;
  justify-content: flex-start !important;
  text-align: left !important;
  border: 1px solid transparent !important;
  border-radius: 8px !important;
  background-color: transparent !important;
  color: var(--muted) !important;
  font-size: 13px !important;
  font-weight: 500 !important;
  transition: all 0.15s ease !important;
}}

[data-testid="stSidebar"] .stButton > button div,
[data-testid="stSidebar"] .stButton > button p,
[data-testid="stSidebar"] .stButton > button span {{
  width: 100% !important;
  text-align: left !important;
  justify-content: flex-start !important;
  margin: 0 !important;
  display: block !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
  white-space: nowrap !important;
}}

[data-testid="stSidebar"] .stButton > button:hover {{
  background-color: var(--hover) !important;
  color: var(--text) !important;
}}

[data-testid="stSidebar"] button[kind="primary"] {{
  background-color: var(--surface2) !important;
  border: 1px solid var(--border2) !important;
  color: var(--text) !important;
  margin-bottom: 8px !important;
  font-weight: 600 !important;
}}

[data-testid="stSidebar"] button[kind="primary"]:hover {{
  background-color: var(--hover) !important;
  border-color: var(--accent) !important;
}}

.block-container {{
  max-width: 820px !important;
  margin: 0 auto !important;
  padding: 100px 24px 130px 24px !important;
}}

.hero-box {{
  text-align: center;
  padding: 44px 12px 28px;
}}

.hero-badge {{
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  background: var(--surface2);
  border: 1px solid var(--border2);
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  margin-bottom: 16px;
}}

.status-pulse {{
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background-color: var(--accent);
  box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
  animation: pulse-indicator 2s infinite cubic-bezier(0.66, 0, 0, 1);
  display: inline-block;
}}

@keyframes pulse-indicator {{
  0% {{ box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }}
  70% {{ box-shadow: 0 0 0 7px rgba(16, 185, 129, 0); }}
  100% {{ box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }}
}}

.hero-title {{
  font-size: 28px;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 8px;
  letter-spacing: -0.02em;
}}

.hero-desc {{
  font-size: 14px;
  color: var(--muted);
  max-width: 480px;
  margin: 0 auto;
  line-height: 1.55;
}}

.main-view-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 14px;
  margin-bottom: 20px;
  border-bottom: 1px solid var(--border);
}}

.view-heading {{
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
}}

.status-indicator {{
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--muted);
}}

[data-testid="stMainBlockContainer"] .stButton > button {{
  background-color: var(--surface-card) !important;
  border: 1px solid var(--border) !important;
  color: var(--text) !important;
  border-radius: 12px !important;
  padding: 16px 20px !important;
  min-height: 84px !important;
  display: flex !important;
  flex-direction: column !important;
  justify-content: center !important;
  align-items: flex-start !important;
  text-align: left !important;
  transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1) !important;
  box-shadow: 0 2px 8px var(--shadow) !important;
}}

[data-testid="stMainBlockContainer"] .stButton > button:hover {{
  background-color: var(--surface2) !important;
  border-color: var(--accent) !important;
  transform: translateY(-2px) !important;
  box-shadow: 0 8px 24px var(--shadow) !important;
}}

[data-testid="stMainBlockContainer"] .stButton > button p {{
  color: var(--text) !important;
  white-space: pre-wrap !important;
  line-height: 1.5 !important;
  margin: 0 !important;
}}

.diagnostic-banner {{
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  background: rgba(245, 158, 11, 0.08);
  border: 1px solid rgba(245, 158, 11, 0.25);
  border-radius: 8px;
  font-size: 13px;
  color: #fbbf24;
  margin: 12px 0 16px 0;
  line-height: 1.4;
}}

.diagnostic-badge {{
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  padding: 3px 8px;
  border-radius: 4px;
  background: rgba(245, 158, 11, 0.2);
  color: #fbbf24;
  white-space: nowrap;
}}

[data-testid="stChatMessage"] {{
  background: transparent !important;
  border: none !important;
  padding: 14px 4px !important;
  margin: 0 auto !important;
}}

[data-testid="stChatMessage"] p, [data-testid="stChatMessage"] li {{
  font-size: 14.5px !important;
  line-height: 1.65 !important;
  color: var(--text) !important;
}}

[data-testid="stChatMessage"] pre, [data-testid="stChatMessage"] code {{
  font-family: 'JetBrains Mono', monospace !important;
  font-size: 13px !important;
}}

[data-testid="stBottom"] {{
  background: var(--bg) !important;
  border: none !important;
}}

[data-testid="stBottom"] > div {{
  background: transparent !important;
}}

[data-testid="stChatInput"] {{
  max-width: 820px !important;
  margin: 0 auto !important;
  padding: 10px 0 16px 0 !important;
  background: transparent !important;
}}

[data-testid="stChatInput"] > div {{
  background-color: var(--surface2) !important;
  border: 1px solid var(--border2) !important;
  border-radius: 20px !important;
  padding: 4px 10px !important;
  box-shadow: 0 4px 20px var(--shadow) !important;
}}

[data-testid="stChatInput"] > div:focus-within {{
  border-color: var(--accent) !important;
}}

[data-testid="stChatInput"] textarea {{
  font-size: 14px !important;
  color: var(--text) !important;
  background: transparent !important;
}}

[data-testid="stChatInput"] textarea::placeholder {{
  color: var(--dim) !important;
}}

[data-testid="stChatInput"] button {{
  background-color: var(--accent) !important;
  color: #ffffff !important;
  border: none !important;
  border-radius: 12px !important;
  transition: background-color 0.15s ease !important;
}}

[data-testid="stChatInput"] button:hover {{
  background-color: var(--accent-hover) !important;
}}

/* AUTHENTICATION & USER PROFILE STYLES */
.auth-container {{
  max-width: 480px;
  margin: 20px auto;
  text-align: center;
}}

.auth-brand-icon {{
  width: 48px;
  height: 48px;
  border-radius: 12px;
  background: var(--surface2);
  border: 1px solid var(--border2);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 12px;
}}

.auth-title {{
  font-size: 22px;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 4px;
  letter-spacing: -0.02em;
}}

.auth-subtitle {{
  font-size: 13px;
  color: var(--muted);
  margin-bottom: 24px;
}}

.auth-mode-heading {{
  margin-bottom: 18px;
  text-align: center;
}}

.auth-mode-heading h3 {{
  font-size: 18px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 4px;
}}

.auth-mode-heading p {{
  font-size: 13px;
  color: var(--muted);
  margin: 0;
}}

.auth-switch-prompt {{
  text-align: center;
  font-size: 13px;
  color: var(--muted);
  margin: 16px 0 8px 0;
}}

.user-card {{
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background-color: var(--surface2);
  border: 1px solid var(--border2);
  border-radius: 8px;
  margin-bottom: 12px;
}}

.user-badge-icon {{
  width: 24px;
  height: 24px;
  border-radius: 6px;
  background: rgba(16, 185, 129, 0.15);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: var(--accent);
}}

.user-email-text {{
  font-size: 12px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}

[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"] {{
  gap: 8px !important;
  margin-bottom: 8px !important;
}}

[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"] .stButton > button {{
  background-color: var(--surface2) !important;
  border: 1px solid var(--border2) !important;
  border-radius: 8px !important;
  color: var(--text) !important;
  font-size: 12px !important;
  font-weight: 500 !important;
  padding: 6px 10px !important;
  height: 36px !important;
  min-height: 36px !important;
  justify-content: center !important;
  text-align: center !important;
}}

[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"] .stButton > button:hover {{
  background-color: var(--hover) !important;
  border-color: var(--accent) !important;
}}

[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"] .stButton > button div,
[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"] .stButton > button p,
[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"] .stButton > button span {{
  text-align: center !important;
  justify-content: center !important;
  font-size: 12px !important;
}}
</style>
""",
unsafe_allow_html=True,
)

if st.session_state.auth_user is None:
    show_auth_view()

st.markdown(
    """
    <div class="top-header-main">
        <div class="brand-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 2L2 7l10 5 10-5-10-5z"></path>
                <path d="M2 17l10 5 10-5"></path>
                <path d="M2 12l10 5 10-5"></path>
            </svg>
        </div>
        <div class="brand-title">IT Helpdesk Console</div>
    </div>
    """,
    unsafe_allow_html=True,
)


with st.sidebar:
    st.markdown(
        """
        <div class="brand-wrapper">
          <div class="brand-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 2L2 7l10 5 10-5-10-5z"></path>
                <path d="M2 17l10 5 10-5"></path>
                <path d="M2 12l10 5 10-5"></path>
            </svg>
          </div>
          <div>
            <div class="brand-title">IT Helpdesk</div>
            <div class="brand-tag">AI Diagnostics</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    user_email = (st.session_state.auth_user or {}).get("email") or "Authenticated User"
    st.markdown(
        f"""
        <div class="user-card">
          <div class="user-badge-icon">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
                <circle cx="12" cy="7" r="4"></circle>
            </svg>
          </div>
          <div class="user-email-text" title="{user_email}">{user_email}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col_signout, col_profile = st.columns(2, gap="small")
    with col_signout:
        if st.button("Sign out", use_container_width=True, key="btn_signout"):
            logout()
    with col_profile:
        if st.button("🪪 My Profile", use_container_width=True, key="btn_profile_top"):
            st.session_state.page = "profile"
            st.rerun()

    if st.button("+  New conversation", use_container_width=True, type="primary"):
        start_new_conversation()
        st.session_state.page = "active"
        st.rerun()

    st.markdown('<div class="sidebar-category">Recent Sessions</div>', unsafe_allow_html=True)

    conversations = STORE.list_conversations()

    if conversations:
        for conversation in conversations[:10]:
            is_current = conversation["id"] == st.session_state.conversation_id
            prefix = "•  " if is_current else "   "
            title = prefix + conversation["title"]
            if len(title) > 30:
                title = title[:28] + "..."

            if st.button(
                title,
                key=f"recent-{conversation['id']}",
                use_container_width=True,
            ):
                load_conversation(conversation["id"])
                st.session_state.page = "active"
                st.rerun()
    else:
        st.caption("No conversations yet.")

    st.markdown('<div class="sidebar-category">Navigation</div>', unsafe_allow_html=True)

    if st.button("🏠  Helpdesk", use_container_width=True, key="nav_helpdesk"):
        st.session_state.page = "active"
        st.rerun()

    if st.button("Conversation archive", use_container_width=True, key="nav_history"):
        st.session_state.page = "history"
        st.rerun()

    if st.button("Support analytics", use_container_width=True, key="nav_analytics"):
        st.session_state.page = "analytics"
        st.rerun()

    theme_toggle_label = "Switch to light theme" if is_dark else "Switch to dark theme"
    if st.button(theme_toggle_label, use_container_width=True, key="nav_theme"):
        st.session_state.theme = "light" if is_dark else "dark"
        st.rerun()


if st.session_state.page == "profile":
    show_profile_view()
    st.stop()

if st.session_state.page == "history":
    st.markdown(
        """
        <div class="main-view-header">
          <div class="view-heading">Conversation Archive</div>
          <div class="status-indicator">All Saved Sessions</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not conversations:
        st.info("No saved conversations yet.")
    else:
        for conversation in conversations:
            if st.button(
                f"•  {conversation['title']}",
                key=f"history-{conversation['id']}",
                use_container_width=True,
            ):
                load_conversation(conversation["id"])
                st.rerun()

    st.stop()

if st.session_state.page == "analytics":
    show_analytics()
    st.stop()


selected_quick_prompt = None

if not st.session_state.messages:
    st.markdown(
        """
        <div class="hero-box">
          <div class="hero-badge">
            <span class="status-pulse"></span>
            <span>IT support ready</span>
          </div>
          <div class="hero-title">IT Diagnostics Console</div>
          <div class="hero-desc">Ask a diagnostic question, check network status, or initiate automated troubleshooting.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button(
            "NETWORK: Wi-Fi & Connectivity\nDiagnose gateway routing & DNS configuration",
            key="card_wifi",
            use_container_width=True,
        ):
            selected_quick_prompt = "My Wi-Fi is connected but the internet is not working."
        if st.button(
            "ACCESS: Password Reset & MFA\nRecover account access or verify authenticator",
            key="card_pwd",
            use_container_width=True,
        ):
            selected_quick_prompt = "I need help resetting my work password and signing in."
    with col2:
        if st.button(
            "VPN: Secure Tunnel Connection\nResolve timeout and authentication session drops",
            key="card_vpn",
            use_container_width=True,
        ):
            selected_quick_prompt = "My VPN is disconnecting and failing to authenticate."
        if st.button(
            "SOFTWARE: Workplace App Crash\nRemediate unresponsive workplace software",
            key="card_app",
            use_container_width=True,
        ):
            selected_quick_prompt = "My required work application is crashing immediately on launch."
else:
    st.markdown(
        """
        <div class="main-view-header">
          <div class="view-heading">IT Diagnostics Console</div>
          <div class="status-indicator">
            <span class="status-pulse"></span>
            <span>IT support ready</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

for message in st.session_state.messages:
    with st.chat_message(message["role"], avatar=USER_AVATAR if message["role"] == "user" else ASSISTANT_AVATAR):
        st.markdown(message["content"])

show_resolution_feedback()

user_input = st.chat_input("Message IT Helpdesk...")
active_prompt = selected_quick_prompt or user_input

if active_prompt:
    if st.session_state.conversation_id is None:
        st.session_state.conversation_id = STORE.create_conversation()

    conversation_id = st.session_state.conversation_id

    user_message = STORE.append_message(
        conversation_id,
        "user",
        active_prompt,
    )
    st.session_state.messages.append(user_message)

    # Record question in Cloud Firestore under user's isolated collection
    try:
        fb_config = FirebaseConfig.load()
        if fb_config and fb_config.project_id and st.session_state.auth_user:
            user_uid = st.session_state.auth_user.get("uid") or st.session_state.auth_user.get("localId")
            cur_token = st.session_state.auth_user.get("id_token")
            fs_client = FirestoreClient(
                project_id=fb_config.project_id,
                id_token=cur_token,
            )
            try:
                fs_client.record_activity(
                    uid=user_uid,
                    question=active_prompt,
                    category=categorize_prompt(active_prompt),
                    conversation_id=conversation_id,
                    caller_uid=user_uid,
                )
            except Exception:
                # If recording failed (e.g. token expired), attempt token refresh once
                refresh_tok = st.session_state.auth_user.get("refresh_token")
                if refresh_tok:
                    try:
                        auth_c = FirebaseAuthClient(fb_config)
                        toks = auth_c.refresh_token(refresh_tok)
                        if toks.get("id_token"):
                            st.session_state.auth_user["id_token"] = toks["id_token"]
                            if toks.get("refresh_token"):
                                st.session_state.auth_user["refresh_token"] = toks["refresh_token"]
                            fs_client = FirestoreClient(
                                project_id=fb_config.project_id,
                                id_token=toks["id_token"],
                            )
                            fs_client.record_activity(
                                uid=user_uid,
                                question=active_prompt,
                                category=categorize_prompt(active_prompt),
                                conversation_id=conversation_id,
                                caller_uid=user_uid,
                            )
                    except Exception:
                        pass
    except Exception:
        # Analytics and Firestore activity recording must never interrupt the user chat
        pass

    with st.chat_message("user", avatar=USER_AVATAR):
        st.markdown(active_prompt)

    with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
        requested_at = datetime.now(UTC).isoformat()
        request_started = perf_counter()
        outcome = "agent_success"
        error_stage = None
        error_type = None
        category = None
        try:
            agent_prompt = active_prompt

            if (
                st.session_state.agent_session is None
                and len(st.session_state.messages) > 1
            ):
                agent_prompt = prompt_with_saved_context(
                    st.session_state.messages[:-1],
                    active_prompt,
                )

            answer, session = asyncio.run(
                get_agent_response(
                    agent_prompt,
                    st.session_state.agent_session,
                )
            )
            st.session_state.agent_session = session

        except Exception as error:
            outcome = "fallback"
            error_stage = "foundry_agent_run"
            error_type = type(error).__name__
            st.markdown(
                """
                <div class="diagnostic-banner">
                    <span class="diagnostic-badge">LOCAL DIAGNOSTIC MODE</span>
                    <span>Microsoft Foundry unreachable &bull; First-line resolution guidance active</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            answer, category = local_troubleshooting_response(active_prompt)

        request_id = None
        try:
            request_id = TELEMETRY.record_request(
                conversation_id=conversation_id,
                requested_at=requested_at,
                latency_ms=round((perf_counter() - request_started) * 1000),
                outcome=outcome,
                error_stage=error_stage,
                error_type=error_type,
                category=category,
            )
        except Exception:
            # Analytics must never interrupt IT support or alter its fallback behavior.
            pass
        else:
            st.session_state.pending_feedback_request_id = request_id

        st.markdown(answer)

        assistant_message = STORE.append_message(
            conversation_id,
            "assistant",
            answer,
        )
        st.session_state.messages.append(assistant_message)

    st.rerun()
