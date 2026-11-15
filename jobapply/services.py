"""Workflows that tie scraping, Claude, email and storage together."""

from __future__ import annotations

import smtplib
from datetime import datetime
from typing import Any, Callable

from bson import ObjectId

from .llm import Claude, ClaudeError
from .mailer import MailError, ReplyChecker, build_message, is_valid_address, send_message
from .resume import render_docx, render_pdf, safe_filename
from .scraper import dedupe_posts, has_any_hashtag, normalize_post, parse_hashtags, scrape_posts
from .store import Store, as_utc, utcnow

RESPONSE_LABELS = {
    "no_response": "No response yet",
    "replied": "Replied",
    "interview": "Interview / call",
    "request_info": "Asked for details",
    "rejected": "Rejected",
    "auto_reply": "Auto-reply",
    "other": "Other",
}
# A reply in one of these categories always updates the status; others only replace weaker statuses.
_STRONG = {"interview", "request_info", "rejected"}
_WEAK = {"no_response", "auto_reply"}
_AUTO_REPLY_HINTS = ("automatic reply", "auto-reply", "autoreply", "out of office", "auto reply")


def build_signature(settings: dict[str, Any]) -> str:
    lines = ["Best regards,"]
    if settings.get("full_name"):
        lines.append(settings["full_name"])
    contact = " | ".join(x for x in (settings.get("phone"), settings.get("email")) if x)
    if contact:
        lines.append(contact)
    if settings.get("linkedin"):
        lines.append(settings["linkedin"])
    return "\n".join(lines)


def default_recipient(post: dict[str, Any]) -> str:
    draft = post.get("draft") or {}
    analysis = post.get("analysis") or {}
    if draft.get("to"):
        return draft["to"]
    if analysis.get("contact_email"):
        return analysis["contact_email"]
    emails = post.get("emails") or []
    return emails[0] if emails else ""


# ----- search -----
def run_search(
    *,
    store: Store,
    apify_token: str,
    claude: Claude | None,
    queries: list[str],
    hashtags: str,
    max_posts: int,
    posted_limit: str,
    require_hashtag: bool,
    only_with_email: bool,
    progress: Callable[[str], None] | None = None,
    scrape_client: Any = None,
) -> dict[str, Any]:
    say = progress or (lambda _msg: None)
    search_id = utcnow().strftime("%Y%m%d-%H%M%S")
    say(f"Running {len(queries)} LinkedIn searches on Apify...")
    raw = scrape_posts(apify_token, queries, max_posts=max_posts, posted_limit=posted_limit, client=scrape_client)
    posts = dedupe_posts(p for p in (normalize_post(item) for item in raw) if p)
    found = len(posts)
    tags = parse_hashtags(hashtags)
    if require_hashtag and tags:
        posts = [p for p in posts if has_any_hashtag(p["text"], tags)]
    with_tag = len(posts)
    if only_with_email:
        posts = [p for p in posts if p["emails"]]
    say(f"Found {found} posts, {with_tag} with the hashtag, {len(posts)} kept. Saving...")
    new = store.upsert_posts(posts, search_id)
    store.save_settings(last_search_id=search_id)

    analyzed = 0
    note = ""
    if claude is None:
        note = "Claude is not configured, so posts were not scored."
    elif store.get_latest_resume() is None:
        note = "Upload your resume in Settings to score posts."
    elif posts:
        say(f"Scoring {len(posts)} posts against your resume with Claude...")
        analyzed = analyze_pending(store, claude, [p["post_id"] for p in posts])
    summary = {
        "search_id": search_id,
        "queries": queries,
        "found": found,
        "with_hashtag": with_tag,
        "kept": len(posts),
        "new": new,
        "analyzed": analyzed,
        "note": note,
        "estimated_cost_usd": round(found * 0.002, 3),
    }
    store.log_event("search", details=summary)
    return summary


