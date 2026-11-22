from email import message_from_bytes, policy
from pathlib import Path

import pytest

from job_agent.gmail_client import build_message

RESUME = Path(__file__).resolve().parent.parent / "resumes" / "AI_ML.pdf"


def test_message_has_body_and_pdf_attachment():
    m = build_message("me@x.com", "hr@acme.com", "ML Engineer – Name", "Hello\nBody", RESUME)
    parsed = message_from_bytes(m.as_bytes(), policy=policy.default)
    assert parsed["To"] == "hr@acme.com" and parsed["Subject"] == "ML Engineer – Name"
    parts = list(parsed.walk())
    att = [p for p in parts if p.get_filename() == "AI_ML.pdf"]
    assert att and att[0].get_content_type() == "application/pdf"
    assert att[0].get_payload(decode=True) == RESUME.read_bytes()


def test_missing_attachment_raises():
    with pytest.raises(FileNotFoundError):
        build_message(None, "a@b.com", "s", "b", Path("/nonexistent.pdf"))
