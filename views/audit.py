import io
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from jobapply.mailer import MailError
from jobapply.services import RESPONSE_LABELS, add_manual_application, check_responses, set_response_status
from jobapply.store import RESPONSE_STATUSES, as_utc
from jobapply.ui import flash, fmt_dt, get_claude, get_store, gmail_credentials, show_flashes

store = get_store()
settings = store.get_settings()
claude = get_claude()
gmail_address, gmail_password = gmail_credentials()
tz = settings["timezone"]

st.title("Audit trail")
st.caption("Every application with its company, role and JD, when the email went out, and whether anyone replied.")
show_flashes()

apps = store.list_applications()
sent = [a for a in apps if a.get("status") == "sent"]
answered = [a for a in sent if a.get("response_status") not in (None, "no_response", "auto_reply")]
interviews = [a for a in sent if a.get("response_status") == "interview"]
failed = [a for a in apps if a.get("status") == "failed"]

m1, m2, m3, m4 = st.columns(4)
m1.metric("Emails sent", len(sent))
m2.metric("Replies", len(answered))
m3.metric("Interviews / calls", len(interviews))
m4.metric("Reply rate", f"{(len(answered) / len(sent) * 100):.0f}%" if sent else "—")

c1, c2 = st.columns([1, 3])
check = c1.button(
    "Check Gmail for replies",
    type="primary",
    icon=":material/mark_email_unread:",
    disabled=not (gmail_address and gmail_password and sent),
    help="Looks in your Gmail for replies from each recruiter (same thread, same address, or same company domain).",
)
checked_times = [as_utc(a["last_checked_at"]) for a in sent if a.get("last_checked_at")]
if checked_times:
    c2.caption(f"Last checked {fmt_dt(max(checked_times), tz)}")
if not (gmail_address and gmail_password):
    c2.caption("Add GMAIL_ADDRESS and GMAIL_APP_PASSWORD to check replies automatically. You can still set statuses by hand.")
if check:
    with st.spinner("Reading Gmail..."):
        try:
            result = check_responses(store=store, gmail_address=gmail_address, app_password=gmail_password, claude=claude)
            flash("success", f"Checked {result['checked']} applications: {result['new_replies']} new replies.")
            st.rerun()
        except MailError as exc:
            st.error(str(exc))

if not apps:
    st.info("No applications yet. Send one from the Apply page, or add one you sent elsewhere below.")
