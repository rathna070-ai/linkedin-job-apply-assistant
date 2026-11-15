from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jobapply.llm import Claude, ClaudeError
from jobapply.mailer import MailError, Reply
from jobapply.schemas import EmailDraft, PostAnalysis, PostAnalysisBatch, ReplyAssessment, TailoredResume
from jobapply.services import (
    add_manual_application,
    build_signature,
    check_responses,
    draft_for_post,
    run_search,
    send_application,
    set_response_status,
    tailor_for_post,
)
from tests.test_mailer import FakeSMTP
from tests.test_scraper import FakeApify

POST_ITEMS = [
    {
        "id": "p1",
        "linkedinUrl": "https://www.linkedin.com/posts/p1",
        "content": "#hiring Delivery Manager BFSI, 14-18 yrs. Send CV to tashvita@harp-india.com",
        "author": {"name": "Tashvita Ingale", "info": "Recruiter"},
        "postedAt": {"date": "2026-11-14"},
    },
    {
        "id": "p2",
        "linkedinUrl": "https://www.linkedin.com/posts/p2",
        "content": "#hiring Pega lead, apply via link",
        "author": {"name": "Someone"},
    },
    {"id": "p3", "content": "Looking for a job, open to work"},
]


class FakeMessages:
    def __init__(self, tailored, refuse=False):
        self.calls = []
        self.tailored = tailored
        self.refuse = refuse

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["fallbacks"] == "default"
        assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
        fmt = kwargs["output_format"]
        if self.refuse:
            return SimpleNamespace(stop_reason="refusal", parsed_output=None)
        if fmt is PostAnalysisBatch:
            out = PostAnalysisBatch(
                results=[
                    PostAnalysis(
                        post_index=0,
                        is_hiring_post=True,
                        company="Harp India",
                        role="Delivery Manager - BFSI",
                        location="Bangalore",
                        experience_required="14-18 yrs",
                        salary="Not stated",
                        contact_email="tashvita@harp-india.com",
                        job_description="Own BFSI delivery.",
                        match_score=140,  # out of range on purpose
                        match_reasons="BFSI delivery.",
                        gaps="",
                    )
                ]
            )
        elif fmt is EmailDraft:
            out = EmailDraft(subject="Application: Delivery Manager - BFSI - Asha Rao", body="Hi Tashvita,\n\nBody")
        elif fmt is TailoredResume:
            out = self.tailored
        elif fmt is ReplyAssessment:
            out = ReplyAssessment(category="interview", summary="Wants a call tomorrow.")
        else:
            raise AssertionError(fmt)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=out)


def make_claude(tailored, refuse=False):
    fake = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(tailored, refuse)))
    return Claude(api_key=None, model="claude-opus-5-5", client=fake), fake.beta.messages


SETTINGS = {"full_name": "Asha Rao", "phone": "+91 90000 00000", "email": "asha@gmail.com", "linkedin": "linkedin.com/in/asha"}


@pytest.fixture
def seeded(store):
    store.save_latest_resume(pdf_name="Asha_Rao.pdf", pdf_bytes=b"%PDF-1.4 resume", text="Asha Rao. Delivery Manager. BFSI.")
    return store


def test_run_search_filters_saves_and_scores(seeded, tailored):
    claude, messages = make_claude(tailored)
    summary = run_search(
        store=seeded,
        apify_token="t",
        claude=claude,
        queries=["#hiring delivery manager"],
        hashtags="#hiring",
        max_posts=20,
        posted_limit="week",
        require_hashtag=True,
        only_with_email=True,
        scrape_client=FakeApify(POST_ITEMS),
    )
    assert summary["found"] == 3 and summary["with_hashtag"] == 2 and summary["kept"] == 1
    assert summary["new"] == 1 and summary["analyzed"] == 1
    post = seeded.get_post("p1")
    assert post["analysis"]["match_score"] == 100  # clamped
    assert post["search_ids"] == [summary["search_id"]]
    # Effort and caching are set on every call.
    call = messages.calls[0]
    assert call["output_config"] == {"effort": "medium"}
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}

    # Running again does not create duplicates.
    again = run_search(
        store=seeded,
        apify_token="t",
        claude=claude,
        queries=["#hiring delivery manager"],
        hashtags="#hiring",
        max_posts=20,
        posted_limit="week",
        require_hashtag=True,
        only_with_email=True,
        scrape_client=FakeApify(POST_ITEMS),
    )
    assert again["new"] == 0 and again["analyzed"] == 0
    assert seeded.posts.count_documents({}) == 1


def test_refusal_raises(seeded, tailored):
    claude, _ = make_claude(tailored, refuse=True)
    with pytest.raises(ClaudeError):
        claude.draft_email(post={}, resume_text="", signature="", candidate_name="", recipient="", tailored=False)


def _shortlisted_post(store, tailored):
    claude, _ = make_claude(tailored)
    run_search(
        store=store,
        apify_token="t",
        claude=claude,
        queries=["q"],
        hashtags="#hiring",
        max_posts=5,
        posted_limit="week",
        require_hashtag=True,
        only_with_email=True,
        scrape_client=FakeApify(POST_ITEMS),
    )
    store.set_shortlisted(["p1"], True)
    return claude


