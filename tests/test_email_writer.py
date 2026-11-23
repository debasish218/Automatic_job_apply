from types import SimpleNamespace

from job_agent.email_writer import EmailParts, assemble, safe_filename

PROFILE = SimpleNamespace(name="Debasish Mahapatra", phone="+91 90000 00000", email="d@example.com",
                          linkedin="https://www.linkedin.com/in/x", github="")


def test_assemble_always_has_line_breaks():
    parts = EmailParts(subject="s", greeting="Dear Hiring Team",
                       paragraphs=["I am applying for the ML Engineer role.\n  I have 1 year and 8 months...",
                                   "  Skills: PyTorch, FastAPI.  ", "", "My resume is attached."],
                       sign_off="Best regards,", attachment_filename="x.pdf")
    body = assemble(parts, PROFILE)
    lines = body.split("\n")
    assert lines[0] == "Dear Hiring Team," and lines[1] == ""
    assert "I am applying for the ML Engineer role. I have 1 year and 8 months..." in body   # no stray breaks inside
    assert "\n\nSkills: PyTorch, FastAPI.\n\nMy resume is attached.\n\nBest regards,\nDebasish Mahapatra\n" in body
    assert body.endswith("https://www.linkedin.com/in/x")      # empty github omitted


def test_safe_filename():
    assert safe_filename("Debasish Mahapatra ML Engineer Resume.pdf", "D M", "t") == "Debasish_Mahapatra_ML_Engineer_Resume.pdf"
    assert safe_filename("../../etc/passwd", "Debasish Mahapatra", "AI/ML Engineer") == "etc_passwd.pdf"
    assert safe_filename("", "Debasish Mahapatra", "AI/ML Engineer") == "Debasish_Mahapatra_AI_ML_Engineer_Resume.pdf"
