import email
from datetime import datetime, timezone

from jobapply.mailer import build_message, imap_date, is_valid_address, message_text, send_message


def test_build_message_has_attachment_and_message_id():
    msg = build_message(
        sender_email="me@gmail.com",
        sender_name="Asha Rao",
        to="hr@acme.com",
        subject="Application: QA Lead - Asha Rao",
        body="Hi,\n\nPlease find my resume attached.",
        attachments=[("resume.pdf", b"%PDF-1.4 test", "application/pdf")],
    )
    assert msg["Message-ID"].endswith("@gmail.com>")
    assert msg["From"] == "Asha Rao <me@gmail.com>"
    parts = list(msg.iter_attachments())
    assert len(parts) == 1 and parts[0].get_filename() == "resume.pdf"
    assert parts[0].get_content() == b"%PDF-1.4 test"


class FakeSMTP:
    sent: list = []
    fail = False

    def __init__(self, host, port, timeout):
        assert (host, port) == ("smtp.gmail.com", 465)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        import smtplib

        if FakeSMTP.fail:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        assert password == "abcdefghijklmnop"  # spaces removed

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_send_message_strips_app_password_spaces():
    msg = build_message(sender_email="me@gmail.com", sender_name="", to="hr@acme.com", subject="s", body="b")
    mid = send_message(msg, gmail_address="me@gmail.com", app_password="abcd efgh ijkl mnop", smtp_factory=FakeSMTP)
    assert mid == msg["Message-ID"]
    assert FakeSMTP.sent[-1] is msg


def test_imap_date_is_locale_independent():
    assert imap_date(datetime(2026, 11, 5, tzinfo=timezone.utc)) == "05-Nov-2026"


def test_message_text_drops_quoted_original():
    raw = (
        "From: hr@acme.com\r\nSubject: Re: Application\r\nContent-Type: text/plain\r\n\r\n"
        "Thanks, can we talk tomorrow at 11?\r\n\r\nOn Sat, 15 Nov 2026 at 10:00, Asha wrote:\r\n> Hi, applying..."
    )
    assert message_text(email.message_from_string(raw)) == "Thanks, can we talk tomorrow at 11?"


def test_is_valid_address():
    assert is_valid_address("a.b@acme.co.in")
    assert not is_valid_address("not an email")
