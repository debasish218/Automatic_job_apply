from datetime import datetime, timedelta, timezone

from job_agent.search.contacts import ContactFinder, ContactOut, ContactsOut, Source
from job_agent.search.fetch import is_data_broker, is_never_fetch
from job_agent.search.job_search import canonical_url, from_json_ld, is_stale, site_of


def test_site_detection_only_individual_postings():
    assert site_of("https://www.naukri.com/job-listings-ai-ml-engineer-ces-ltd-hyderabad-0-to-2-years-281026017123") == "naukri"
    assert site_of("https://in.linkedin.com/jobs/view/ai-engineer-at-aira-ai-4307650520") == "linkedin"
    assert site_of("https://www.foundit.in/job/ai-ml-engineer-acme-bengaluru-123") == "foundit"
    assert site_of("https://jobs.lever.co/acme/0f1e2d3c-4b5a-6978-8091-a2b3c4d5e6f7") == "lever"
    assert site_of("https://www.naukri.com/ai-jobs-in-hyderabad-secunderabad") is None          # listing page
    assert site_of("https://in.linkedin.com/jobs/machine-learning-engineer-jobs-bengaluru") is None


def test_canonical_url():
    assert canonical_url("https://in.linkedin.com/jobs/view/x-123?trk=abc") == "https://www.linkedin.com/jobs/view/x-123"
    assert canonical_url("https://in.indeed.com/viewjob?jk=abc&from=serp") == "https://in.indeed.com/viewjob?jk=abc"


def test_json_ld_mapping():
    ld = {"@type": "JobPosting", "title": "ML Engineer", "description": "<p>Build <b>models</b></p>",
          "hiringOrganization": {"name": "Acme AI", "sameAs": "https://acme.ai"},
          "jobLocation": [{"address": {"addressLocality": "Hyderabad", "addressCountry": "IN"}}],
          "experienceRequirements": {"monthsOfExperience": 24}, "datePosted": "2026-11-01"}
    c = from_json_ld(ld, "https://www.foundit.in/job/x-1", "foundit", "snip")
    assert c.posting.company == "Acme AI" and c.posting.location.startswith("Hyderabad")
    assert c.min_years == 2 and "Build" in c.posting.description and c.company_url == "https://acme.ai"


def test_stale_detection():
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    assert "older than" in is_stale(old, None, 45)
    assert "expired" in is_stale(None, "2020-01-01", 45)
    assert is_stale(datetime.now(timezone.utc).isoformat(), None, 45) is None
    assert is_stale("3 days ago", None, 45) is None


def test_never_fetch_lists():
    assert is_never_fetch("https://in.linkedin.com/in/someone")
    assert is_data_broker("https://rocketreach.co/acme-email-format")
    assert not is_never_fetch("https://www.foundit.in/job/x")


def _finder():
    return ContactFinder(searx=None, fetcher=None, gemini=None)


def test_validate_drops_emails_not_in_source():
    sources = [Source(0, "https://acme.ai/careers", "Careers", "", "Send CV to careers@acme.ai today", True),
               Source(1, "https://in.linkedin.com/in/priya", "Priya Rao - Talent Acquisition - Acme AI | LinkedIn", "")]
    out = ContactsOut(company_domain="acme.ai", contacts=[
        ContactOut(name="", designation="", email="careers@acme.ai", linkedin_url="", source_id=0,
                   evidence_quote="Send CV to careers@acme.ai", kind="careers_inbox", works_at_company=True),
        ContactOut(name="Priya Rao", designation="Talent Acquisition", email="priya.rao@acme.ai",   # invented!
                   linkedin_url="https://in.linkedin.com/in/priya", source_id=1, evidence_quote="Priya Rao - Talent Acquisition",
                   kind="recruiter", works_at_company=True),
        ContactOut(name="Bob", designation="HR", email="bob@other.com", linkedin_url="", source_id=1,
                   evidence_quote="x", kind="recruiter", works_at_company=False),
        ContactOut(name="", designation="", email="j***@acme.ai", linkedin_url="", source_id=0,
                   evidence_quote="", kind="hr_inbox", works_at_company=True),
    ])
    got = _finder()._validate(out, sources, "acme.ai")
    emails = {c.email for c in got}
    assert "careers@acme.ai" in emails
    assert "priya.rao@acme.ai" not in emails                      # fabricated email removed
    assert "bob@other.com" not in emails and not any("*" in (e or "") for e in emails)
    priya = next(c for c in got if c.name == "Priya Rao")
    assert priya.email is None and priya.linkedin == "https://in.linkedin.com/in/priya"
    assert priya.verification == "linkedin_search_result"
    careers = next(c for c in got if c.email == "careers@acme.ai")
    assert careers.verification == "verified_on_page" and careers.domain_match is True


def test_malformed_urls_do_not_crash():
    from job_agent.search.contacts import company_domain_from_url
    from job_agent.search.fetch import host_of
    assert host_of("http://[not-ipv6/careers") == ""
    assert company_domain_from_url("https://[broken") is None
    assert company_domain_from_url("https://www.zensar.com/careers") == "zensar.com"
