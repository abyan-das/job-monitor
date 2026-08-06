#!/usr/bin/env python3
"""Poll company career pages and send new early-career PM roles to Discord."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_DB = ROOT / "data" / "jobs.sqlite3"
USER_AGENT = "PersonalNewGradJobMonitor/1.0 (career-page polling)"


@dataclass(frozen=True)
class Job:
    company: str
    external_id: str
    title: str
    location: str
    url: str
    description: str = ""
    posted_at: str = ""
    source: str = ""

    @property
    def key(self) -> str:
        raw = f"{self.company}|{self.external_id or self.url}|{self.title}"
        return hashlib.sha256(raw.encode()).hexdigest()


def env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class Monitor:
    def __init__(self, config_path: Path, db_path: Path):
        self.config = json.loads(config_path.read_text())
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
            job_key TEXT PRIMARY KEY, company TEXT, external_id TEXT, title TEXT,
            location TEXT, url TEXT, posted_at TEXT, first_seen TEXT, matched INTEGER,
            notified INTEGER DEFAULT 0, active INTEGER DEFAULT 1, last_seen TEXT
            , source TEXT
            )"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS source_health (
            company TEXT PRIMARY KEY, provider TEXT, last_checked TEXT,
            last_success TEXT, last_count INTEGER, consecutive_failures INTEGER DEFAULT 0,
            last_error TEXT
            )"""
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "notified" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN notified INTEGER DEFAULT 0")
        if "active" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN active INTEGER DEFAULT 1")
        if "last_seen" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN last_seen TEXT")
        if "source" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN source TEXT")
            self.db.execute("UPDATE jobs SET source=company WHERE source IS NULL")
        self.db.commit()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json,text/html"})
        retries = Retry(total=2, connect=2, read=2, backoff_factor=0.5,
                        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET", "POST"))
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        self.timeout = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "25"))

    def close(self) -> None:
        self.db.close()

    def fetch(self, company: dict[str, Any]) -> list[Job]:
        provider = company["provider"]
        if provider == "greenhouse":
            return self._greenhouse(company)
        if provider == "lever":
            return self._lever(company)
        if provider == "ashby":
            return self._ashby(company)
        if provider == "workday":
            return self._workday(company)
        return self._generic(company)

    def _greenhouse(self, c: dict[str, Any]) -> list[Job]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs?content=true"
        data = self._get_json(url)
        jobs = []
        for item in data.get("jobs", []):
            location = (item.get("location") or {}).get("name", "")
            jobs.append(Job(c["name"], str(item.get("id", "")), item.get("title", ""),
                            location, item.get("absolute_url", ""),
                            _text(item.get("content", "")), item.get("updated_at", "")))
        return jobs

    def _lever(self, c: dict[str, Any]) -> list[Job]:
        data = self._get_json(f"https://api.lever.co/v0/postings/{c['slug']}?mode=json")
        return [Job(c["name"], str(x.get("id", "")), x.get("text", ""),
                    ((x.get("categories") or {}).get("location", "")), x.get("hostedUrl", ""),
                    _text(x.get("descriptionPlain", "") + " " + x.get("additionalPlain", "")),
                    str(x.get("createdAt", ""))) for x in data]

    def _ashby(self, c: dict[str, Any]) -> list[Job]:
        data = self._get_json(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}?includeCompensation=true")
        jobs = []
        for x in data.get("jobs", []):
            jobs.append(Job(c["name"], x.get("jobUrl", ""), x.get("title", ""),
                            x.get("location", ""), x.get("applyUrl") or x.get("jobUrl", ""),
                            _text(x.get("descriptionHtml", "")), x.get("publishedAt", "")))
        return jobs

    def _workday(self, c: dict[str, Any]) -> list[Job]:
        """Read Workday's public CXS endpoint, following pagination."""
        host = c["host"].rstrip("/")
        endpoint = f"{host}/wday/cxs/{c['tenant']}/{c['site']}/jobs"
        jobs: list[Job] = []
        offset = 0
        while True:
            response = self.session.post(
                endpoint,
                json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
            postings = data.get("jobPostings", [])
            for x in postings:
                path = x.get("externalPath", "")
                jobs.append(Job(c["name"], path, x.get("title", ""),
                                x.get("locationsText", ""), f"{host}/en-US/{c['site']}{path}",
                                "", x.get("postedOn", "")))
            offset += len(postings)
            if not postings or offset >= int(data.get("total", 0)):
                break
        return jobs

    def _generic(self, c: dict[str, Any]) -> list[Job]:
        """Extract JobPosting JSON-LD and recognizable job links from a search page."""
        response = self.session.get(c["url"], timeout=self.timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        jobs: list[Job] = []
        for tag in soup.select('script[type="application/ld+json"]'):
            try:
                payload = json.loads(tag.string or "null")
            except json.JSONDecodeError:
                continue
            for x in _jsonld_jobs(payload):
                loc = _jsonld_location(x)
                url = x.get("url") or c["url"]
                jobs.append(Job(c["name"], x.get("identifier", {}).get("value", url),
                                x.get("title", ""), loc, url, _text(x.get("description", "")),
                                x.get("datePosted", "")))
        if jobs:
            return jobs
        # Fallback is intentionally conservative: only links whose visible text resembles a PM role.
        for a in soup.find_all("a", href=True):
            title = " ".join(a.get_text(" ", strip=True).split())
            if "program" in title.lower() and "manager" not in title.lower():
                continue
            if not re.search(r"\b(product manager|associate product|apm|rpm)\b", title, re.I):
                continue
            url = urljoin(c["url"], a["href"])
            company_name = c["name"]
            external_id = url
            if c.get("aggregator"):
                path = urlsplit(url).path
                match = re.search(r"-at-(.+)-(\d+)$", path, re.I)
                if not match or "/jobs/view/" not in path:
                    continue
                employer_slug, posting_id = match.groups()
                targets = {
                    re.sub(r"[^a-z0-9]+", "-", item["name"].lower()).strip("-"): item["name"]
                    for item in self.config["companies"]
                }
                company_name = targets.get(employer_slug, employer_slug.replace("-", " ").title())
                external_id = f"linkedin:{posting_id}"
            jobs.append(Job(company_name, external_id, title, "", url, source=c["name"]))
        return list({j.url: j for j in jobs}.values())

    def _get_json(self, url: str) -> Any:
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def matches(self, job: Job) -> bool:
        f = self.config["filters"]
        title = job.title.lower()
        full = f"{job.title} {job.description} {job.location}".lower()
        if any(x.lower() in title for x in f["exclude_any"]):
            return False
        # Require both PM identity and an early-career signal. APM/RPM titles imply both.
        compact = re.sub(r"[^a-z0-9]+", " ", title).strip()
        acronym = bool(re.search(r"(^| )(apm|rpm)( |$)", compact)) and any(
            phrase in full for phrase in ("associate product manager", "rotational product manager", "product management")
        )
        # Require PM language in the title; many non-PM descriptions merely mention PM partners.
        pm = acronym or any(len(x.strip()) > 3 and x.lower() in title for x in f["pm_signal_any"])
        early = acronym or any(x.lower() in full for x in f["early_career_signal_any"])
        if not (pm and early):
            return False
        locations = f.get("locations_any", [])
        return not locations or any(x.lower() in full for x in locations)

    def run(self, bootstrap: bool = False, dry_run: bool = False) -> tuple[list[Job], list[str]]:
        new_matches: list[Job] = []
        errors: list[str] = []
        now = datetime.now(timezone.utc).isoformat()
        sources = self.config["companies"] + self.config.get("discovery_sources", [])
        for company in sources:
            try:
                jobs = self.fetch(company)
                logging.info("%s: fetched %d jobs", company["name"], len(jobs))
                self._record_health(company, now, len(jobs), None)
                self.db.execute("UPDATE jobs SET active=0 WHERE source=?", (company["name"],))
            except Exception as exc:  # continue monitoring healthy sources
                msg = f"{company['name']}: {type(exc).__name__}: {exc}"
                logging.warning(msg)
                errors.append(msg)
                self._record_health(company, now, None, msg)
                continue
            for job in jobs:
                matched = self.matches(job)
                exists = self.db.execute("SELECT 1 FROM jobs WHERE job_key=?", (job.key,)).fetchone()
                if not exists:
                    self.db.execute(
                        """INSERT INTO jobs
                        (job_key,company,external_id,title,location,url,posted_at,first_seen,
                         matched,notified,active,last_seen,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (job.key, job.company, job.external_id, job.title, job.location,
                         job.url, job.posted_at, now, int(matched), int(bootstrap or not matched),
                         1, now, job.source or company["name"]),
                    )
                else:
                    self.db.execute(
                        """UPDATE jobs SET title=?, location=?, url=?, posted_at=?, matched=?,
                        active=1,last_seen=?,company=?,source=? WHERE job_key=?""",
                        (job.title, job.location, job.url, job.posted_at, int(matched), now,
                         job.company, job.source or company["name"], job.key),
                    )
        if not bootstrap:
            rows = self.db.execute(
                "SELECT company, external_id, title, location, url, posted_at FROM jobs "
                "WHERE matched=1 AND notified=0 AND active=1"
            ).fetchall()
            new_matches = [Job(*row[:5], posted_at=row[5]) for row in rows]
        if dry_run:
            self.db.rollback()
        else:
            self.db.commit()
        return new_matches, errors

    def _record_health(self, company: dict[str, Any], now: str,
                       count: Optional[int], error: Optional[str]) -> None:
        previous = self.db.execute(
            "SELECT consecutive_failures FROM source_health WHERE company=?", (company["name"],)
        ).fetchone()
        failures = (previous[0] if previous else 0) + 1 if error else 0
        self.db.execute(
            """INSERT INTO source_health
            (company,provider,last_checked,last_success,last_count,consecutive_failures,last_error)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(company) DO UPDATE SET provider=excluded.provider,
            last_checked=excluded.last_checked,
            last_success=COALESCE(excluded.last_success,source_health.last_success),
            last_count=COALESCE(excluded.last_count,source_health.last_count),
            consecutive_failures=excluded.consecutive_failures,last_error=excluded.last_error""",
            (company["name"], company["provider"], now, None if error else now,
             count, failures, error),
        )

    def health(self) -> list[tuple[Any, ...]]:
        return self.db.execute(
            "SELECT company,provider,last_count,consecutive_failures,last_error "
            "FROM source_health ORDER BY consecutive_failures DESC, company"
        ).fetchall()


def _text(value: Any) -> str:
    return BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)


def _jsonld_jobs(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [j for x in value for j in _jsonld_jobs(x)]
    if not isinstance(value, dict):
        return []
    found = [value] if value.get("@type") == "JobPosting" else []
    for key in ("@graph", "itemListElement"):
        if key in value:
            found.extend(_jsonld_jobs(value[key]))
    return found


def _jsonld_location(x: dict[str, Any]) -> str:
    locations = x.get("jobLocation", [])
    if isinstance(locations, dict):
        locations = [locations]
    parts = []
    for loc in locations:
        address = loc.get("address", {}) if isinstance(loc, dict) else {}
        parts.append(", ".join(str(address.get(k, "")) for k in
                               ("addressLocality", "addressRegion", "addressCountry") if address.get(k)))
    return "; ".join(filter(None, parts)) or x.get("jobLocationType", "")


def _discord_payloads(jobs: list[Job]) -> list[dict[str, Any]]:
    payloads = []
    for start in range(0, len(jobs), 10):
        embeds = []
        for job in jobs[start:start + 10]:
            details = []
            if job.location:
                details.append(f"📍 {job.location}")
            if job.posted_at:
                details.append(f"Posted: {job.posted_at}")
            embeds.append({
                "author": {"name": job.company[:256]},
                "title": job.title[:256],
                "url": job.url,
                "description": "\n".join(details)[:4096] or "New early-career PM role",
                "fields": [{"name": "Company", "value": job.company[:1024], "inline": True}],
                "color": 0x5865F2,
            })
        payloads.append({
            "content": "🚨 **New early-career PM role detected**",
            "username": "New Grad PM Monitor",
            "allowed_mentions": {"parse": []},
            "embeds": embeds,
        })
    return payloads


def send_discord(jobs: list[Job]) -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise RuntimeError("Missing Discord setting: DISCORD_WEBHOOK_URL")
    if not re.match(r"^https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/", webhook_url):
        raise RuntimeError("DISCORD_WEBHOOK_URL does not look like a Discord webhook URL")
    session = requests.Session()
    retries = Retry(total=4, connect=3, read=3, backoff_factor=1,
                    status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("POST",),
                    respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retries))
    for payload in _discord_payloads(jobs):
        response = session.post(webhook_url, params={"wait": "true"}, json=payload, timeout=25)
        response.raise_for_status()


def mark_notified(db_path: Path, jobs: list[Job]) -> None:
    with sqlite3.connect(db_path) as db:
        db.executemany("UPDATE jobs SET notified=1 WHERE job_key=?", [(job.key,) for job in jobs])


def run_state_file(monitor: Monitor, state_path: Path, bootstrap: bool = False) -> tuple[list[Job], list[str]]:
    """Stateless-runner mode: persist only matching IDs in a small JSON file."""
    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        state = {"version": 1, "seen": {}}
    seen = state.setdefault("seen", {})
    matches: dict[str, Job] = {}
    errors = []
    sources = monitor.config["companies"] + monitor.config.get("discovery_sources", [])
    for company in sources:
        try:
            jobs = monitor.fetch(company)
            logging.info("%s: fetched %d jobs", company["name"], len(jobs))
        except Exception as exc:
            msg = f"{company['name']}: {type(exc).__name__}: {exc}"
            logging.warning(msg)
            errors.append(msg)
            continue
        for job in jobs:
            if monitor.matches(job):
                matches[job.key] = job
    new_jobs = [job for key, job in matches.items() if key not in seen]
    if new_jobs and not bootstrap:
        send_discord(new_jobs)
    now = datetime.now(timezone.utc).isoformat()
    for key, job in matches.items():
        if key not in seen:
            seen[key] = {"company": job.company, "title": job.title, "url": job.url, "first_seen": now}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return ([] if bootstrap else new_jobs), errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--bootstrap", action="store_true", help="save current jobs without alerting")
    parser.add_argument("--dry-run", action="store_true", help="print matches without saving or notifying")
    parser.add_argument("--loop", action="store_true", help="poll continuously")
    parser.add_argument("--status", action="store_true", help="show source health without polling")
    parser.add_argument("--test-notification", action="store_true", help="send a Discord test alert")
    parser.add_argument("--state-file", type=Path,
                        help="use compact JSON state for scheduled CI runners")
    args = parser.parse_args()
    env_file(ROOT / ".env")
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.test_notification:
        send_discord([Job("Test Company", "test", "Associate Product Manager, New Grad",
                          "San Francisco, CA", "https://example.com/jobs/test",
                          posted_at=datetime.now(timezone.utc).isoformat())])
        print("Discord test notification sent successfully.")
        return 0
    if args.status:
        monitor = Monitor(args.config, args.db)
        try:
            for company, provider, count, failures, error in monitor.health():
                state = "FAIL" if failures else ("EMPTY" if count == 0 else "OK")
                print(f"{state:5} {company:24} {provider:10} jobs={count!s:5} failures={failures}")
                if error:
                    print(f"      {error}")
        finally:
            monitor.close()
        return 0
    if args.state_file:
        monitor = Monitor(args.config, args.db)
        try:
            jobs, errors = run_state_file(monitor, args.state_file, bootstrap=args.bootstrap)
        finally:
            monitor.close()
        logging.info("State run complete: %d new role(s), %d source error(s)", len(jobs), len(errors))
        return 0
    while True:
        monitor = Monitor(args.config, args.db)
        try:
            jobs, errors = monitor.run(bootstrap=args.bootstrap, dry_run=args.dry_run)
        finally:
            monitor.close()
        for job in jobs:
            print(f"MATCH: {job.company} | {job.title} | {job.location} | {job.url}")
        if jobs and not args.dry_run and not args.bootstrap:
            send_discord(jobs)
            mark_notified(args.db, jobs)
            logging.info("Sent Discord notification with %d role(s)", len(jobs))
        if errors:
            logging.info("Completed with %d source error(s)", len(errors))
        if not args.loop or args.bootstrap or args.dry_run:
            return 0
        time.sleep(max(60, int(os.getenv("POLL_SECONDS", "120"))))


if __name__ == "__main__":
    sys.exit(main())
