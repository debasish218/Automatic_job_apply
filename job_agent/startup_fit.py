"""Gemini scoring of a funded startup against the resume, and the cold outreach email."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .email_writer import EmailDraft, EmailParts, _clean, _slug, assemble, safe_filename
from .gemini_client import GeminiClient
from .resume import ResumeBundle, profile_for_prompt
from .search.funding import NON_INDIAN_PENALTY


class StartupFit(BaseModel):
    score: int = Field(description="Overall fit 0-100")
    matching_skills: list[str] = Field(description="Resume skills directly useful for what this startup builds")
    relevance: Literal["high", "medium", "low"]
    recommendation: Literal["reach_out", "consider", "skip"]
    reasoning: str
    best_resume: str = Field(description="File name of the best-fitting resume version")
    likely_roles: list[str] = Field(description="AI/ML roles the candidate could fill there (e.g. 'ML Engineer')")
    hiring_signals: list[str] = Field(description="Facts from the source suggesting they will hire (e.g. 'funds will be "
                                                  "used to expand the engineering team'); empty if none")
    talking_points: list[str] = Field(description="1-3 specific facts about the startup from the source, usable in an "
                                                  "email (product, round, lead investor)")


FIT_PROMPT = """You evaluate whether a candidate should cold-email a recently funded startup about AI/ML engineering work.

CANDIDATE
- Actual professional experience: {experience} in AI/ML engineering (use exactly this figure).
- Target roles: {roles}
- Structured resume profile (source of truth, do not add anything not in it):
{profile}

AVAILABLE RESUME VERSIONS (return the exact file name of the best one):
{variants}

STARTUP (facts extracted from funding news; use only these)
Name: {name}
What they build: {description}
Sector: {sector}
Headquarters: {hq}
India presence: {india_presence}
Hiring from India evidence: {india_hiring}
Round: {round} · Amount: {amount} · Date: {date}
Investors: {investors}
Founders: {founders}
Use of funds: {use_of_funds}
News evidence: {evidence}

SCORING RULES
- Score 0-100: technical overlap between what the startup builds and the candidate's skills/projects (50%),
  relevance to the target roles (30%), likelihood they would hire an engineer with {experience} in India soon
  (20%: hiring plans in the use of funds, team/office in India, AI-heavy product).
- An early-stage startup building AI as its core product with an India team is a strong prospect; a company where AI
  is a minor feature, or whose work does not use the candidate's skills, is a weak prospect.
- matching_skills: only skills from the resume profile that are clearly relevant to the startup's product.
- Never invent facts about the startup. talking_points must come from the facts above.
- recommendation: "reach_out" (score>=70), "consider" (50-69), "skip" otherwise.
"""


def score_startup(gemini: GeminiClient, resume: ResumeBundle, s: dict, experience: str,
                  roles: list[str]) -> StartupFit:
    prompt = FIT_PROMPT.format(
        experience=experience, roles=", ".join(roles), profile=profile_for_prompt(resume.profile),
        variants=resume.variant_summary(), name=s["name"], description=s.get("description") or "not stated",
        sector=s.get("sector") or "not stated",
        hq=", ".join(x for x in (s.get("hq_city"), s.get("hq_country")) if x) or "not stated",
        india_presence=s.get("india_presence") or "not stated",
        india_hiring=s.get("india_hiring_evidence") or "not stated",
        round=s.get("funding_round") or "?", amount=s.get("amount") or "not stated", date=s.get("funding_date") or "?",
        investors=", ".join(s.get("investors") or []) or "not stated",
        founders=", ".join(s.get("founders") or []) or "not stated",
        use_of_funds=s.get("use_of_funds") or "not stated", evidence=s.get("evidence") or "")
    fit = gemini.generate_json(prompt, StartupFit)
    if fit.best_resume not in resume.variants:
        fit.best_resume = next(iter(resume.variants))
    return apply_startup_policy(fit, s.get("region"))


def apply_startup_policy(fit: StartupFit, region: str | None) -> StartupFit:
    """Indian companies are preferred: others lose NON_INDIAN_PENALTY points. Recommendation follows the score."""
    fit.score = max(0, min(100, fit.score))
    if region and region != "india":
        fit.score = max(0, fit.score - NON_INDIAN_PENALTY)
        fit.reasoning = f"Not headquartered in India (−{NON_INDIAN_PENALTY} by preference). " + fit.reasoning
    if fit.score < 50:
        fit.recommendation = "skip"
    elif fit.score < 70 and fit.recommendation == "reach_out":
        fit.recommendation = "consider"
    return fit


OUTREACH_PROMPT = """You are an experienced technical recruiter and career-communication specialist.
Write a short cold email from the candidate to a recently funded startup, asking about AI/ML engineering roles.
There is no specific job posting.

