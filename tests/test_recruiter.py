from job_agent.recruiter import classify, emails_in_text, is_valid_email


def test_classification():
    assert classify("careers@acme.com") == "recruiting_inbox"
    assert classify("talent.acquisition@acme.com") == "recruiting_inbox"
    assert classify("priya.sharma@acme.com") == "personal"
    assert classify("privacy@acme.com") is None
    assert classify("accommodations@acme.com") is None
    assert classify("noreply@acme.com") is None


def test_extract_only_literal_emails():
    text = "Send your CV to hiring@acme.in. For privacy see privacy@acme.in. Contact Priya (priya.s@acme.in)."
    got = {c.email for c in emails_in_text(text, "https://acme/job")}
    assert got == {"hiring@acme.in", "priya.s@acme.in"}
    assert all(c.verification == "verified_on_page" for c in emails_in_text(text, "u"))
    assert emails_in_text("no emails here", "u") == []


def test_valid_email():
    assert is_valid_email("a.b@c.co")
    assert not is_valid_email("not-an-email")
