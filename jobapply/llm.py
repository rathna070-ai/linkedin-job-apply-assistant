"""Claude calls: screen posts, draft emails, tailor resumes, classify replies."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

import anthropic
from pydantic import BaseModel

from .config import DEFAULT_MODEL
from .schemas import EmailDraft, PostAnalysis, PostAnalysisBatch, ReplyAssessment, TailoredResume

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
ANALYSIS_BATCH_SIZE = 8


class ClaudeError(RuntimeError):
    pass


ANALYZE_SYSTEM = """You screen LinkedIn posts for a job seeker and score how well each one fits their resume.

For every post you receive, return one result with the same post_index:
- is_hiring_post: true only if the post advertises a specific open role that someone can apply for. Job-seeker posts, generic articles, event or course promotions are false.
- company, role, location, experience_required, salary: copy them from the post. Use "Not stated" when the post does not say. If the post lists several roles, pick the one that best fits the candidate.
- contact_email: the address the post asks applicants to send resumes to. Use "" if there is none.
- job_description: the role's responsibilities and requirements from the post, without hashtags or emojis. Keep every requirement detail.
- match_score: an integer from 0 to 100 for how well the candidate fits this role, judged on seniority and years, role type, domain, skills and tools, and location. Be calibrated: 85 or more means the candidate meets the core must-haves; 60 to 84 is a partial fit with real gaps; below 60 is a weak fit. Non-hiring posts get 0.
- match_reasons: one or two sentences on what matches.
- gaps: one or two sentences on what the post asks for that the resume does not show. Use "" if nothing is missing.

Base everything on the post text and the resume. Do not guess facts that neither states.

<candidate_resume>
{resume}
</candidate_resume>"""

EMAIL_SYSTEM = """You write short job application emails for a candidate, to send to the recruiter named in a LinkedIn hiring post.

Write in plain text, no markdown. Follow this shape:
1. Greeting: "Hi <first name>," when the post author's name is known and the email goes to them, otherwise "Hi Hiring Team,".
2. One or two sentences: the role and company being applied for, and the candidate's years of experience and focus.
3. A line "Relevant to your requirements:" followed by three to five bullets starting with "- ". Each bullet pairs a requirement from the post with concrete evidence from the resume (numbers, team sizes, outcomes).
4. If the post has a hard requirement the resume clearly does not meet, say so in one honest sentence and offer to discuss how related experience maps.
5. One sentence saying the resume is attached, and that CTC, notice period and other details can be shared on request.
6. "Thank you." then the signature block exactly as given.

Rules: every claim must come from the resume. Never invent experience, certifications, numbers or domains. Keep the body between 120 and 230 words before the signature. The subject line is "Application: <Role> - <Candidate name>", with the location added when the post names one city."""

TAILOR_SYSTEM = """You tailor a candidate's resume to one job description.

You may reorder sections and bullets, rewrite the headline and summary, rephrase bullets to use the job description's vocabulary, drop bullets that are irrelevant to this role, and move the most relevant competencies first.

You must not invent anything. Every role, employer, date, client, number, certification, award, degree and tool must already appear in the master resume. Only use a job description keyword when the master resume already supports it. Keep all employers and dates. Keep the candidate's contact details exactly as written.

Fill the fields like this:
- headline: one line of role titles that fit this job, taken from the candidate's real titles and focus.
- contact_line: location | email | phone, exactly as in the master resume.
- links_line: profile links from the master resume, separated by " | ".
- summary: three to five sentences aimed at this job.
- highlights: three or four quantified achievements from the resume, most relevant first.
- competencies: groups with a short label and a comma-separated list of items.
- experience: every role from the master resume, newest first. context holds the client, engagement size or team line when the resume has one, otherwise "".
- projects and projects_tech: from the resume's projects section, or empty if it has none.
- certifications, awards, education: from the resume.
- extra_line: languages or similar one-line extras, or "".
- jd_keywords_used: job description keywords now present in the tailored resume.
- gaps: job description requirements the master resume does not support. These go to the candidate for review and are not added to the resume."""

REPLY_SYSTEM = """You classify a reply to a job application email.

Categories:
- interview: the sender wants to schedule a call, interview or assessment.
- request_info: the sender asks for details such as CTC, notice period, an updated resume or documents.
- rejected: the sender says the candidate is not shortlisted or the role is closed.
- auto_reply: an automatic acknowledgement or out-of-office message.
- replied: a human reply that fits none of the above.
- other: not related to the application.

