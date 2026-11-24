from job_agent.search.fetch import Page, _markup_emails, decode_cfemail
from job_agent.search.site_emails import classify_site_email, crawl_site_emails, site_mentions
from job_agent.startups import best_startup_recipient, has_recruiter_email, has_specific_email, pick_company_site
from job_agent.search.searx import SearchResult


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        p = self.pages.get(url.rstrip("/"))
        return p or Page(url, False, "http_404")


def page(url, text, mailtos=(), links=()):
    return Page(url, True, "fetched", text, [], list(mailtos), list(links), url)


def test_recruiter_email_preferred_over_general():
    f = FakeFetcher({
        "https://acmevision.in": page("https://acmevision.in", "AcmeVision builds AI quality control. Write to hello@acmevision.in",
                                   links=[("https://acmevision.in/careers", "Careers")]),
        "https://acmevision.in/careers": page("https://acmevision.in/careers",
                                           "Join AcmeVision. Send your resume to careers@acmevision.in"),
        "https://acmevision.in/contact": page("https://acmevision.in/contact", "Contact", mailtos=[("info@acmevision.in", "Email us")]),
    })
    emails, msg = crawl_site_emails(f, "acmevision.in", "AcmeVision")
    kinds = [(e.email, e.kind) for e in emails]
    assert kinds[0] == ("careers@acmevision.in", "careers_inbox")
    assert ("hello@acmevision.in", "general_inbox") in kinds and ("info@acmevision.in", "general_inbox") in kinds
    assert f.calls[1] == "https://acmevision.in/careers"            # careers page read right after the homepage


def test_wrong_website_not_crawled():
    f = FakeFetcher({"https://acmeledger.example": page("https://acmeledger.example", "AcmeLedger — accounting software. hi@acmeledger.example")})
    emails, msg = crawl_site_emails(f, "acmeledger.example", "AcmeVision")
    assert emails == [] and "does not mention" in msg


def test_site_email_classification():
    k = lambda *a: (classify_site_email(*a) or [None])[0]
    assert k("privacy@x.ai", "x.ai", "https://x.ai/contact", "") is None
    assert k("someone@agency.com", "x.ai", "https://x.ai/", "") is None                 # other company
    assert k("ravi@x.ai", "x.ai", "https://x.ai/careers", "send your resume to") == "recruiter"
    assert k("ravi@x.ai", "x.ai", "https://x.ai/careers", "") == "team_member"         # footer on careers page
    assert k("solutions@x.ai", "x.ai", "https://x.ai/careers", "") == "general_inbox"   # department inbox
    assert k("ravi.x@gmail.com", "x.ai", "https://x.ai/careers", "send your resume") == "recruiter"
    assert k("x@gmail.com", "x.ai", "https://x.ai/about", "") is None
    people = [("Asha Verma", "Founder & CEO"), ("Venkatesh Iyer", "CTO"), ("Priya Rao", "Engineer")]
    assert classify_site_email("asha@x.ai", "x.ai", "https://x.ai/about", "", people) == \
        ("founder", "Asha Verma", "Founder & CEO")
    assert k("venkat@x.ai", "x.ai", "https://x.ai/", "", people) == "founder"
    assert k("priya.rao@x.ai", "x.ai", "https://x.ai/", "", people) == "team_member"
    assert k("ceo@x.ai", "x.ai", "https://x.ai/", "") == "founder"
    assert site_mentions("Welcome to Nimbus Semi", "Nimbus Semi")


def test_mailto_and_cloudflare_links():
    from bs4 import BeautifulSoup
    enc = "42" + "".join("%02x" % (ord(c) ^ 0x42) for c in "jobs@x.ai")
    html = f'<a href="mailto:Hello@x.ai?subject=Hi">Mail</a><a href="/cdn-cgi/l/email-protection#{enc}">e</a><a href="/careers">Careers</a>'
    mailtos, links = _markup_emails(BeautifulSoup(html, "html.parser"), "https://x.ai/")
    assert ("Hello@x.ai", "Mail") in mailtos and ("jobs@x.ai", "e") in mailtos
    assert ("https://x.ai/careers", "Careers") in links
    assert decode_cfemail("zz") is None


def test_recipient_priority_general_last():
    recs = [{"email": "hello@x.ai", "kind": "general_inbox", "verification": "verified_on_page", "domain_match": 1},
            {"email": "ravi@x.ai", "kind": "team_member", "verification": "verified_on_page", "domain_match": 1},
            {"email": "careers@x.ai", "kind": "careers_inbox", "verification": "verified_on_page", "domain_match": 1}]
    founder = {"email": "asha@x.ai", "kind": "founder", "verification": "verified_on_page", "domain_match": 1}
    assert best_startup_recipient(recs + [founder])["email"] == "careers@x.ai"     # recruiter mail first
    assert best_startup_recipient(recs[:2] + [founder])["email"] == "asha@x.ai"  # then founder
    assert best_startup_recipient(recs[:2])["email"] == "ravi@x.ai"
    assert best_startup_recipient(recs[:1])["email"] == "hello@x.ai"               # general inbox as last resort
    assert not has_specific_email(recs[:2]) and has_specific_email(recs)
    assert not has_recruiter_email(recs[:2] + [founder]) and has_recruiter_email(recs)


def test_pick_company_site():
    r = lambda u: SearchResult(u, "", "", [])
    res = [r("https://www.linkedin.com/company/nimbussemi"), r("https://inc42.com/x"), r("https://nimbussemi.in/")]
    assert pick_company_site("Nimbus Semi", res) == "nimbussemi.in"
    assert pick_company_site("AcmeVision", [r("https://tracxn.com/countai")]) is None
