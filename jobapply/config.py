"""Reads secrets from Streamlit secrets first, then environment variables."""

from __future__ import annotations

import os

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_DB_NAME = "job_apply_assistant"

SECRET_NAMES = [
    "APP_PASSWORD",
    "MONGODB_URI",
    "MONGODB_DB",
    "ANTHROPIC_API_KEY",
    "CLAUDE_MODEL",
    "APIFY_TOKEN",
    "GMAIL_ADDRESS",
    "GMAIL_APP_PASSWORD",
]


def get_secret(name: str, default: str | None = None) -> str | None:
    value = None
    try:
        import streamlit as st

        if name in st.secrets:
            value = st.secrets[name]
    except Exception:
        # No secrets.toml, or not running under Streamlit.
        value = None
    if value is None or value == "":
        value = os.environ.get(name)
    if value is None or value == "":
        return default
    return str(value)


def claude_model() -> str:
    return get_secret("CLAUDE_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