summary: one sentence on what the sender wants or said."""


class Claude:
    def __init__(self, api_key: str | None, model: str = DEFAULT_MODEL, client: Any = None):
        if client is None:
            if not api_key:
                raise ClaudeError("ANTHROPIC_API_KEY is not set.")
            client = anthropic.Anthropic(api_key=api_key)
        self.client = client
        self.model = model

    def _parse(self, *, system: str, user: str, schema: type[T], effort: str, max_tokens: int = 16000) -> T:
        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                output_config={"effort": effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as exc:
            raise ClaudeError("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY.") from exc
        except anthropic.PermissionDeniedError as exc:
            raise ClaudeError(f"The Anthropic API key lacks permission: {exc.message}") from exc
        except anthropic.NotFoundError as exc:
            raise ClaudeError(f"Model '{self.model}' was not found. Check CLAUDE_MODEL.") from exc
        except anthropic.RateLimitError as exc:
            raise ClaudeError("Claude rate limit reached. Wait a minute and try again.") from exc
        except anthropic.APIStatusError as exc:
            raise ClaudeError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ClaudeError("Could not reach the Claude API. Check the network connection.") from exc

        if response.stop_reason == "refusal":
            raise ClaudeError("Claude declined this request.")
        if response.stop_reason == "max_tokens":
            raise ClaudeError("Claude's answer was cut off before it finished. Try again.")
        parsed = response.parsed_output
        if parsed is None:
            raise ClaudeError("Claude did not return the expected structured output.")
        return parsed

    # ----- post screening -----
    def analyze_posts(
        self,
        posts: list[dict[str, Any]],
        resume_text: str,
        progress: Callable[[int, int], None] | None = None,
        max_workers: int = 4,
    ) -> dict[str, dict[str, Any]]:
        """Returns {post_id: analysis dict}. Posts are sent in batches, several batches at once."""
        system = ANALYZE_SYSTEM.format(resume=resume_text)
        batches = [posts[i : i + ANALYSIS_BATCH_SIZE] for i in range(0, len(posts), ANALYSIS_BATCH_SIZE)]
        results: dict[str, dict[str, Any]] = {}
        done = 0

        def run(batch: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
            payload = [
                {
                    "post_index": i,
                    "author": p.get("author_name", ""),
                    "author_headline": p.get("author_headline", ""),
                    "posted": p.get("posted_at", ""),
                    "emails_found": p.get("emails", []),
                    "text": p.get("text", ""),
                }
                for i, p in enumerate(batch)
            ]
            user = (
                f"Screen these {len(batch)} LinkedIn posts. Return exactly one result per post_index.\n\n"
                f"<posts>\n{json.dumps(payload, ensure_ascii=False, indent=1)}\n</posts>"
            )
            out = self._parse(system=system, user=user, schema=PostAnalysisBatch, effort="medium")
            mapped: dict[str, dict[str, Any]] = {}
            for item in out.results:
                if 0 <= item.post_index < len(batch):
                    data = clean_analysis(item)
                    source = batch[item.post_index]
                    mapped[source.get("_id") or source["post_id"]] = data
            return mapped

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for mapped in pool.map(run, batches):
                results.update(mapped)
                done += 1
                if progress:
                    progress(done, len(batches))
        return results

    # ----- email drafting -----
    def draft_email(
        self,
        *,
        post: dict[str, Any],
        resume_text: str,
        signature: str,
        candidate_name: str,
        recipient: str,
        tailored: bool,
    ) -> EmailDraft:
        analysis = post.get("analysis") or {}
        user = (
            "<job_post>\n"
            f"Company: {analysis.get('company', '')}\n"
            f"Role: {analysis.get('role', '')}\n"
            f"Location: {analysis.get('location', '')}\n"
            f"Experience asked: {analysis.get('experience_required', '')}\n"
            f"Post author: {post.get('author_name', '')} ({post.get('author_headline', '')})\n"
            f"Email goes to: {recipient}\n"
            f"Requirements:\n{analysis.get('job_description', '')}\n\n"
            f"Original post text:\n{post.get('text', '')}\n"
            "</job_post>\n\n"
            f"<resume>\n{resume_text}\n</resume>\n\n"
            f"Candidate name: {candidate_name}\n"
            f"The attached resume is {'tailored to this job' if tailored else 'the candidate’s standard resume'}.\n\n"
            f"<signature>\n{signature}\n</signature>\n\n"
            "Write the application email."
        )
        return self._parse(system=EMAIL_SYSTEM, user=user, schema=EmailDraft, effort="medium")

    # ----- resume tailoring -----
    def tailor_resume(self, *, resume_text: str, post: dict[str, Any]) -> TailoredResume:
        analysis = post.get("analysis") or {}
        user = (
            f"<master_resume>\n{resume_text}\n</master_resume>\n\n"
            "<job_description>\n"
            f"Company: {analysis.get('company', '')}\n"
            f"Role: {analysis.get('role', '')}\n"
            f"{analysis.get('job_description') or post.get('text', '')}\n"
            "</job_description>\n\n"
            "Tailor the resume to this job."
        )
        return self._parse(system=TAILOR_SYSTEM, user=user, schema=TailoredResume, effort="high")

    # ----- reply classification -----
    def classify_reply(self, *, application: dict[str, Any], reply_subject: str, reply_text: str) -> ReplyAssessment:
        user = (
            f"Application: {application.get('role', '')} at {application.get('company', '')}\n"
            f"Reply subject: {reply_subject}\n\n<reply>\n{reply_text}\n</reply>"
        )
        return self._parse(system=REPLY_SYSTEM, user=user, schema=ReplyAssessment, effort="low", max_tokens=4000)

    def check_model(self) -> str:
        info = self.client.models.retrieve(self.model)
        return getattr(info, "display_name", None) or self.model


def clean_analysis(item: PostAnalysis) -> dict[str, Any]:
    data = item.model_dump()
    data.pop("post_index", None)
    data["match_score"] = max(0, min(100, int(data.get("match_score") or 0)))
    if not data.get("is_hiring_post"):
        data["match_score"] = 0
    data["contact_email"] = (data.get("contact_email") or "").strip()
    return data
