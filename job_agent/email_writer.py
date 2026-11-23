"""Gemini-generated, personalised application emails.

Gemini returns the email as structured parts (greeting, paragraphs, sign-off); the final text is assembled
here, so line breaks and the signature layout are always correct regardless of how the model formats text.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .gemini_client import GeminiClient
from .matcher import MatchResult
from .resume import ResumeBundle, profile_for_prompt


class EmailParts(BaseModel):
    subject: str = Field(description="Clear, specific subject containing the exact job title and candidate name")
    greeting: str = Field(description="e.g. 'Dear Priya,' or 'Dear Hiring Team,' — greeting line only")
    paragraphs: list[str] = Field(description="3-4 short paragraphs, each a separate item, no greeting/sign-off")
    sign_off: str = Field(description="e.g. 'Best regards,'")
    attachment_filename: str = Field(description="Professional resume filename, e.g. "
                                                 "'Firstname_Lastname_ML_Engineer_Resume.pdf'")


class EmailDraft(BaseModel):
    subject: str
    body: str
    attachment_filename: str


EMAIL_PROMPT = """You are an experienced technical recruiter and professional career-communication specialist.
Write the job application email the candidate will send to a recruiter.

OBJECTIVE: within 15-20 seconds the recruiter must understand who the candidate is, what relevant experience
they bring, why the profile matches this position, and why the attached resume is worth opening.

CANDIDATE PROFILE (source of truth):
{profile}

RESUME BEING ATTACHED ({resume_key}):
{resume_text}

JOB: "{title}" at {company} ({location})
JOB DESCRIPTION (excerpt):
{description}

MATCH ANALYSIS:
- Skills in both the JD and the resume: {matching}
- JD skills NOT in the resume (never claim these): {missing}
- Required experience: {required}
- Experience gap: {gap}
- Facts about the company/team stated in the JD: {highlights}

RECIPIENT: {greeting_name}

STRUCTURE (return each part separately):
- subject: professional and specific to the exact job title "{title}"; include the candidate's name.
- greeting: if a recruiter name is given, use their first name only ("Dear Craig,"); otherwise "Dear Hiring Team,".
- paragraphs (3-4 items):
  1. Opening: the exact position being applied for + a one-line professional introduction, stating the
     experience as exactly "{experience}" of professional experience in AI/ML engineering (no other figure).
  2. Technical relevance: 3-5 of the most important skills that directly match the JD, woven into a sentence or two
     (no bullet list, no explanations of what the technologies are).
  3. Project highlights: 1-2 of the most relevant projects/achievements, focused on impact and outcomes that the
     resume supports (e.g. metrics stated in the resume).
  4. Closing: genuine interest, the resume is attached, and an invitation to discuss the application / a short call.
{gap_instruction}
- sign_off: "Best regards,"
- attachment_filename: e.g. "{name_slug}_{title_slug}_Resume.pdf"

WRITING RULES:
- 120-180 words across all paragraphs. Every sentence must add value.
- Natural, confident, human; no exaggeration, flattery or desperation; no unnecessary adjectives.
- Prioritise the strongest matching experience; customise to this JD.
- Do not copy whole sentences from the resume and do not retell the resume as prose.
- Never invent experience, skills, achievements, responsibilities, metrics or qualifications.
- Describe each achievement exactly as the resume supports it: do not attach tools, methods or causes to a result
  unless the resume explicitly connects them (e.g. if the resume lists TensorRT as a skill but the latency result
  does not mention it, do not say the result was achieved with TensorRT).
- Mention the company only through the JD facts above (or not at all).
- No salary, notice period or availability. No markdown, no emojis, no bracketed placeholders, no signature
  block (it is added automatically).
- Avoid clichés: "I am writing to express", "I am writing to apply", "passionate", "excited to apply", "perfect fit", "leverage",
  "synergy", "dynamic", "thrilled", "delve", "I believe I would be a great asset".
"""

GAP_TEXT = (
    "  The JD asks for more experience than the candidate has: in paragraph 1 or 2, honestly address it in one "
    "sentence adapted to this role, conveying: \"Although I am slightly below the stated experience requirement, I "
    "have strong hands-on knowledge and practical experience in developing, optimizing, and deploying machine "
    "learning solutions.\" Do not apologise."
)


def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]+", "_", text)).strip("_")


def _clean(text: str) -> str:
    """One paragraph: collapse stray line breaks/spaces inside it."""
    return re.sub(r"\s+", " ", text or "").strip()


def signature(profile) -> str:
    lines = [profile.name, profile.phone, profile.email, profile.linkedin, profile.github]
    return "\n".join(x.strip() for x in lines if x and x.strip())


def assemble(parts: EmailParts, profile) -> str:
    paragraphs = [_clean(p) for p in parts.paragraphs if _clean(p)]
    greeting = _clean(parts.greeting) or "Dear Hiring Team,"
    if not greeting.endswith((",", ":")):
        greeting += ","
    sign_off = _clean(parts.sign_off) or "Best regards,"
    return f"{greeting}\n\n" + "\n\n".join(paragraphs) + f"\n\n{sign_off}\n{signature(profile)}"


def safe_filename(name: str, profile_name: str, title: str) -> str:
    stem = _slug(re.sub(r"\.pdf$", "", name or "", flags=re.I))
    if len(stem) < 8:
        stem = f"{_slug(profile_name)}_{_slug(title)[:40]}_Resume"
    return f"{stem[:90]}.pdf"


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
        greeting_name=recruiter_name or "unknown (use 'Dear Hiring Team,')",
        experience=experience,
        gap_instruction=GAP_TEXT if match.experience_gap else "  No experience gap: do not mention any gap.",
        name_slug=_slug(p.name), title_slug=_slug(job["title"])[:40])
    parts = gemini.generate_json(prompt, EmailParts, temperature=0.6)
    return EmailDraft(subject=_clean(parts.subject), body=assemble(parts, p),
                      attachment_filename=safe_filename(parts.attachment_filename, p.name, job["title"]))
