"""Streamlit helpers shared by the pages."""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Any

import streamlit as st

from .config import DEFAULT_DB_NAME, claude_model, get_secret
from .llm import Claude
from .services import to_local
from .store import Store


@st.cache_resource(show_spinner=False)
def _store(uri: str, db_name: str) -> Store:
    return Store(uri, db_name)


def get_store() -> Store:
    uri = get_secret("MONGODB_URI")
    if not uri:
        st.error("MONGODB_URI is not set. Add it to `.streamlit/secrets.toml` or the app's Secrets on Streamlit Cloud.")
        st.stop()
    try:
        return _store(uri, get_secret("MONGODB_DB", DEFAULT_DB_NAME) or DEFAULT_DB_NAME)
    except Exception as exc:  # connection problems surface here
        st.error(f"Could not connect to MongoDB: {exc}")
        st.stop()


@st.cache_resource(show_spinner=False)
def _claude(api_key: str, model: str) -> Claude:
    return Claude(api_key, model)


def get_claude() -> Claude | None:
    key = get_secret("ANTHROPIC_API_KEY")
    return _claude(key, claude_model()) if key else None


def gmail_credentials() -> tuple[str | None, str | None]:
    return get_secret("GMAIL_ADDRESS"), get_secret("GMAIL_APP_PASSWORD")


def require_login() -> None:
    password = get_secret("APP_PASSWORD")
    if not password:
        st.sidebar.warning(
            "APP_PASSWORD is not set. Anyone who can open this app could send email as you. "
            "Set it before deploying."
        )
        return
    if st.session_state.get("authenticated"):
        with st.sidebar:
            if st.button("Sign out", icon=":material/logout:"):
                st.session_state["authenticated"] = False
                st.rerun()
        return
    st.title("Job Apply Assistant")
    with st.form("login"):
        entered = st.text_input("App password", type="password")
        if st.form_submit_button("Sign in", type="primary"):
            if hmac.compare_digest(entered.encode(), password.encode()):
                st.session_state["authenticated"] = True
                st.rerun()
            st.error("Wrong password.")
    st.stop()


def fmt_dt(dt: datetime | None, tz_name: str, with_time: bool = True) -> str:
    local = to_local(dt, tz_name)
    if local is None:
        return ""
    return local.strftime("%d %b %Y, %I:%M %p" if with_time else "%d %b %Y")


def flash(kind: str, message: str) -> None:
    """Queues a message to show after the next rerun."""
    st.session_state.setdefault("_flash", []).append((kind, message))


def show_flashes() -> None:
    for kind, message in st.session_state.pop("_flash", []):
        getattr(st, kind, st.info)(message)


def score_badge(score: Any, threshold: int) -> str:
    if score is None:
        return "Not scored"
    return f"{score}% {'✅' if int(score) >= threshold else '⚠️'}"
