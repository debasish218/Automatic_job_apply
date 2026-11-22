from __future__ import annotations

import html

from .base import JobPosting, JobSource, html_to_text


class GreenhouseSource(JobSource):
    """https://developers.greenhouse.io/job-board.html (public Job Board API)."""
    kind = "greenhouse"

    def fetch(self) -> list[JobPosting]:
        data = self._get_json(f"https://boards-api.greenhouse.io/v1/boards/{self.board}/jobs", content="true")
        out = []
        for j in data.get("jobs", []):
            out.append(JobPosting(
                source=self.kind, external_id=f"{self.board}:{j['id']}", company=self.company,
                title=j.get("title", "").strip(), location=(j.get("location") or {}).get("name", ""),
                url=j.get("absolute_url", ""), description=html_to_text(html.unescape(j.get("content") or "")),
                posted_at=j.get("updated_at")))
        return out
