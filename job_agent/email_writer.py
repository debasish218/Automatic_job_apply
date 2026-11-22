"""Gemini-generated, personalised application emails."""
from __future__ import annotations

from pydantic import BaseModel

from .gemini_client import GeminiClient
from .matcher import MatchResult
from .resume import ResumeBundle, profile_for_prompt


class EmailDraft(BaseModel):
    subject: str
    body: str


EMAIL_PROMPT = """Write a job application email from the candidate to a recruiter.

CANDIDATE PROFILE (source of truth; mention only things in here):
{profile}

RESUME BEING ATTACHED ({resume_key}):
{resume_text}

JOB: "{title}" at {company} ({location})
JOB DESCRIPTION (excerpt):
{description}

MATCH ANALYSIS:
- Matching skills: {matching}
- Missing skills: {missing}
- Required experience: {required}
- Experience gap: {gap}
- Verified facts about the company/team from the JD: {highlights}

RECIPIENT: {greeting_name}

REQUIREMENTS
- Subject: include the exact job title "{title}" and the candidate's name; concise.
- Mention the exact job title in the first paragraph.
- Reference the company/team ONLY using the verified facts listed above (skip this if none). No outside claims.
- Highlight 2-3 concrete, matching projects/achievements from the resume, emphasising practical project and
  production deployment experience (e.g. optimisation, serving, MLOps) relevant to this JD.
- State the candidate's professional experience as exactly "{experience}" of professional experience in AI/ML
  engineering (do not use any other figure).
{gap_instruction}
- Mention the resume is attached.
- End with a clear, polite request for an interview / a short call.
- Tone: confident, specific, human. 150-230 words. Plain text, no markdown, no emojis, no placeholders in brackets.
- Avoid clichés: "I am writing to express", "passionate", "excited to apply", "perfect fit", "leverage",
  "synergy", "dynamic", "thrilled", "delve".
- Never invent skills, employers, metrics or experience not in the profile/resume.
- Sign-off: "Best regards," then on separate lines: {name}, {phone}, {email}, {linkedin}, {github}.
"""

GAP_TEXT = (
    "- The JD asks for more experience than the candidate has. Address it honestly in one or two sentences, adapted "
    "to this specific role, conveying: \"I have {experience} of professional experience in AI/ML engineering. "
    "Although I am slightly below the stated experience requirement, I have strong hands-on knowledge and practical "
    "experience in developing, optimizing, and deploying machine learning solutions.\" Tie it to the JD's key "
    "requirements. Do not apologise excessively."
)


def write_email(gemini: GeminiClient, resume: ResumeBundle, job: dict, match: MatchResult, experience: str,
                recruiter_name: str | None) -> EmailDraft:
    p = resume.profile
    variant = resume.variants[match.best_resume]
    prompt = EMAIL_PROMPT.format(
        profile=profile_for_prompt(p), resume_key=variant.key, resume_text=variant.text[:6000],
        title=job["title"], company=job["company"], location=job.get("location", ""),
        description=(job.get("description") or "")[:5000],
        matching=", ".join(match.matching_skills) or "none", missing=", ".join(match.missing_skills) or "none",
        required=match.required_experience, gap="yes" if match.experience_gap else "no",
        highlights="; ".join(match.company_highlights) or "none",
        greeting_name=recruiter_name or "unknown (greet as 'Dear Hiring Team')",
        experience=experience,
        gap_instruction=GAP_TEXT.format(experience=experience) if match.experience_gap else
        "- No experience gap; do not mention any gap.",
        name=p.name, phone=p.phone, email=p.email, linkedin=p.linkedin, github=p.github)
    draft = gemini.generate_json(prompt, EmailDraft, temperature=0.7)
    draft.subject = draft.subject.strip()
    draft.body = draft.body.strip()
    return draft