def analyze_pending(
    store: Store,
    claude: Claude,
    post_ids: list[str] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    resume = store.get_latest_resume()
    if resume is None:
        raise ValueError("Upload your resume in Settings first.")
    query: dict[str, Any] = {"analysis": {"$exists": False}}
    if post_ids is not None:
        query["_id"] = {"$in": post_ids}
    pending = store.list_posts(query)
    if not pending:
        return 0
    results = claude.analyze_posts(pending, resume["text"], progress=progress)
    for post_id, analysis in results.items():
        store.save_analysis(post_id, analysis, claude.model)
    store.log_event("posts_scored", details={"count": len(results), "model": claude.model})
    return len(results)


# ----- drafting and tailoring -----
def draft_for_post(*, store: Store, claude: Claude, post: dict[str, Any], settings: dict[str, Any], mode: str) -> None:
    resume = store.get_latest_resume()
    if resume is None:
        raise ValueError("Upload your resume in Settings first.")
    recipient = default_recipient(post)
    draft = claude.draft_email(
        post=post,
        resume_text=resume["text"],
        signature=build_signature(settings),
        candidate_name=settings.get("full_name") or "",
        recipient=recipient,
        tailored=(mode == "tailored"),
    )
    store.update_draft(post["_id"], to=recipient, subject=draft.subject, body=draft.body, mode=mode, reviewed=False)
    store.log_event("draft_created", post_id=post["_id"], details={"subject": draft.subject, "mode": mode})


def tailor_for_post(*, store: Store, claude: Claude, post: dict[str, Any]) -> ObjectId:
    resume = store.get_latest_resume()
    if resume is None:
        raise ValueError("Upload your resume in Settings first.")
    tailored = claude.tailor_resume(resume_text=resume["text"], post=post)
    analysis = post.get("analysis") or {}
    base = safe_filename(tailored.full_name, analysis.get("company", ""), analysis.get("role", "")[:40])
    resume_id = store.save_tailored_resume(
        post_id=post["_id"],
        base_name=base,
        docx_bytes=render_docx(tailored),
        pdf_bytes=render_pdf(tailored),
        data=tailored.model_dump(),
        model=claude.model,
    )
    store.update_draft(post["_id"], tailored_resume_id=resume_id, reviewed=False)
    store.log_event(
        "resume_tailored",
        post_id=post["_id"],
        details={"resume_id": str(resume_id), "keywords": tailored.jd_keywords_used, "gaps": tailored.gaps},
    )
    return resume_id


def attachment_for(store: Store, post: dict[str, Any]) -> dict[str, Any] | None:
    draft = post.get("draft") or {}
    if draft.get("mode") == "tailored":
        rid = draft.get("tailored_resume_id")
        return store.get_resume(rid) if rid else None
    return store.get_latest_resume()


# ----- sending -----
def send_application(
    *,
    store: Store,
    post: dict[str, Any],
    settings: dict[str, Any],
    gmail_address: str,
    app_password: str,
    smtp_factory: Callable[..., Any] = smtplib.SMTP_SSL,
) -> ObjectId:
    draft = post.get("draft") or {}
    analysis = post.get("analysis") or {}
    to = (draft.get("to") or "").strip()
    subject = (draft.get("subject") or "").strip()
    body = draft.get("body") or ""
    mode = draft.get("mode") or "latest"
    if not is_valid_address(to):
        raise MailError(f"'{to}' is not a valid email address.")
    if not subject or not body.strip():
        raise MailError("The email needs a subject and a body.")
    resume = attachment_for(store, post)
    if resume is None:
        raise MailError("No resume to attach. Upload one in Settings, or tailor one for this post.")

    msg = build_message(
        sender_email=gmail_address,
        sender_name=settings.get("full_name") or "",
        to=to,
        subject=subject,
        body=body,
        attachments=[(resume["pdf_name"], bytes(resume["pdf"]), "application/pdf")],
    )
    record = {
        "source": "app",
        "post_id": post["_id"],
        "post_url": post.get("url", ""),
        "company": analysis.get("company", ""),
        "role": analysis.get("role", ""),
        "location": analysis.get("location", ""),
        "job_description": analysis.get("job_description") or post.get("text", ""),
        "match_score": analysis.get("match_score"),
        "recipient": to,
        "subject": subject,
        "body": body,
        "resume_mode": mode,
        "resume_id": resume["_id"],
        "resume_filename": resume["pdf_name"],
    }
    try:
        message_id = send_message(msg, gmail_address=gmail_address, app_password=app_password, smtp_factory=smtp_factory)
    except MailError as exc:
        app_id = store.create_application(record | {"status": "failed", "error": str(exc), "attempted_at": utcnow()})
        store.log_event("send_failed", application_id=app_id, post_id=post["_id"], details={"error": str(exc)})
        raise
    app_id = store.create_application(record | {"status": "sent", "sent_at": utcnow(), "message_id": message_id})
    store.log_event(
        "sent",
        application_id=app_id,
        post_id=post["_id"],
        details={"recipient": to, "subject": subject, "resume": resume["pdf_name"], "mode": mode},
    )
    store.mark_applied(post["_id"], app_id)
    return app_id


def add_manual_application(store: Store, fields: dict[str, Any]) -> ObjectId:
    record = {
        "source": "manual",
        "status": "sent",
        "company": fields.get("company", ""),
        "role": fields.get("role", ""),
        "location": fields.get("location", ""),
        "job_description": fields.get("job_description", ""),
        "post_url": fields.get("post_url", ""),
        "recipient": fields.get("recipient", ""),
        "subject": fields.get("subject", ""),
        "body": fields.get("body", ""),
        "resume_mode": fields.get("resume_mode", "latest"),
        "resume_filename": fields.get("resume_filename", ""),
        "match_score": fields.get("match_score"),
        "sent_at": fields["sent_at"],
        "message_id": fields.get("message_id"),
        "notes": fields.get("notes", ""),
    }
    app_id = store.create_application(record)
    store.log_event("manual_entry", application_id=app_id, details={"company": record["company"], "role": record["role"]})
    return app_id


# ----- responses -----
def _guess_category(subject: str, text: str) -> str:
    blob = f"{subject}\n{text}".lower()
    if any(h in blob for h in _AUTO_REPLY_HINTS):
        return "auto_reply"
    return "replied"


def check_responses(
    *,
    store: Store,
    gmail_address: str,
    app_password: str,
    claude: Claude | None = None,
    checker_factory: Callable[..., ReplyChecker] = ReplyChecker,
) -> dict[str, int]:
    apps = store.list_applications({"status": "sent"})
    new_replies = 0
    with checker_factory(gmail_address, app_password) as checker:
        for app in apps:
            since = as_utc(app.get("sent_at") or app.get("created_at")) or utcnow()
            replies = checker.find_replies(recipient=app.get("recipient", ""), since=since, message_id=app.get("message_id"))
            known = set(app.get("reply_ids") or [])
            fresh = [r for r in replies if r.message_id not in known]
            update: dict[str, Any] = {"last_checked_at": utcnow()}
            if fresh:
                status = app.get("response_status") or "no_response"
                stored = list(app.get("replies") or [])
                for reply in fresh:
                    category, summary = _guess_category(reply.subject, reply.snippet), ""
                    if claude is not None:
                        try:
                            assessment = claude.classify_reply(
                                application=app, reply_subject=reply.subject, reply_text=reply.snippet
                            )
                            category, summary = assessment.category, assessment.summary
                        except ClaudeError:
                            pass
                    stored.append(
                        {
                            "from": reply.sender,
                            "subject": reply.subject,
                            "date": reply.date,
                            "snippet": reply.snippet[:1500],
                            "message_id": reply.message_id,
                            "matched_by": reply.matched_by,
                            "category": category,
                            "summary": summary,
                        }
                    )
                    if category == "other":
                        continue
                    if category in _STRONG or status in _WEAK or (status == "replied" and category != "auto_reply"):
                        status = category
                    update.update(
                        {
                            "response_at": reply.date or utcnow(),
                            "response_from": reply.sender,
                            "response_subject": reply.subject,
                            "response_snippet": reply.snippet[:1500],
                            "response_summary": summary,
                        }
                    )
                    new_replies += 1
                    store.log_event(
                        "response_found",
                        application_id=app["_id"],
                        details={"from": reply.sender, "subject": reply.subject, "category": category},
                    )
                update["response_status"] = status
                update["replies"] = stored
                update["reply_ids"] = sorted(known | {r.message_id for r in fresh})
            store.update_application(app["_id"], **update)
    store.log_event("responses_checked", details={"checked": len(apps), "new_replies": new_replies})
    return {"checked": len(apps), "new_replies": new_replies}


def set_response_status(store: Store, app_id: Any, status: str, notes: str) -> None:
    app = store.get_application(app_id)
    if app is None:
        return
    fields: dict[str, Any] = {"response_status": status, "notes": notes}
    if status != "no_response" and not app.get("response_at"):
        fields["response_at"] = utcnow()
    store.update_application(app_id, **fields)
    store.log_event(
        "status_updated",
        application_id=app_id,
        details={"from": app.get("response_status"), "to": status, "notes": notes},
    )


def to_local(dt: datetime | None, tz_name: str) -> datetime | None:
    if dt is None:
        return None
    from zoneinfo import ZoneInfo

    try:
        return as_utc(dt).astimezone(ZoneInfo(tz_name))
    except Exception:
        return as_utc(dt)
