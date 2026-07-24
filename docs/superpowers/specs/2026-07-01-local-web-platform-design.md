# Local WeChat Archive Platform Design

## Goal

Build a local website for the existing WeChat public-article archive project. The site lets the user input one public article URL, run the existing reverse archive flow, monitor progress, and browse exported account folders and articles.

## Scope

- Run only on the local machine.
- Reuse the existing SQLite database, article parser, history discoverer, crawler, and Markdown exporter.
- Show account folders first, then article lists after clicking an account.
- Show task progress and estimated remaining time for active archive jobs.
- Keep the existing safety boundary: no CAPTCHA solving, no login bypass, no paid-wall bypass, no private database decryption.

## Architecture

- `wechat_crawler/web_app.py` provides a small local HTTP server using Python standard library only.
- API endpoints read from `Storage` and `output/`.
- Archive jobs run in a background thread and update an in-memory job state object.
- Static UI files live under `wechat_crawler/web/static/`.
- `main.py web` starts the local server.

## Data Flow

```text
Browser URL input
-> POST /api/jobs
-> background archive job
-> parse input article
-> discover history URLs where publicly available
-> crawl pending article URLs
-> export Markdown
-> UI polls /api/jobs/<id>
-> UI refreshes /api/accounts
```

## Pages

- Home: URL input, active job progress, account folder list.
- Account view: account metadata and article list.
- Article view: selected article body and metadata.

## Progress

Progress uses coarse phases:

- Parse account: 0-10%.
- Discover history URLs: 10-35%.
- Crawl article bodies: 35-90%.
- Export Markdown: 90-100%.

During crawling, ETA is based on elapsed time per processed URL multiplied by remaining URLs.

## Error Handling

- History discovery blocked by verification/session expiry becomes a warning in the job, not a bypass attempt.
- Restricted or unavailable articles are marked as restricted and counted.
- Fetch and parse failures are recorded in logs and shown in the job message.
