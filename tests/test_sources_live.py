"""Live checks against public ATS APIs. Run with: pytest -m live"""
import pytest

from job_agent.config import load_settings
from job_agent.sources import load_sources
from job_agent.sources.base import make_session

pytestmark = pytest.mark.live


@pytest.mark.parametrize("kind", ["greenhouse", "lever", "ashby", "smartrecruiters"])
def test_each_source_kind_returns_jobs(kind):
    s = load_settings()
    src = next(x for x in load_sources(s.companies_file, make_session(s.http_user_agent)) if x.kind == kind)
    jobs = src.fetch()
    assert jobs, f"{src} returned no jobs"
    j = jobs[0]
    assert j.title and j.url and j.external_id.startswith(src.board)
    assert src.fetch_description(j), "description should be non-empty"
