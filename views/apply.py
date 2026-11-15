from concurrent.futures import ThreadPoolExecutor

import streamlit as st

from jobapply.llm import ClaudeError
from jobapply.mailer import MailError, is_valid_address
from jobapply.services import (
    attachment_for,
    build_signature,
    default_recipient,
    draft_for_post,
    send_application,
    tailor_for_post,
)
from jobapply.ui import flash, fmt_dt, get_claude, get_store, gmail_credentials, show_flashes

store = get_store()
settings = store.get_settings()
claude = get_claude()
gmail_address, gmail_password = gmail_credentials()
tz = settings["timezone"]

MODES = {"latest": "Apply with my latest resume", "tailored": "Tailor the resume to this JD"}
FIELDS = ("to", "subject", "body", "mode", "reviewed")


def key(field: str, pid: str) -> str:
    return f"{field}_{pid}"


def load_state(pid: str, force: bool = False) -> None:
    """Copies the saved draft into the widgets' session state."""
    post = store.get_post(pid)
    if post is None:
        return
    a = post.get("analysis") or {}
    d = post.get("draft") or {}
    defaults = {
        "to": default_recipient(post),
        "subject": d.get("subject") or f"Application: {a.get('role', '')} - {settings.get('full_name', '')}".strip(" -"),
        "body": d.get("body") or f"Hi Hiring Team,\n\n\n\nThank you.\n\n{build_signature(settings)}",
        "mode": d.get("mode") or settings.get("default_mode", "latest"),
        "reviewed": bool(d.get("reviewed")),
    }
    for field, value in defaults.items():
        if force or key(field, pid) not in st.session_state:
            st.session_state[key(field, pid)] = value


def save_widgets(pid: str) -> None:
    values = {f: st.session_state[key(f, pid)] for f in FIELDS if key(f, pid) in st.session_state}
    store.update_draft(pid, **values)


# ----- callbacks (run before the page renders, so they may update widget state) -----
def on_change(pid: str, field: str) -> None:
    store.update_draft(pid, **{field: st.session_state[key(field, pid)]})
    if field != "reviewed":
        st.session_state[key("reviewed", pid)] = False
        store.update_draft(pid, reviewed=False)


def do_draft(pid: str) -> None:
    save_widgets(pid)
    mode = st.session_state.get(key("mode", pid), "latest")
    with st.spinner("Claude is drafting the email..."):
        try:
            draft_for_post(store=store, claude=claude, post=store.get_post(pid), settings=settings, mode=mode)
            load_state(pid, force=True)
            flash("success", "Draft ready. Review it before sending.")
        except (ClaudeError, ValueError) as exc:
            flash("error", f"Drafting failed: {exc}")


def do_tailor(pid: str) -> None:
    save_widgets(pid)
    with st.spinner("Claude is tailoring your resume to this JD (this can take a minute)..."):
        try:
            tailor_for_post(store=store, claude=claude, post=store.get_post(pid))
            st.session_state[key("reviewed", pid)] = False
            flash("success", "Tailored resume ready. Check it before sending.")
        except (ClaudeError, ValueError) as exc:
            flash("error", f"Tailoring failed: {exc}")


def prepare_one(pid: str) -> str | None:
    """Tailors (if needed) and drafts one post. Runs in a worker thread, so it must not touch session state."""
    post = store.get_post(pid)
    d = post.get("draft") or {}
    mode = d.get("mode") or settings.get("default_mode", "latest")
    try:
        if mode == "tailored" and not d.get("tailored_resume_id"):
            tailor_for_post(store=store, claude=claude, post=post)
            post = store.get_post(pid)
        draft_for_post(store=store, claude=claude, post=post, settings=settings, mode=mode)
        return None
    except (ClaudeError, ValueError) as exc:
        a = post.get("analysis") or {}
        return f"{a.get('company', pid)}: {exc}"


def do_prepare_all(pids: list[str]) -> None:
    for pid in pids:
        save_widgets(pid)
    with st.spinner(f"Claude is preparing {len(pids)} drafts..."):
        with ThreadPoolExecutor(max_workers=4) as pool:
            errors = [e for e in pool.map(prepare_one, pids) if e]
    for pid in pids:
        load_state(pid, force=True)
    done = len(pids) - len(errors)
    flash("success" if not errors else "warning", f"Prepared {done} of {len(pids)} drafts." + (" " + " | ".join(errors) if errors else ""))


