"""Gmail sending (SMTP) and reply lookup (IMAP) using a Google app password."""

from __future__ import annotations

import email
import email.header
import imaplib
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage, Message
from email.utils import formataddr, formatdate, make_msgid, parseaddr, parsedate_to_datetime
from typing import Any, Callable, Iterable

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
IMAP_HOST = "imap.gmail.com"

PUBLIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com", "live.com",
    "rediffmail.com", "icloud.com", "proton.me", "protonmail.com", "aol.com", "zoho.com", "gmx.com",
}

EMAIL_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class MailError(RuntimeError):
    pass


def is_valid_address(address: str) -> bool:
    return bool(EMAIL_ADDRESS_RE.match((address or "").strip()))


def build_message(
    *,
    sender_email: str,
    sender_name: str,
    to: str,
    subject: str,
    body: str,
    attachments: Iterable[tuple[str, bytes, str]] = (),
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_email)) if sender_name else sender_email
    msg["To"] = to.strip()
    msg["Subject"] = subject.strip()
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender_email.split("@")[-1] if "@" in sender_email else None)
    msg.set_content(body)
    for filename, data, mime in attachments:
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)
    return msg


def send_message(
    msg: EmailMessage,
    *,
    gmail_address: str,
    app_password: str,
    smtp_factory: Callable[..., Any] = smtplib.SMTP_SSL,
) -> str:
    """Sends through Gmail. Gmail also files a copy in the Sent folder. Returns the Message-ID."""
    try:
        with smtp_factory(SMTP_HOST, SMTP_PORT, timeout=60) as smtp:
            smtp.login(gmail_address, (app_password or "").replace(" ", ""))
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(
            "Gmail rejected the login. Use a 16-character app password (Google Account > Security > App passwords), "
            "not your normal password."
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"Sending failed: {exc}") from exc
    return str(msg["Message-ID"])


def check_login(gmail_address: str, app_password: str, smtp_factory: Callable[..., Any] = smtplib.SMTP_SSL) -> None:
    try:
        with smtp_factory(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(gmail_address, (app_password or "").replace(" ", ""))
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError("Gmail rejected the app password.") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"Could not connect to Gmail: {exc}") from exc


# ----- replies -----
@dataclass
class Reply:
    sender: str
    subject: str
    date: datetime | None
    snippet: str
    message_id: str
    matched_by: str


def imap_date(dt: datetime) -> str:
    """IMAP wants 15-Nov-2026 regardless of locale."""
    return f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year}"


def message_text(msg: Message, limit: int = 4000) -> str:
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
                continue
            if part.get_content_type() == "text/plain":
                parts.append(_decode(part))
        if not parts:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    parts.append(_strip_html(_decode(part)))
    else:
        text = _decode(msg)
        parts.append(_strip_html(text) if msg.get_content_type() == "text/html" else text)
    text = "\n".join(parts)
    # Drop the quoted original message so the snippet shows only the new reply.
    text = re.split(r"\n(?:On .{5,200}wrote:|-{2,} ?Original Message ?-{2,})", text, maxsplit=1)[0]
    text = "\n".join(line for line in text.splitlines() if not line.startswith(">"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:limit]


def _decode(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return str(part.get_payload() or "")
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
    return re.sub(r"<[^>]+>", "", html)


def _header(msg: Message, name: str) -> str:
    value = msg.get(name, "")
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except Exception:
        return str(value)


class ReplyChecker:
    """Opens one IMAP session and looks up replies for many applications."""

    def __init__(self, gmail_address: str, app_password: str, imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL):
        self.gmail_address = gmail_address.lower()
        self.app_password = (app_password or "").replace(" ", "")
        self.imap_factory = imap_factory
        self.conn: Any = None

    def __enter__(self) -> "ReplyChecker":
        try:
            self.conn = self.imap_factory(IMAP_HOST)
            self.conn.login(self.gmail_address, self.app_password)
        except imaplib.IMAP4.error as exc:
            raise MailError("Gmail IMAP login failed. Check the app password and that IMAP is enabled.") from exc
        except OSError as exc:
            raise MailError(f"Could not connect to Gmail IMAP: {exc}") from exc
        status, _ = self.conn.select('"[Gmail]/All Mail"', readonly=True)
        if status != "OK":
            self.conn.select("INBOX", readonly=True)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self.conn.logout()
        except Exception:
            pass

    def _search(self, criteria: str) -> list[bytes]:
        status, data = self.conn.uid("SEARCH", None, criteria)
        if status != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def find_replies(self, *, recipient: str, since: datetime, message_id: str | None) -> list[Reply]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        since_str = imap_date((since - timedelta(days=1)).astimezone(timezone.utc))
        recipient = (recipient or "").strip().lower()
        matches: dict[bytes, str] = {}
        if message_id:
            for uid in self._search(f'(HEADER In-Reply-To "{message_id}")'):
                matches.setdefault(uid, "thread")
            for uid in self._search(f'(HEADER References "{message_id}")'):
                matches.setdefault(uid, "thread")
        if recipient:
            for uid in self._search(f'(SINCE {since_str} FROM "{recipient}")'):
                matches.setdefault(uid, "sender")
            domain = recipient.split("@")[-1]
            if domain and domain not in PUBLIC_DOMAINS:
                for uid in self._search(f'(SINCE {since_str} FROM "@{domain}")'):
                    matches.setdefault(uid, "company domain")

        replies: list[Reply] = []
        for uid, how in matches.items():
            status, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                continue
            msg = email.message_from_bytes(data[0][1])
            sender = parseaddr(_header(msg, "From"))[1].lower()
            if sender == self.gmail_address:
                continue  # our own message
            try:
                date = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
                if date and date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                date = None
            if date and since and date < since and how != "thread":
                continue
            replies.append(
                Reply(
                    sender=sender,
                    subject=_header(msg, "Subject"),
                    date=date,
                    snippet=message_text(msg),
                    message_id=str(msg.get("Message-ID", "")),
                    matched_by=how,
                )
            )
        replies.sort(key=lambda r: r.date or datetime.min.replace(tzinfo=timezone.utc))
        return replies
