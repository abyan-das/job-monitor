# New-grad PM job monitor

Polls a curated 103-company watchlist, identifies early-career product-management roles, remembers every job it has seen, and sends newly discovered matches to Discord. Official company career pages and their publishing backends (Greenhouse, Lever, Ashby, and Workday) are checked directly. Aggregator discovery is disabled by default.

## Set up on your Mac

Requires Python 3.10+.

```bash
cd /Users/abyandas/job-monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

In Discord, open the destination channel's settings, choose **Integrations → Webhooks → New Webhook**, and copy its URL. Edit `.env` and place that URL in `DISCORD_WEBHOOK_URL`. Treat the webhook URL like a password and never commit or share it.

Send a test alert:

```bash
.venv/bin/python job_monitor.py --test-notification
```

Initialize the database with all currently visible roles. This is important: it prevents the first live run from reporting old listings as new.

```bash
.venv/bin/python job_monitor.py --bootstrap
```

Preview matching without saving or notifying Discord:

```bash
.venv/bin/python job_monitor.py --dry-run
```

Start continuous monitoring (default: every two minutes):

```bash
.venv/bin/python job_monitor.py --loop
```

Inspect source health at any time:

```bash
.venv/bin/python job_monitor.py --status
```

The included `com.abyandas.job-monitor.plist` can run the process under macOS `launchd`. Install it only after `.env` is configured and a manual run succeeds:

```bash
cp com.abyandas.job-monitor.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.abyandas.job-monitor.plist
```

Keep the process running with `launchd`, a small always-on server, or a process manager. A laptop can only monitor while it is awake and connected to the internet.

## Matching behavior

A listing must contain both:

1. a PM signal such as `Product Manager`, `Associate Product`, `APM`, or `RPM`; and
2. an early-career signal such as `new grad`, `university grad`, `early career`, or `0-2 years`.

`APM` and `RPM` titles satisfy both. Senior, staff, director, product-marketing, program-management, and project-management roles are excluded. Edit `config.json` to change these rules or add locations.

## Source health

Greenhouse, Lever, Ashby, and Workday sources use the public endpoints powering each company's official job board. They are publishing infrastructure, not delayed reposting aggregators. Companies with custom career sites are read directly using conservative HTML/structured-data extraction and may occasionally block automated requests. LinkedIn discovery is retained as an optional backup and runs only when `ENABLE_DISCOVERY_SOURCES=true`. Every local database scan stores source health, including the last job count and consecutive failures. `--status` labels feeds as `OK`, `EMPTY`, or `FAIL`.

## Discord safety

The monitor stores the webhook URL only in `.env`, which is gitignored. Alerts contain application-link cards and are split into batches of at most 10 embeds, matching Discord's webhook limit. Mentions are disabled. A match is marked as notified only after Discord accepts every batch, so a failed notification is retried on the next cycle.

## Direct Notion recruiting sync

New matching jobs are also inserted into Recruiting Tracker when `NOTION_TOKEN` and
`NOTION_DATA_SOURCE_ID` are configured. Add `NOTION_TOKEN` as a GitHub Actions secret
for the cloud runner. Use a Notion internal connection with read and insert content
access, shared only with Recruiting Tracker. Never commit the token.

The tracker needs Role (title), Company, Location, Job ID, Source Posted, Next Step
(rich text), Role Link (URL), Date Found (date), and Status (select with New).
Source Posted preserves the source's original label; some sources return an update
time rather than a publication date. It is never used as an application deadline.

Notion inserts are deduplicated using the source job ID or exact application URL.
Existing rows are left unchanged, including user-managed application status.
Discord and Notion have separate pending queues; a failed service is retried on
the next scan without repeating the successful service. Pending Notion deliveries
are retained even if the listing disappears. Old baseline jobs are not backfilled.
The first scan after deployment starts collecting new jobs for Notion, even while
the token is being configured. Bootstrap and dry-run do not send Notion entries.

Only one live runner should target this tracker. Notion has no unique constraint,
so parallel independent runners could race between the lookup and insert. The
GitHub workflow serializes its runs. A create timeout is resolved by a fresh lookup
on the next scan, rather than automatically retrying the insert.

Run regression tests with `python -m unittest -v`.

## Canvas assignments → Notion

`canvas_sync.py` syncs the configured Canvas semester into a Notion Assignments data source.
It imports upcoming and undated, unsubmitted assignments, updates names/deadlines/Canvas links,
and preserves Done, course relations, and manually selected types on existing rows.
Canvas IDs and links prevent repeat inserts. It never writes assignment payloads to repository
state, artifacts, or logs. Logs contain aggregate counts and sanitized failures only.

The `canvas-sync.yml` manual workflow requires `CANVAS_TOKEN` and the existing `NOTION_TOKEN`.
The Notion connection must have read, insert, and update access to Assignments. Use the
`dry_run` input before the initial sync and `verify_replay` to check a second pass creates no
extra rows. Schedules are enabled only after the live test succeeds. Canvas personal tokens
expire in at most 90 days; the current setup expires December 13, 2026 and needs renewal.

The sync leaves old unimported assignments and unpublished/deleted source items alone.
It does not automatically mark tasks completed or delete Notion rows. Course term configuration
must be changed for a new semester. Scheduled GitHub runs can be delayed by runner availability.
