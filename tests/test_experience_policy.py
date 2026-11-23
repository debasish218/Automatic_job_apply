from job_agent.matcher import MatchResult, apply_experience_policy


def mk(**kw):
    base = dict(score=90, matching_skills=[], missing_skills=[], required_experience="", required_min_years=None,
                experience_comparison="", experience_gap=False, relevance="high", recommendation="apply",
                reasoning="r", best_resume="AI_ML.pdf", company_highlights=[], required_max_years=None,
                entry_level=False, strong_employer=False)
    base.update(kw)
    return apply_experience_policy(MatchResult(**base))


def test_one_plus_and_zero_to_two_plus_are_fine():
    for lo, hi in [(1, 3), (1, 4), (1, None), (0, 2), (0, 3), (0, 5), (2, 4), (2, None)]:
        r = mk(required_min_years=lo, required_max_years=hi)
        assert r.recommendation == "apply" and r.score == 90, (lo, hi)


def test_zero_to_one_skipped_unless_strong_employer():
    r = mk(required_min_years=0, required_max_years=1)
    assert r.recommendation == "skip" and r.entry_level
    r = mk(entry_level=True)                           # "entry level" with no numbers
    assert r.recommendation == "skip"
    r = mk(required_min_years=0, required_max_years=1, strong_employer=True)
    assert r.recommendation == "consider" and r.score == 60


def test_three_plus_skipped():
    assert mk(required_min_years=3).recommendation == "skip"
