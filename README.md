# New-grad PM job monitor

Polls a curated 103-company watchlist, identifies early-career product-management roles, remembers every job it has seen, and sends newly discovered matches to Discord. Direct Greenhouse, Lever, Ashby, and Workday feeds are preferred; official career-page parsing is the fallback.

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

Greenhouse, Lever, Ashby, and Workday sources use public job-board endpoints. Large companies with custom career sites use conservative HTML/structured-data extraction and may occasionally block automated requests. Every scan stores source health, including the last job count and consecutive failures. `--status` labels feeds as `OK`, `EMPTY`, or `FAIL`.

## Discord safety

The monitor stores the webhook URL only in `.env`, which is gitignored. Alerts contain application-link cards and are split into batches of at most 10 embeds, matching Discord's webhook limit. Mentions are disabled. A match is marked as notified only after Discord accepts every batch, so a failed notification is retried on the next cycle.
