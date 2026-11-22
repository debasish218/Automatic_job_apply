from job_agent import db as dbm


def make_job(**kw):
    j = dict(source="greenhouse", external_id="x:1", company="Acme", title="ML Engineer",
             location="Hyderabad", url="https://x", description="desc")
    j.update(kw)
    return j


def test_upsert_and_duplicates(tmp_path):
    d = dbm.Database(tmp_path / "t.db")
    jid, new = d.upsert_job(make_job())
    assert new
    jid2, new2 = d.upsert_job(make_job())
    assert jid2 == jid and not new2                    # same source id -> not new
    jid3, new3 = d.upsert_job(make_job(source="lever", external_id="y:9"))
    assert not new3                                    # cross-source duplicate
    assert d.get_job(jid3)["status"] == dbm.DUPLICATE and d.get_job(jid3)["duplicate_of"] == jid


def test_email_dedupe_and_limits(tmp_path):
    d = dbm.Database(tmp_path / "t.db")
    jid, _ = d.upsert_job(make_job())
    jid2, _ = d.upsert_job(make_job(external_id="x:2", title="CV Engineer"))
    assert d.duplicate_send_reason(jid, "hr@acme.com", "Acme", 30) is None
    eid = d.save_email(jid, "HR@acme.com", "Asha", "subj", "body", "AI_ML.pdf")
    assert d.duplicate_send_reason(jid, "hr@acme.com", "Acme", 30) is None   # drafts don't count
    d.update_email(eid, status="sent", sent_at=dbm.now())
    assert "already sent" in d.duplicate_send_reason(jid, "hr@acme.com", "Acme", 30)
    assert "already emailed" in d.duplicate_send_reason(jid2, "hr@acme.com", "acme", 30)
    assert d.duplicate_send_reason(jid2, "other@acme.com", "Acme", 30) is None
    assert d.sent_today() == 1


def test_analysis_roundtrip(tmp_path):
    d = dbm.Database(tmp_path / "t.db")
    jid, _ = d.upsert_job(make_job())
    d.save_analysis(jid, dict(score=80, matching_skills=["PyTorch"], missing_skills=["Spark"],
                              required_experience="1-3 years", experience_comparison="ok", experience_gap=False,
                              relevance="high", recommendation="apply", reasoning="r", best_resume="AI_ML.pdf"))
    a = d.get_analysis(jid)
    assert a["score"] == 80 and a["matching_skills"] == ["PyTorch"] and a["experience_gap"] is False
