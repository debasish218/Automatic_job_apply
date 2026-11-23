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
    required_max_years: float | None = Field(default=None,
                                             description="Upper end of the experience range, null if open-ended/not stated")
    entry_level: bool = Field(default=False, description="True if the job targets freshers / entry level / graduates "
                                                         "/ 0-1 year (upper end of range <= 1 year)")
    strong_employer: bool = Field(default=False, description="True only for a well-known, reputable employer likely "
                                                             "to pay well, or a JD that states a strong package")


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
- EXPERIENCE RULES (the candidate's explicit preferences):
  * Minimum of 1 year or more up to 2 years (1+, 1-3, 1-4, 1-anything, 2+, 2-4): good fit. A 2-year minimum is a
    small gap: set experience_gap=true, but it must NOT by itself make the recommendation "skip".
  * Range starting at 0 whose upper end is 2 or more years (0-2, 0-3, 0-5): good fit, no gap.
  * Entry level: freshers, graduates, "entry level", 0-1 year, or any range whose upper end is <= 1 year:
    set entry_level=true. Recommend "skip" UNLESS strong_employer is true (well-known, reputable company likely to
    pay well, or the JD states a strong compensation package). If not skipped, the score must be at most 60.
  * Minimum of 3+ years: experience_gap=true and recommend "skip".
  * Internships / trainee programmes: recommend "skip".
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
    return apply_experience_policy(result)


ENTRY_LEVEL_MAX_SCORE = 60


def apply_experience_policy(result: MatchResult) -> MatchResult:
    """Enforce the candidate's experience preferences in code (not only in the prompt)."""
    result.score = max(0, min(100, result.score))
    max_y = result.required_max_years
    min_y = result.required_min_years
    entry = result.entry_level or (max_y is not None and max_y <= 1 and (min_y or 0) <= 1)
    if entry:
        result.entry_level = True
        if not result.strong_employer:
            result.recommendation = "skip"
            result.reasoning = "Entry-level / 0-1 year role at a non-standout employer (skipped by policy). " \
                               + result.reasoning
        elif result.score > ENTRY_LEVEL_MAX_SCORE:
            result.score = ENTRY_LEVEL_MAX_SCORE
            result.reasoning = f"Entry-level role kept only because of the employer; score capped at " \
                               f"{ENTRY_LEVEL_MAX_SCORE}. " + result.reasoning
            if result.recommendation == "apply":
                result.recommendation = "consider"
    if min_y is not None and min_y > 2:
        result.recommendation = "skip"
    return result
