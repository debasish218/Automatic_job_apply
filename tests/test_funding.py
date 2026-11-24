from datetime import date

from job_agent import db as dbm
from job_agent.search.funding import (Deal, DealCandidate, check_deal, domain_from_website, location_policy,
                                      norm_round, parse_date, verified_evidence)
from job_agent.startup_fit import StartupFit, apply_startup_policy

TODAY = date(2026, 11, 23)


def deal(**kw):
    d = dict(company="NeuroLoop", website="neuroloop.ai", description="LLM agents for insurance claims",
             sector="Generative AI", is_ai_related=True, funding_round="Seed", amount="$3 million",
             funding_date="2026-11-10", investors=["Accel"], hq_city="Bengaluru", hq_country="India",
             founded_year="2024", founders=["A. Rao (CEO)"], india_presence="", hires_from_india_evidence="",
             use_of_funds="hiring engineers", evidence_quote="NeuroLoop has raised $3 million in a seed round")
    d.update(kw)
    return Deal(**d)


def cand(d, text=None):
    text = text or "NeuroLoop has raised $3 million in a seed round led by Accel."
    return DealCandidate(d, "https://inc42.com/x", "NeuroLoop raises $3M", text, None, "snippet",
                         parse_date(d.funding_date))


def test_round_normalisation():
    assert norm_round("Seed") == "seed"
    assert norm_round("Pre-Seed") == "pre-seed"
    assert norm_round("pre seed") == "pre-seed"
    assert norm_round("Series A") == "series a"
    assert norm_round("Pre-Series A") == "pre-series a"
    assert norm_round("Series B") == "series b"


def test_parse_date_fallbacks():
    assert parse_date("", "2026-11-02T08:00:00Z") == date(2026, 11, 2)
    assert parse_date("Nov 2026", None) is None


def test_indian_seed_ai_kept():
    c = cand(deal())
    assert check_deal(c, 30, TODAY) is None
    assert c.region == "india"


def test_old_wrong_round_non_ai_rejected():
    assert "older than 30" in check_deal(cand(deal(funding_date="2026-09-01")), 30, TODAY)
    assert "round" in check_deal(cand(deal(funding_round="Series B")), 30, TODAY)
    assert "not AI" in check_deal(cand(deal(is_ai_related=False, sector="D2C apparel")), 30, TODAY)
    assert "date unknown" in check_deal(cand(deal(funding_date="")), 30, TODAY)


def test_company_must_appear_in_source():
    c = cand(deal(company="Ghost Labs"))
    assert "not found" in check_deal(c, 30, TODAY)


def test_location_policy():
    assert location_policy(deal(hq_city="Tokyo", hq_country="Japan"))[1]           # no India evidence: rejected
    region, why = location_policy(deal(hq_city="Tokyo", hq_country="Japan",
                                       hires_from_india_evidence="building its engineering team in Bengaluru"))
    assert region == "japan" and why is None
    region, why = location_policy(deal(hq_city="Dubai", hq_country="UAE",
                                       india_presence="R&D centre in Hyderabad"))
    assert region == "uae" and why is None
    assert location_policy(deal(hq_city="Austin", hq_country="USA"))[1]             # other countries excluded
    region, why = location_policy(deal(hq_city="San Francisco", hq_country="USA",
                                       india_presence="engineering team in Pune"))
    assert region == "india_team" and why is None


def test_non_indian_penalty_and_threshold():
    fit = StartupFit(score=75, matching_skills=[], relevance="high", recommendation="reach_out", reasoning="r",
                     best_resume="a.pdf", likely_roles=[], hiring_signals=[], talking_points=[])
    assert apply_startup_policy(fit.model_copy(), "india").score == 75
    f = apply_startup_policy(fit.model_copy(), "japan")
    assert f.score == 65 and f.recommendation == "consider"
    low = apply_startup_policy(fit.model_copy(update={"score": 45}), "india")
    assert low.recommendation == "skip"


def test_evidence_must_be_verbatim():
    c = cand(deal(evidence_quote="NeuroLoop raised $50 million"))       # not in text -> fallback to snippet
    assert verified_evidence(c).startswith("NeuroLoop raises $3M")
    assert verified_evidence(cand(deal())).startswith("NeuroLoop has raised")


def test_domain_from_website():
    assert domain_from_website("https://www.neuroloop.ai/") == "neuroloop.ai"
    assert domain_from_website("neuroloop.ai") == "neuroloop.ai"
    assert domain_from_website("not stated") is None


def test_startup_db_dedupe_and_shared_daily_limit(tmp_path):
    d = dbm.Database(tmp_path / "t.db")
    sid = d.insert_startup({"name": "Sarvam AI Pvt Ltd", "domain": "sarvam.ai", "founders": ["X"],
                            "investors": ["Y"], "source_url": "https://a"})
    assert d.find_startup("Sarvam")["id"] == sid
    assert d.find_startup("Other name", "sarvam.ai")["id"] == sid
    d.add_startup_source(sid, "https://b")
    assert d.get_startup(sid)["other_sources"] == ["https://b"]
    eid = d.save_startup_email(sid, "Hello@Sarvam.ai", None, "s", "b", "/tmp/x.pdf", None, None)
    d.update_startup_email(eid, status="sent", sent_at=dbm.now())
    assert d.sent_today() == 1
    assert "Sarvam" in d.recipient_recently_emailed("hello@sarvam.ai", 30)


def test_skip_headlines_about_known_startups():
    from job_agent.startups import mentions_known
    known = ["bharat semi", "humyn labs", "ai"]
    assert mentions_known("Bharat Semi raises $24 mn for AI sensors", known)
    assert mentions_known("India's Humyn Labs raises $47M in Series A", known)
    assert not mentions_known("Funding roundup: 12 Indian startups raised $80M this week", known)
