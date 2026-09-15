"""Idempotent, insert-only delivery to a Notion recruiting data source."""
import hashlib
import os

import requests


def enabled():
    return bool(os.getenv("NOTION_TOKEN", "").strip() and
                os.getenv("NOTION_DATA_SOURCE_ID", "").strip())


def job_id(job):
    # A title edit must not create another application record.
    return hashlib.sha256(f"{job.company}|{job.external_id or job.url}".encode()).hexdigest()


class NotionSync:
    def __init__(self):
        self.data_source = os.environ["NOTION_DATA_SOURCE_ID"].strip()
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": "Bearer " + os.environ["NOTION_TOKEN"].strip(),
            "Notion-Version": "2025-09-03",
            "Content-Type": "application/json",
        })

    def request(self, path, body):
        # Never automatically retry a create POST: a timeout may have created it.
        # The next scan queries Notion again before attempting another insert.
        response = self.session.post("https://api.notion.com/v1/" + path,
                                     json=body, timeout=25)
        if not response.ok:
            raise RuntimeError(f"Notion HTTP {response.status_code}")
        return response.json()

    def sync(self, job, first_seen):
        filters = [{"property": "Job ID", "rich_text": {"equals": job_id(job)}}]
        if job.url:
            filters.append({"property": "Role Link", "url": {"equals": job.url}})
        found = self.request(f"data_sources/{self.data_source}/query",
                             {"filter": {"or": filters}, "page_size": 1})
        if found.get("results"):
            return found["results"][0]["id"]

        def text(value):
            return [{"text": {"content": value[:2000]}}] if value else []

        properties = {
            "Role": {"title": text(job.title)},
            "Company": {"rich_text": text(job.company)},
            "Location": {"rich_text": text(job.location)},
            "Role Link": {"url": job.url or None},
            "Date Found": {"date": {"start": first_seen}},
            "Job ID": {"rich_text": text(job_id(job))},
            "Source Posted": {"rich_text": text(job.posted_at)},
            "Status": {"select": {"name": "New"}},
        }
        result = self.request("pages", {
            "parent": {"type": "data_source_id", "data_source_id": self.data_source},
            "properties": properties,
        })
        return result["id"]
