from __future__ import annotations

from .base import JobPosting, JobSource, html_to_text


class LeverSource(JobSource):
    """https://github.com/lever/postings-api (public Postings API)."""
    kind = "lever"

    def fetch(self) -> list[JobPosting]:
        data = self._get_json(f"https://api.lever.co/v0/postings/{self.board}", mode="json")
        out = []
        for j in data:
            cats = j.get("categories") or {}
            locs = cats.get("allLocations") or [cats.get("location", "")]
            parts = [j.get("descriptionPlain") or html_to_text(j.get("description", ""))]
            for lst in j.get("lists", []):
                parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content', ''))}")
            parts.append(j.get("additionalPlain") or "")
            out.append(JobPosting(
                source=self.kind, external_id=f"{self.board}:{j['id']}", company=self.company,
                title=j.get("text", "").strip(), location=", ".join(filter(None, locs)),
                url=j.get("hostedUrl", ""), description="\n\n".join(p for p in parts if p),
                posted_at=str(j.get("createdAt", ""))))
        return out
