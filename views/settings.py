import streamlit as st

from jobapply.config import SECRET_NAMES, claude_model, get_secret
from jobapply.llm import ClaudeError
from jobapply.mailer import MailError, check_login
from jobapply.resume import extract_docx_text, extract_pdf_text
from jobapply.services import build_signature
from jobapply.ui import flash, fmt_dt, get_claude, get_store, gmail_credentials, show_flashes

store = get_store()
settings = store.get_settings()
tz = settings["timezone"]

st.title("Settings")
show_flashes()

resume_tab, profile_tab, conn_tab = st.tabs(["Resume", "Profile and defaults", "Connections"])

with resume_tab:
    latest = store.get_latest_resume()
    if latest:
        st.success(f"Current resume: **{latest['pdf_name']}** (uploaded {fmt_dt(latest.get('uploaded_at'), tz)})")
        c1, c2, _ = st.columns([1, 1, 3])
        c1.download_button("Download PDF", bytes(latest["pdf"]), latest["pdf_name"], "application/pdf", on_click="ignore")
        if latest.get("docx"):
            c2.download_button(
                "Download DOCX",
                bytes(latest["docx"]),
                latest["docx_name"],
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                on_click="ignore",
            )
    else:
        st.info("No resume yet. Upload your latest resume to start.")

    with st.form("upload_resume", clear_on_submit=True):
        st.markdown("**Upload a new latest resume**")
        pdf = st.file_uploader("Resume PDF (this file is attached to emails)", type=["pdf"])
        docx = st.file_uploader("Resume DOCX (optional; Claude reads its text more reliably)", type=["docx"])
        if st.form_submit_button("Save resume", type="primary"):
            if pdf is None:
                st.error("A PDF is required.")
            else:
                pdf_bytes = pdf.getvalue()
                docx_bytes = docx.getvalue() if docx else None
                try:
                    text = extract_docx_text(docx_bytes) if docx_bytes else extract_pdf_text(pdf_bytes)
                except Exception as exc:
                    st.error(f"Could not read the file: {exc}")
                    text = ""
                if text:
                    store.save_latest_resume(
                        pdf_name=pdf.name,
                        pdf_bytes=pdf_bytes,
                        text=text,
                        docx_name=docx.name if docx else None,
                        docx_bytes=docx_bytes,
                    )
                    store.log_event("resume_uploaded", details={"pdf": pdf.name, "docx": docx.name if docx else None})
                    flash("success", "Resume saved. Check the text below.")
                    st.rerun()
                elif pdf is not None:
                    st.error("No text could be read from the file. Upload the DOCX version too.")

    if latest:
        with st.form("resume_text"):
            st.markdown("**Resume text Claude reads** (edit if the extraction missed something)")
            text = st.text_area("Resume text", latest.get("text", ""), height=420, label_visibility="collapsed")
            if st.form_submit_button("Save text"):
                store.update_latest_resume_text(text)
                flash("success", "Resume text saved.")
                st.rerun()

with profile_tab:
    gmail_address, _ = gmail_credentials()
    with st.form("profile"):
        st.markdown("**Your details** (used in the email signature)")
        c1, c2 = st.columns(2)
        full_name = c1.text_input("Full name", settings.get("full_name", ""))
        email = c2.text_input("Email", settings.get("email") or gmail_address or "")
        phone = c1.text_input("Phone", settings.get("phone", ""))
        linkedin = c2.text_input("LinkedIn URL", settings.get("linkedin", ""))
        timezone_name = c1.text_input("Time zone", tz, help="IANA name, for example Asia/Kolkata")
        st.markdown("**Defaults**")
        default_mode = st.radio(
            "Default resume choice",
            ["latest", "tailored"],
            index=0 if settings.get("default_mode", "latest") == "latest" else 1,
            format_func={"latest": "Apply with latest resume", "tailored": "Tailor to each JD"}.get,
            horizontal=True,
        )
        threshold = st.slider(
            "Minimum match score (confidence) for a post to count as eligible",
            0,
            100,
            int(settings["match_threshold"]),
            step=5,
        )
        if st.form_submit_button("Save", type="primary"):
            from zoneinfo import ZoneInfo

            try:
                ZoneInfo(timezone_name)
            except Exception:
                st.error(f"Unknown time zone: {timezone_name}")
            else:
                store.save_settings(
                    full_name=full_name.strip(),
                    email=email.strip(),
                    phone=phone.strip(),
                    linkedin=linkedin.strip(),
                    timezone=timezone_name.strip(),
                    default_mode=default_mode,
                    match_threshold=threshold,
                )
                flash("success", "Saved.")
                st.rerun()
    st.markdown("**Signature preview**")
    st.code(build_signature(settings), language=None)

with conn_tab:
    st.markdown(
        "Secrets live in `.streamlit/secrets.toml` locally, or in **App settings > Secrets** on Streamlit Cloud. "
        "Values are never shown here."
    )
    st.dataframe(
        [{"Secret": name, "Set": "✅" if get_secret(name) else "—"} for name in SECRET_NAMES],
        hide_index=True,
        width="content",
    )
    st.caption(f"Claude model: `{claude_model()}`")

    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Test MongoDB"):
        try:
            store.ping()
            st.success("MongoDB is reachable.")
        except Exception as exc:
            st.error(f"MongoDB: {exc}")
    if c2.button("Test Claude"):
        claude = get_claude()
        if claude is None:
            st.error("ANTHROPIC_API_KEY is not set.")
        else:
            try:
                st.success(f"Claude works: {claude.check_model()}")
            except ClaudeError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"Claude: {exc}")
    if c3.button("Test Apify"):
        token = get_secret("APIFY_TOKEN")
        if not token:
            st.error("APIFY_TOKEN is not set.")
        else:
            try:
                from apify_client import ApifyClient

                user = ApifyClient(token).user().get()
                name = getattr(user, "username", None) or (user.get("username") if isinstance(user, dict) else None)
                st.success(f"Apify works{': ' + name if name else ''}.")
            except Exception as exc:
                st.error(f"Apify: {exc}")
    if c4.button("Test Gmail"):
        address, password = gmail_credentials()
        if not (address and password):
            st.error("GMAIL_ADDRESS and GMAIL_APP_PASSWORD are required.")
        else:
            try:
                check_login(address, password)
                st.success(f"Gmail login works for {address}.")
            except MailError as exc:
                st.error(str(exc))

    with st.expander("How to get each secret"):
        st.markdown(
            """
- **ANTHROPIC_API_KEY**: create a key at platform.claude.com > API keys.
- **APIFY_TOKEN**: apify.com > Settings > API & Integrations. Searches use the `harvestapi/linkedin-post-search` actor (about $0.002 per post).
- **GMAIL_APP_PASSWORD**: turn on 2-Step Verification for your Google account, then open Google Account > Security > App passwords and create one for "Mail". Paste the 16 characters.
- **MONGODB_URI**: your Atlas connection string. On Streamlit Cloud, allow access from anywhere (0.0.0.0/0) in Atlas > Network Access, because Streamlit Cloud has no fixed IP.
- **APP_PASSWORD**: any password you choose. The app asks for it before showing anything.
"""
        )
