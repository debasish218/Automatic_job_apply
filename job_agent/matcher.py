"""Gemini-based job/resume matching and ranking."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .gemini_client import GeminiClient
from .resume import ResumeBundle, profile_for_prompt


class MatchResult(BaseModel):
    score: int = Field(description="Overall fit 0-100")
    matching_skills: list[str]
    missing_skills: list[str]
    required_experience: str = Field(description="Experience requirement as stated in the JD, or 'Not stated'")
    required_min_years: float | None = Field(description="Minimum years required, null if not stated")
    experience_comparison: str
    experience_gap: bool = Field(description="True if the JD minimum exceeds the candidate's experience")
    relevance: Literal["high", "medium", "low"]
    recommendation: Literal["apply", "consider", "skip"]
    reasoning: str
    best_resume: str = Field(description="File name of the best-fitting resume version")
    company_highlights: list[str] = Field(
        description="Specific product/team/work facts about the company or team stated in the JD (verbatim facts only)")


MATCH_PROMPT = """You are a precise technical recruiter evaluating fit between a candidate and a job.

CANDIDATE
- Actual professional experience: {experience} ({months} months) in AI/ML engineering.
- Target roles: {roles}
- Structured resume profile (source of truth, do not add anything not in it):
{profile}

AVAILABLE RESUME VERSIONS (choose the best one for this job; return its exact file name):
{variants}

JOB
Company: {company}
Title: {title}
Location: {location}
Description:
{description}

SCORING RULES
- The candidate's experience is exactly {experience} ({months} months). Always use this figure in
  experience_comparison and reasoning; ignore any other experience figure written in the resume text.
- Score 0-100 on: core technical skill overlap (50%), role/domain relevance to the target roles (30%),
  experience fit (20%).
- Jobs requiring up to 2 years (incl. exactly 2 years, 0-2, 1-2, 1-3) are a reasonable fit; a 2-year minimum is a
  small gap (set experience_gap=true) but must NOT by itself make the recommendation "skip".
- Minimum of 3+ years: experience_gap=true and recommend "skip" unless the fit is exceptional.
- matching_skills: only skills that appear in BOTH the JD and the resume profile.
- missing_skills: important JD skills absent from the resume profile.
- company_highlights: facts about the company/team/product that are explicitly written in the JD (max 3). No outside
  knowledge.
- recommendation: "apply" (score>=70, no major gaps), "consider" (50-69 or notable gaps), "skip" otherwise.
"""


def analyse_job(gemini: GeminiClient, resume: ResumeBundle, job: dict, experience: str, months: int,
                roles: list[str]) -> MatchResult:
    prompt = MATCH_PROMPT.format(
        experience=experience, months=months, roles=", ".join(roles),
        profile=profile_for_prompt(resume.profile), variants=resume.variant_summary(),
        company=job["company"], title=job["title"], location=job.get("location", ""),
        description=(job.get("description") or "")[:12000])
    result = gemini.generate_json(prompt, MatchResult)
    if result.best_resume not in resume.variants:          # guard against hallucinated filenames
        result.best_resume = next(iter(resume.variants))
    result.score = max(0, min(100, result.score))
    return result