else:
    f1, f2, f3 = st.columns([2, 1, 1])
    search = f1.text_input("Search company, role or recipient", "")
    statuses = f2.multiselect(
        "Reply status", RESPONSE_STATUSES, format_func=RESPONSE_LABELS.get, placeholder="All"
    )
    mail_filter = f3.selectbox("Mail", ["All", "Sent", "Failed"])

    def keep(a: dict) -> bool:
        blob = f"{a.get('company', '')} {a.get('role', '')} {a.get('recipient', '')}".lower()
        if search and search.lower() not in blob:
            return False
        if statuses and a.get("response_status", "no_response") not in statuses:
            return False
        if mail_filter != "All" and a.get("status") != mail_filter.lower():
            return False
        return True

    shown = [a for a in apps if keep(a)]
    rows = [
        {
            "id": str(a["_id"]),
            "Sent": fmt_dt(a.get("sent_at") or a.get("attempted_at"), tz),
            "Company": a.get("company", ""),
            "Role": a.get("role", ""),
            "Location": a.get("location", ""),
            "Recipient": a.get("recipient", ""),
            "Resume": "Tailored" if a.get("resume_mode") == "tailored" else "Latest",
            "Match %": a.get("match_score"),
            "Mail": "Failed" if a.get("status") == "failed" else "Sent",
            "Reply": RESPONSE_LABELS.get(a.get("response_status", "no_response"), ""),
            "Reply on": fmt_dt(a.get("response_at"), tz),
            "Post": a.get("post_url", ""),
        }
        for a in shown
    ]
    df = pd.DataFrame(rows)
    event = st.dataframe(
        df,
        hide_index=True,
        column_config={
            "id": None,
            "Match %": st.column_config.NumberColumn("Match %", format="%d%%"),
            "Post": st.column_config.LinkColumn("Post", display_text="Open"),
        },
        on_select="rerun",
        selection_mode="single-row",
        key="apps_table",
    )

    export = pd.DataFrame(
        [
            {
                "Sent": fmt_dt(a.get("sent_at") or a.get("attempted_at"), tz),
                "Company": a.get("company", ""),
                "Role": a.get("role", ""),
                "Location": a.get("location", ""),
                "Recipient": a.get("recipient", ""),
                "Subject": a.get("subject", ""),
                "Resume mode": a.get("resume_mode", ""),
                "Resume file": a.get("resume_filename", ""),
                "Match %": a.get("match_score"),
                "Mail status": a.get("status", ""),
                "Error": a.get("error", ""),
                "Reply status": RESPONSE_LABELS.get(a.get("response_status", "no_response"), ""),
                "Reply on": fmt_dt(a.get("response_at"), tz),
                "Reply from": a.get("response_from", ""),
                "Reply summary": a.get("response_summary", ""),
                "Notes": a.get("notes", ""),
                "Post link": a.get("post_url", ""),
                "Job description": a.get("job_description", ""),
                "Email body": a.get("body", ""),
            }
            for a in shown
        ]
    )
    xlsx = io.BytesIO()
    export.to_excel(xlsx, index=False, sheet_name="Applications")
    d1, d2, _ = st.columns([1, 1, 3])
    d1.download_button("Download CSV", export.to_csv(index=False).encode("utf-8"), "applications.csv", "text/csv", on_click="ignore")
    d2.download_button(
        "Download Excel",
        xlsx.getvalue(),
        "applications.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        on_click="ignore",
    )

    st.subheader("Application details")
    selected_rows = event.selection.rows if event and hasattr(event, "selection") else []
    ids = df["id"].tolist()
    if not ids:
        st.info("No applications match the filters.")
    else:
        default_index = selected_rows[0] if selected_rows else 0
        by_id = {str(a["_id"]): a for a in shown}
        app_id = st.selectbox(
            "Application",
            ids,
            index=default_index,
            format_func=lambda i: f"{by_id[i].get('company', '')} — {by_id[i].get('role', '')} ({fmt_dt(by_id[i].get('sent_at'), tz, False)})",
            help="Or click a row in the table above.",
        )
        app = by_id[app_id]
        left, right = st.columns([1.3, 1], gap="large")
        with left:
            st.markdown(
                f"**Sent:** {fmt_dt(app.get('sent_at'), tz) or 'not sent'}  \n"
                f"**To:** {app.get('recipient', '')}  \n"
                f"**Subject:** {app.get('subject', '')}  \n"
                f"**Resume:** {app.get('resume_filename', '')} ({'tailored' if app.get('resume_mode') == 'tailored' else 'latest'})"
                + (f"  \n**Post:** [open]({app['post_url']})" if app.get("post_url") else "")
            )
            if app.get("status") == "failed":
                st.error(f"Sending failed: {app.get('error', '')}")
            if app.get("resume_id"):
                resume = store.get_resume(app["resume_id"])
                if resume:
                    st.download_button(
                        "Download the resume that was sent",
                        bytes(resume["pdf"]),
                        resume["pdf_name"],
                        "application/pdf",
                        on_click="ignore",
                        key=f"res_{app_id}",
                    )
            with st.expander("Job description"):
                st.write(app.get("job_description", "") or "—")
            with st.expander("Email that was sent"):
                st.text(app.get("body", "") or "—")
        with right:
            with st.form(f"status_{app_id}"):
                current = app.get("response_status", "no_response")
                new_status = st.selectbox(
                    "Reply status",
                    RESPONSE_STATUSES,
                    index=RESPONSE_STATUSES.index(current) if current in RESPONSE_STATUSES else 0,
                    format_func=RESPONSE_LABELS.get,
                )
                notes = st.text_area("Notes", app.get("notes", ""), height=100)
                if st.form_submit_button("Save"):
                    set_response_status(store, app["_id"], new_status, notes)
                    flash("success", "Status saved.")
                    st.rerun()
            replies = app.get("replies") or []
            if replies:
                st.markdown("**Replies found in Gmail**")
                for reply in reversed(replies):
                    title = f"{fmt_dt(reply.get('date'), tz)} · {reply.get('from', '')} · {RESPONSE_LABELS.get(reply.get('category'), reply.get('category', ''))}"
                    with st.expander(title):
                        if reply.get("summary"):
                            st.markdown(f"_{reply['summary']}_")
                        st.caption(f"Subject: {reply.get('subject', '')} · matched by {reply.get('matched_by', '')}")
                        st.text(reply.get("snippet", ""))
            elif app.get("status") == "sent":
                st.caption("No replies found yet.")

        events = store.list_events(app["_id"])
        if events:
            st.markdown("**History**")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "When": fmt_dt(e.get("ts"), tz),
                            "Event": e.get("type", "").replace("_", " "),
                            "Details": ", ".join(f"{k}: {v}" for k, v in (e.get("details") or {}).items()),
                        }
                        for e in events
                    ]
                ),
                hide_index=True,
            )

with st.expander("Add an application you sent outside this app"):
    with st.form("manual", clear_on_submit=True):
        c1, c2 = st.columns(2)
        company = c1.text_input("Company *")
        role = c2.text_input("Role *")
        recipient = c1.text_input("Recipient email")
        location = c2.text_input("Location")
        sent_date = c1.date_input("Sent on *", value=datetime.now(ZoneInfo(tz)).date())
        sent_time = c2.time_input("Time", value=time(10, 0))
        post_url = st.text_input("Post link")
        subject = st.text_input("Subject")
        jd = st.text_area("Job description", height=120)
        mode = st.radio("Resume", ["latest", "tailored"], format_func={"latest": "Latest", "tailored": "Tailored"}.get, horizontal=True)
        notes = st.text_area("Notes", height=70)
        if st.form_submit_button("Add to audit trail"):
            if not company or not role:
                st.error("Company and role are required.")
            else:
                sent_at = datetime.combine(sent_date, sent_time, tzinfo=ZoneInfo(tz))
                add_manual_application(
                    store,
                    {
                        "company": company,
                        "role": role,
                        "recipient": recipient,
                        "location": location,
                        "post_url": post_url,
                        "subject": subject,
                        "job_description": jd,
                        "resume_mode": mode,
                        "notes": notes,
                        "sent_at": sent_at,
                    },
                )
                flash("success", f"Added {company} — {role}.")
                st.rerun()

with st.expander("Activity log (searches, drafts, sends, reply checks)"):
    recent = store.list_events(limit=100)
    if recent:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "When": fmt_dt(e.get("ts"), tz),
                        "Event": e.get("type", "").replace("_", " "),
                        "Details": ", ".join(f"{k}: {v}" for k, v in (e.get("details") or {}).items())[:300],
                    }
                    for e in recent
                ]
            ),
            hide_index=True,
        )
    else:
        st.caption("Nothing yet.")
