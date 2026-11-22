from __future__ import annotations

from .base import JobPosting, JobSource, html_to_text


class AshbySource(JobSource):
    """https://developers.ashbyhq.com/docs/public-job-posting-api"""
    kind = "ashby"

    def fetch(self) -> list[JobPosting]:
        data = self._get_json(f"https://api.ashbyhq.com/posting-api/job-board/{self.board}")
        out = []
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", [])]
            out.append(JobPosting(
                source=self.kind, external_id=f"{self.board}:{j['id']}", company=self.company,
                title=j.get("title", "").strip(), location=", ".join(filter(None, locs)),
                url=j.get("jobUrl", ""),
                description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
                posted_at=j.get("publishedAt")))
        return out
