"""Renders every page headlessly against an in-memory database."""

from datetime import datetime, timezone
from pathlib import Path

import mongomock
import pytest
from streamlit.testing.v1 import AppTest

import jobapply.ui as ui
from jobapply.services import add_manual_application
from jobapply.store import Store

APP = str(Path(__file__).resolve().parents[1] / "app.py")
PAGES = ["views/find_posts.py", "views/apply.py", "views/audit.py", "views/settings.py"]


@pytest.fixture
def memory_store(monkeypatch):
    store = Store(db_name="smoke", client=mongomock.MongoClient(tz_aware=True))
    monkeypatch.setattr(ui, "get_store", lambda: store)
    return store


def make_app(**secrets):
    at = AppTest.from_file(APP, default_timeout=30)
    base = {"MONGODB_URI": "mongodb://unused", "APP_PASSWORD": "", "ANTHROPIC_API_KEY": "", "APIFY_TOKEN": ""}
    for k, v in (base | secrets).items():
        at.secrets[k] = v
    return at


@pytest.mark.parametrize("page", PAGES)
def test_pages_render_empty(memory_store, page):
    at = make_app()
    at.run()
    at.switch_page(page)
    at.run()
    assert not at.exception, at.exception


def test_pages_render_with_data(memory_store):
    s = memory_store
    s.save_latest_resume(pdf_name="r.pdf", pdf_bytes=b"%PDF-1.4", text="Resume text")
    s.upsert_posts(
        [{"post_id": "p1", "url": "https://x", "text": "#hiring QA lead hr@acme.com", "emails": ["hr@acme.com"]}],
        "s1",
    )
    s.save_settings(last_search_id="s1")
    s.save_analysis(
        "p1",
        {
            "is_hiring_post": True,
            "company": "Acme",
            "role": "QA Lead",
            "location": "Bengaluru",
            "experience_required": "10 yrs",
            "salary": "Not stated",
            "contact_email": "hr@acme.com",
            "job_description": "Lead QA.",
            "match_score": 90,
            "match_reasons": "Fits.",
            "gaps": "",
        },
        "claude-opus-5-5",
    )
    s.set_shortlisted(["p1"], True)
    add_manual_application(
        s, {"company": "Harp", "role": "DM", "recipient": "t@harp.com", "sent_at": datetime.now(timezone.utc)}
    )
    for page in PAGES:
        at = make_app()
        at.run()
        at.switch_page(page)
        at.run()
        assert not at.exception, (page, at.exception)

    # The confidence slider on the Find posts page changes the saved threshold.
    at = make_app()
    at.run()
    slider = next(sl for sl in at.slider if "confidence" in sl.label.lower())
    slider.set_value(70).run()
    assert s.get_settings()["match_threshold"] == 70


def test_password_gate_blocks_pages(memory_store):
    at = make_app(APP_PASSWORD="secret")
    at.run()
    assert at.text_input[0].label == "App password"
    assert not at.slider  # nothing behind the gate rendered
    at.text_input[0].input("secret")
    at.button[0].click().run()
    assert at.session_state["authenticated"] is True