def test_tailor_draft_send_and_audit(seeded, tailored):
    claude = _shortlisted_post(seeded, tailored)
    post = seeded.get_post("p1")
    store = seeded

    rid = tailor_for_post(store=store, claude=claude, post=post)
    resume = store.get_resume(rid)
    assert resume["pdf_name"] == "Asha_Rao_Harp_India_Delivery_Manager_BFSI.pdf"
    assert bytes(resume["pdf"]).startswith(b"%PDF")

    draft_for_post(store=store, claude=claude, post=store.get_post("p1"), settings=SETTINGS, mode="tailored")
    post = store.get_post("p1")
    assert post["draft"]["to"] == "tashvita@harp-india.com"
    assert post["draft"]["mode"] == "tailored"

    FakeSMTP.fail = False
    FakeSMTP.sent.clear()
    app_id = send_application(
        store=store,
        post=post,
        settings=SETTINGS,
        gmail_address="asha@gmail.com",
        app_password="abcd efgh ijkl mnop",
        smtp_factory=FakeSMTP,
    )
    msg = FakeSMTP.sent[-1]
    assert msg["To"] == "tashvita@harp-india.com"
    assert [p.get_filename() for p in msg.iter_attachments()] == ["Asha_Rao_Harp_India_Delivery_Manager_BFSI.pdf"]

    app = store.get_application(app_id)
    assert app["status"] == "sent" and app["company"] == "Harp India" and app["resume_mode"] == "tailored"
    assert app["job_description"] == "Own BFSI delivery."
    assert app["message_id"] == msg["Message-ID"]
    assert store.get_post("p1")["applied"] is True
    assert [e["type"] for e in store.list_events(app_id)] == ["sent"]
    assert len(store.applications_to("TASHVITA@harp-india.com")) == 1


def test_failed_send_is_recorded(seeded, tailored):
    _shortlisted_post(seeded, tailored)
    seeded.update_draft("p1", to="hr@acme.com", subject="s", body="b", mode="latest")
    FakeSMTP.fail = True
    try:
        with pytest.raises(MailError):
            send_application(
                store=seeded,
                post=seeded.get_post("p1"),
                settings=SETTINGS,
                gmail_address="asha@gmail.com",
                app_password="x",
                smtp_factory=FakeSMTP,
            )
    finally:
        FakeSMTP.fail = False
    apps = seeded.list_applications()
    assert len(apps) == 1 and apps[0]["status"] == "failed"
    assert seeded.get_post("p1")["applied"] is False


class FakeChecker:
    def __init__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def find_replies(self, *, recipient, since, message_id):
        if recipient != "hr@acme.com":
            return []
        return [
            Reply(
                sender="hr@acme.com",
                subject="Re: Application",
                date=since + timedelta(hours=3),
                snippet="Can we talk tomorrow at 11?",
                message_id="<r1@acme.com>",
                matched_by="thread",
            )
        ]


def test_check_responses_updates_status_once(store, tailored):
    claude, _ = make_claude(tailored)
    sent_at = datetime(2026, 11, 15, 5, 0, tzinfo=timezone.utc)
    a1 = add_manual_application(store, {"company": "Acme", "role": "QA Lead", "recipient": "hr@acme.com", "sent_at": sent_at})
    add_manual_application(store, {"company": "Other", "role": "PM", "recipient": "x@other.com", "sent_at": sent_at})

    result = check_responses(store=store, gmail_address="me@gmail.com", app_password="p", claude=claude, checker_factory=FakeChecker)
    assert result == {"checked": 2, "new_replies": 1}
    app = store.get_application(a1)
    assert app["response_status"] == "interview"
    assert app["response_summary"] == "Wants a call tomorrow."
    assert len(app["replies"]) == 1

    again = check_responses(store=store, gmail_address="me@gmail.com", app_password="p", claude=claude, checker_factory=FakeChecker)
    assert again["new_replies"] == 0
    assert len(store.get_application(a1)["replies"]) == 1


def test_manual_status_update_logs_event(store):
    app_id = add_manual_application(
        store, {"company": "Acme", "role": "QA", "recipient": "a@acme.com", "sent_at": datetime.now(timezone.utc)}
    )
    set_response_status(store, app_id, "rejected", "Closed")
    app = store.get_application(app_id)
    assert app["response_status"] == "rejected" and app["notes"] == "Closed" and app["response_at"]
    assert store.list_events(app_id)[0]["type"] == "status_updated"


def test_signature():
    assert build_signature(SETTINGS) == "Best regards,\nAsha Rao\n+91 90000 00000 | asha@gmail.com\nlinkedin.com/in/asha"


def test_settings_threshold_round_trip(store):
    assert store.get_settings()["match_threshold"] == 85
    store.save_settings(match_threshold=70)
    assert store.get_settings()["match_threshold"] == 70