def do_send(pid: str) -> None:
    save_widgets(pid)
    post = store.get_post(pid)
    try:
        send_application(
            store=store, post=post, settings=settings, gmail_address=gmail_address, app_password=gmail_password
        )
        flash("success", f"Sent to {post['draft']['to']}. It is logged in the audit trail.")
    except MailError as exc:
        flash("error", f"Not sent: {exc}")


def do_send_all(pids: list[str]) -> None:
    sent, failed = 0, []
    with st.spinner(f"Sending {len(pids)} emails..."):
        for pid in pids:
            save_widgets(pid)
            post = store.get_post(pid)
            try:
                send_application(
                    store=store, post=post, settings=settings, gmail_address=gmail_address, app_password=gmail_password
                )
                sent += 1
            except MailError as exc:
                failed.append(f"{(post.get('analysis') or {}).get('company', pid)}: {exc}")
    flash("success" if not failed else "warning", f"Sent {sent} of {len(pids)}." + (" " + " | ".join(failed) if failed else ""))


def do_unshortlist(pid: str) -> None:
    store.set_shortlisted([pid], False)


# ----- page -----
st.title("Apply")
st.caption(
    "For each shortlisted post, choose your latest resume or a resume tailored to the JD, draft the email with "
    "Claude, review it, and send it from your Gmail. Every send is recorded in the audit trail."
)
show_flashes()

latest = store.get_latest_resume()
if latest is None:
    st.warning("Upload your latest resume in **Settings** before applying.", icon=":material/description:")
if not (gmail_address and gmail_password):
    st.warning("Gmail is not set up, so sending is off. Add GMAIL_ADDRESS and GMAIL_APP_PASSWORD (see Settings).")
if claude is None:
    st.info("ANTHROPIC_API_KEY is not set, so Claude drafting and tailoring are off. You can still write emails yourself.")

posts = store.list_posts({"shortlisted": True, "applied": {"$ne": True}})
if not posts:
    st.info("No shortlisted posts waiting. Shortlist posts on the Find posts page.")
    st.page_link("views/find_posts.py", label="Go to Find posts", icon=":material/search:")
    st.stop()

for post in posts:
    load_state(post["_id"])

def bulk_sendable(p: dict) -> bool:
    d = p.get("draft") or {}
    to = d.get("to", "")
    return (
        bool(d.get("reviewed"))
        and is_valid_address(to)
        and bool((d.get("subject") or "").strip() and (d.get("body") or "").strip())
        and attachment_for(store, p) is not None
        and not store.applications_to(to)  # repeat recipients must be sent one by one
    )


ready = [p["_id"] for p in posts if bulk_sendable(p)]
no_draft = [p["_id"] for p in posts if not (p.get("draft") or {}).get("body")]

b1, b2, _ = st.columns([1, 1, 2])
b1.button(
    f"Draft all with Claude ({len(no_draft)})",
    icon=":material/edit_note:",
    disabled=claude is None or latest is None or not no_draft,
    on_click=do_prepare_all,
    args=(no_draft,),
    help="Drafts an email for every post without one. Posts set to 'tailor' also get a tailored resume.",
)
b2.button(
    f"Send all reviewed ({len(ready)})",
    type="primary",
    icon=":material/send:",
    disabled=not ready or not (gmail_address and gmail_password),
    on_click=do_send_all,
    args=(ready,),
    help="Sends only the drafts you have ticked as reviewed. Emails to an address you already wrote to must be sent one by one.",
)

