from __future__ import annotations

from .base import JobPosting, JobSource, html_to_text


class SmartRecruitersSource(JobSource):
    """https://developers.smartrecruiters.com/docs/posting-api (public Posting API). India postings only."""
    kind = "smartrecruiters"

    def fetch(self) -> list[JobPosting]:
        out, offset = [], 0
        while True:
            data = self._get_json(f"https://api.smartrecruiters.com/v1/companies/{self.board}/postings",
                                  country="in", limit=100, offset=offset)
            for j in data.get("content", []):
                loc = j.get("location") or {}
                out.append(JobPosting(
                    source=self.kind, external_id=f"{self.board}:{j['id']}", company=self.company,
                    title=j.get("name", "").strip(),
                    location=", ".join(filter(None, [loc.get("city"), loc.get("region"), "India"])),
                    url=f"https://jobs.smartrecruiters.com/{self.board}/{j['id']}",
                    posted_at=j.get("releasedDate")))
            offset += 100
            if offset >= data.get("totalFound", 0) or not data.get("content"):
                return out

    def fetch_description(self, posting: JobPosting) -> str:
        if posting.description:
            return posting.description
        pid = posting.external_id.split(":", 1)[1]
        d = self._get_json(f"https://api.smartrecruiters.com/v1/companies/{self.board}/postings/{pid}")
        sections = (d.get("jobAd") or {}).get("sections") or {}
        parts = [f"{s.get('title', '')}\n{html_to_text(s.get('text', ''))}" for s in sections.values()
                 if isinstance(s, dict)]
        posting.description = "\n\n".join(parts)
        return posting.description
