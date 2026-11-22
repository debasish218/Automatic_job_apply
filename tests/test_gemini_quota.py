from job_agent.gemini_client import _parse_quota

REAL_429 = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota... \\n* Quota "
            "exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, "
            "model: gemini-3.6-flash, description: ...\\nPlease retry in 17h4m47.212786985s.', 'details': "
            "[{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '61487s'}]}}")


def test_parse_daily_quota():
    model, limit, retry = _parse_quota(REAL_429)
    assert model == "gemini-3.6-flash" and limit == 20 and retry == 61487


def test_parse_zero_quota_and_text_retry():
    model, limit, retry = _parse_quota("limit: 0, model: gemini-4-argon, ... Please retry in 2m5s.")
    assert model == "gemini-4-argon" and limit == 0 and retry == 125


def test_parse_unknown():
    assert _parse_quota("You exceeded your current quota") == (None, None, None)