threshold = int(settings["match_threshold"])
for post in posts:
    pid = post["_id"]
    a = post.get("analysis") or {}
    d = post.get("draft") or {}
    score = a.get("match_score")
    bits = [
        f"{score}% match" if score is not None else "not scored",
        "reviewed" if d.get("reviewed") else ("draft ready" if d.get("body") else "no draft yet"),
    ]
    label = f"**{a.get('company') or post.get('author_name', '')}** — {a.get('role') or 'role not scored'}  ·  {'  ·  '.join(bits)}"
    with st.expander(label, expanded=len(posts) == 1):
        left, right = st.columns([1, 1.25], gap="large")
        with left:
            st.markdown(
                f"**Location:** {a.get('location', '')}  \n**Experience:** {a.get('experience_required', '')}  \n"
                f"**Posted by:** {post.get('author_name', '')} · [open post]({post.get('url', '')})"
            )
            if score is not None and score < threshold:
                st.warning(f"Match score {score}% is below your minimum of {threshold}%.")
            with st.container(height=260):
                st.write(a.get("job_description") or post.get("text", ""))
            if a.get("gaps"):
                st.caption(f"Gaps: {a['gaps']}")

            mode = st.radio(
                "Resume",
                list(MODES),
                format_func=MODES.get,
                key=key("mode", pid),
                on_change=on_change,
                args=(pid, "mode"),
            )
            if mode == "tailored":
                rid = d.get("tailored_resume_id")
                tailored = store.get_resume(rid) if rid else None
                if tailored:
                    data = tailored.get("data") or {}
                    st.success(f"Tailored resume: {tailored['pdf_name']} ({fmt_dt(tailored.get('created_at'), tz)})")
                    if data.get("jd_keywords_used"):
                        st.caption("JD keywords used: " + ", ".join(data["jd_keywords_used"]))
                    if data.get("gaps"):
                        st.caption("Not in your resume (left out): " + "; ".join(data["gaps"]))
                    c1, c2, c3 = st.columns(3)
                    c1.download_button("PDF", bytes(tailored["pdf"]), tailored["pdf_name"], "application/pdf", key=f"tpdf_{pid}", on_click="ignore")
                    c2.download_button(
                        "DOCX",
                        bytes(tailored["docx"]),
                        tailored["docx_name"],
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        key=f"tdocx_{pid}",
                        on_click="ignore",
                    )
                    c3.button("Re-tailor", key=f"retailor_{pid}", on_click=do_tailor, args=(pid,), disabled=claude is None)
                else:
                    st.button(
                        "Tailor resume with Claude",
                        key=f"tailor_{pid}",
                        icon=":material/auto_fix_high:",
                        on_click=do_tailor,
                        args=(pid,),
                        disabled=claude is None or latest is None,
                    )
            elif latest is not None:
                st.caption(f"Attaching {latest['pdf_name']} (uploaded {fmt_dt(latest.get('uploaded_at'), tz, False)})")
            st.button("Remove from shortlist", key=f"unshort_{pid}", type="tertiary", on_click=do_unshortlist, args=(pid,))

        with right:
            to = st.text_input("To", key=key("to", pid), on_change=on_change, args=(pid, "to"))
            previous = store.applications_to(to)
            if previous:
                last = previous[0]
                st.warning(
                    f"You already emailed {to} on {fmt_dt(last.get('sent_at'), tz, False)} "
                    f"({last.get('company', '')}, {last.get('role', '')})."
                )
            st.text_input("Subject", key=key("subject", pid), on_change=on_change, args=(pid, "subject"))
            st.text_area("Email", key=key("body", pid), height=340, on_change=on_change, args=(pid, "body"))
            st.button(
                "Redraft with Claude" if d.get("body") else "Draft with Claude",
                key=f"draft_{pid}",
                icon=":material/edit_note:",
                on_click=do_draft,
                args=(pid,),
                disabled=claude is None or latest is None,
            )
            attachment = attachment_for(store, post)
            st.caption(f"Attachment: {attachment['pdf_name']}" if attachment else "Attachment: none yet")
            reviewed = st.checkbox(
                "I have reviewed this email and the attachment",
                key=key("reviewed", pid),
                on_change=on_change,
                args=(pid, "reviewed"),
            )
            override = True
            if previous:
                override = st.checkbox("Send anyway", key=f"dup_{pid}")
            problems = []
            if not is_valid_address(to):
                problems.append("a valid recipient")
            if attachment is None:
                problems.append("a resume to attach")
            if not (gmail_address and gmail_password):
                problems.append("Gmail settings")
            if problems:
                st.caption("Needs " + ", ".join(problems) + " before sending.")
            st.button(
                "Send email",
                key=f"send_{pid}",
                type="primary",
                icon=":material/send:",
                on_click=do_send,
                args=(pid,),
                disabled=bool(problems) or not reviewed or not override,
            )
