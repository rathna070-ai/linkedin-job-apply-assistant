# Job Apply Assistant

A Streamlit app that finds LinkedIn hiring posts by hashtag, scores each one against your resume with Claude, and emails your application from Gmail, with either your latest resume or one tailored to the job description. Every application goes into an audit trail that records the company, role, JD, send time and whether the recruiter replied.

## What it does

| Page | What you do there |
|---|---|
| **Find posts** | Enter hashtags (for example `#hiring`), optional role keywords and a location. The app searches LinkedIn posts through Apify, keeps posts that mention your hashtags and include an email address, and Claude scores each post from 0 to 100 against your resume. A **minimum match score (confidence)** slider decides which posts count as eligible. Tick the posts you want and shortlist them. |
| **Apply** | For each shortlisted post, choose **Apply with my latest resume** or **Tailor the resume to this JD**. Claude drafts the email (and the tailored resume, as PDF and DOCX), you edit and tick *reviewed*, then send. Emails go out from your Gmail with the PDF attached. The app warns you before you email an address you have already written to. |
| **Audit trail** | Every application with its company, role, JD, recipient, send time, resume used and reply status. **Check Gmail for replies** looks for replies in the same thread, from the same address, or from the same company domain, and Claude classifies each one (interview, asked for details, rejected, auto-reply). You can also set a status by hand, add notes, add applications sent elsewhere, and export to CSV or Excel. |
| **Settings** | Upload your latest resume (PDF to attach; DOCX optional for cleaner text), set your signature details, default resume choice and match threshold, and test each connection. |

Tailoring never invents experience. Claude may reorder, rephrase and drop bullets, but every employer, date, number and certification must already be in your master resume. Job requirements you don't meet are listed as gaps for you to review, not added to the resume.

## How it works

```
Hashtags ──► Apify (harvestapi/linkedin-post-search) ──► posts ──► Claude scores vs resume
                                                                     │
                       MongoDB ◄──── shortlist, drafts, tailored resumes, audit trail, events
                                                                     │
          Gmail SMTP (send) ◄── Claude drafts email / tailors resume ┘
          Gmail IMAP (read replies) ──► Claude classifies replies ──► audit trail
```

- `jobapply/scraper.py`: Apify actor call and post normalisation
- `jobapply/llm.py`: Claude calls with structured outputs (post scoring, email drafting, resume tailoring, reply classification)
- `jobapply/resume.py`: resume text extraction, and DOCX/PDF rendering of tailored resumes
- `jobapply/mailer.py`: Gmail sending and reply lookup with an app password
- `jobapply/store.py`: MongoDB collections `posts`, `applications`, `events`, `resumes`, `settings`
- `jobapply/services.py`: the workflows the pages call
- `views/`: the four Streamlit pages

Claude calls use `claude-opus-5-5` (set `CLAUDE_MODEL` to change it) with server-side refusal fallback enabled.

## Setup

You need:

1. **MongoDB Atlas** connection string.
2. **Anthropic API key** from platform.claude.com.
3. **Apify token** from apify.com > Settings > API & Integrations. The LinkedIn post search costs about $0.002 per post.
4. **Gmail app password**: turn on 2-Step Verification for your Google account, then go to Google Account > Security > App passwords and create one. The app needs this because a standalone app cannot use the Gmail connector inside claude.ai.

Run locally:

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .streamlit\secrets.toml.example .streamlit\secrets.toml   # then fill in the values
streamlit run app.py
```

Then open **Settings**, upload your resume, fill in your details and use the **Test** buttons.

## Deploy to Streamlit Community Cloud

1. Sign in at [share.streamlit.io](https://share.streamlit.io) with GitHub and allow access to this repository (private repositories are supported).
2. **Create app** > pick this repo, branch `main`, main file `app.py`. Under **Advanced settings**, choose Python 3.12 and paste the contents of your `secrets.toml` into **Secrets**.
3. In MongoDB Atlas > **Network Access**, add `0.0.0.0/0`. Streamlit Cloud has no fixed IP address, so the app can't reach Atlas otherwise.
4. Always set `APP_PASSWORD`. Without it, anyone with the link could send email from your Gmail.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests use an in-memory MongoDB, fake Apify, Claude and Gmail clients, and render every page headlessly. They never call real services.

## Notes

- Searching LinkedIn through a third-party scraper is subject to LinkedIn's terms. Keep volumes small and personal.
- Secrets stay in `.streamlit/secrets.toml`, which git ignores. Never commit it.