OBJECTIVE: within 15-20 seconds the reader must understand who the candidate is, why their skills fit what this
startup builds, and why the attached resume is worth opening.

CANDIDATE PROFILE (source of truth):
{profile}

RESUME BEING ATTACHED ({resume_key}):
{resume_text}

STARTUP: {name} — {description}
Facts you may mention (from funding news): {talking_points}
Candidate skills relevant to them: {matching}
Roles the candidate could fill: {roles}

RECIPIENT: {greeting_name}

STRUCTURE (return each part separately):
- subject: specific and professional, e.g. "AI/ML Engineer interested in joining {name} — <candidate name>".
- greeting: if a person's name is given, their first name only ("Dear Priya,"); otherwise "Dear {name} Team,".
- paragraphs (3-4 items):
  1. Opening: one line noting their recent funding round or product (a fact above), then who the candidate is,
     stating the experience as exactly "{experience}" of professional experience in AI/ML engineering.
  2. Technical relevance: 3-5 of the candidate's skills that matter for what they build, in a sentence or two.
  3. Project highlights: 1-2 relevant projects/achievements with outcomes the resume supports.
  4. Closing: ask whether they are hiring AI/ML engineers (or will be as they grow), mention the resume is
     attached, and invite a short call.
- sign_off: "Best regards,"
- attachment_filename: e.g. "{name_slug}_AI_ML_Engineer_Resume.pdf"

WRITING RULES:
- 110-170 words across all paragraphs. Every sentence must add value.
- Natural, confident, human; no flattery, exaggeration or desperation. Congratulate in at most a few words.
- Never invent experience, skills, achievements, metrics, or facts about the startup.
- Describe each achievement exactly as the resume supports it: do not attach tools, methods or causes to a result
  unless the resume explicitly connects them.
- No salary, notice period or availability. No markdown, no emojis, no placeholders, no signature block.
- Avoid clichés: "I am writing to express", "passionate", "excited to", "perfect fit", "leverage", "synergy",
  "dynamic", "thrilled", "delve", "game-changing", "revolutionize", "I believe I would be a great asset".
"""


def write_outreach(gemini: GeminiClient, resume: ResumeBundle, s: dict, fit: StartupFit, experience: str,
                   recipient_name: str | None) -> EmailDraft:
    p = resume.profile
    variant = resume.variants[fit.best_resume]
    prompt = OUTREACH_PROMPT.format(
        profile=profile_for_prompt(p), resume_key=variant.key, resume_text=variant.text[:6000], name=s["name"],
        description=s.get("description") or s.get("sector") or "",
        talking_points="; ".join(fit.talking_points) or f"{s.get('funding_round')} round",
        matching=", ".join(fit.matching_skills) or "none", roles=", ".join(fit.likely_roles) or "AI/ML Engineer",
        greeting_name=recipient_name or f"unknown (use 'Dear {s['name']} Team,')", experience=experience,
        name_slug=_slug(p.name))
    parts: EmailParts = gemini.generate_json(prompt, EmailParts, temperature=0.6)
    if not _clean(parts.greeting):
        parts.greeting = f"Dear {s['name']} Team,"
    return EmailDraft(subject=_clean(parts.subject), body=assemble(parts, p),
                      attachment_filename=safe_filename(parts.attachment_filename, p.name, "AI_ML_Engineer"))
