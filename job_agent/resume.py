"""Resume loading and Gemini-based structured analysis. The resume PDFs are the source of truth."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel
from pypdf import PdfReader

from .gemini_client import GeminiClient

log = logging.getLogger(__name__)


class Experience(BaseModel):
    company: str
    role: str
    start: str
    end: str
    highlights: list[str]


class Project(BaseModel):
    name: str
    description: str
    technologies: list[str]


class Education(BaseModel):
    institution: str
    degree: str
    period: str


class ResumeProfile(BaseModel):
    name: str
    email: str
    phone: str
    location: str
    linkedin: str
    github: str
    summary: str
    experience_stated_in_resume: str
    technical_skills: list[str]
    employment_history: list[Experience]
    projects: list[Project]
    education: list[Education]
    deployment_and_production_experience: list[str]
    certifications_and_achievements: list[str]


@dataclass
class ResumeVariant:
    key: str           # file name, e.g. "computer_vision.pdf"
    path: Path
    text: str


@dataclass
class ResumeBundle:
    profile: ResumeProfile
    variants: dict[str, ResumeVariant]

    def variant_summary(self) -> str:
        return "\n".join(f"- {k}: {v.text.split('SKILLS')[0][-600:].strip()}" for k, v in self.variants.items())


def extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    text = "\n".join(p.extract_text() or "" for p in reader.pages).strip()
    if not text:
        raise ValueError(f"No extractable text in {path.name} (scanned PDF?)")
    return text


def find_resumes(resumes_dir: Path) -> list[Path]:
    pdfs = sorted(resumes_dir.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"No resume PDFs found in {resumes_dir}")
    return pdfs


PROFILE_PROMPT = """You are analysing a candidate's resume. Several role-targeted versions of the SAME person's resume
are given below. Merge them into one factual profile.

STRICT RULES:
- Use ONLY information explicitly present in the resume text. Never invent or infer skills, employers, dates,
  metrics or tools that are not written there.
- If a field is absent, use an empty string or empty list.
- technical_skills: deduplicated list of every technical skill/tool explicitly listed.
- deployment_and_production_experience: concrete statements about deploying, serving, optimizing or running
  models/systems in production, quoted or closely paraphrased from the resume.
- experience_stated_in_resume: the total experience exactly as the resume words it.

RESUME VERSIONS:
{resumes}
"""


def load_resume_bundle(resumes_dir: Path, gemini: GeminiClient, cache_dir: Path) -> ResumeBundle:
    paths = find_resumes(resumes_dir)
    variants = {p.name: ResumeVariant(p.name, p, extract_pdf_text(p)) for p in paths}
    digest = hashlib.sha256(b"".join(p.read_bytes() for p in paths) + gemini.model.encode()).hexdigest()[:16]
    cache = cache_dir / f"resume_profile_{digest}.json"
    if cache.exists():
        log.info("Using cached resume profile %s", cache.name)
        profile = ResumeProfile.model_validate_json(cache.read_text())
    else:
        joined = "\n\n".join(f"===== {k} =====\n{v.text}" for k, v in variants.items())
        profile = gemini.generate_json(PROFILE_PROMPT.format(resumes=joined), ResumeProfile, temperature=0)
        cache.write_text(profile.model_dump_json(indent=2))
        log.info("Resume profile analysed and cached to %s", cache.name)
    return ResumeBundle(profile=profile, variants=variants)


def profile_for_prompt(profile: ResumeProfile) -> str:
    return json.dumps(profile.model_dump(), indent=1)
