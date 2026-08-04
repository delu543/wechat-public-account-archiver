# Capability Map

## Active

- URL import: read public WeChat article URLs from text files and store de-duplicated `article_urls`.
- Reverse account archive: parse a public input article URL, identify its account, batch-discover history URLs, crawl pending bodies, and export Markdown.
- Account discovery: `discover-account --url` parses one article and fills the URL pool through account history discovery without requiring manual per-article input.
- Optional start-date boundary: `discover-account`, `reverse`, `history-sync`, staged `crawl`, and local website jobs accept an inclusive `YYYY-MM-DD` start date. History pagination stops after crossing that boundary; leaving it empty preserves full-history behavior.
- Article crawl: fetch public article pages conservatively, stop on CAPTCHA/login/rate-limit/paywall signals, and log failures.
- Account-scoped crawl: limit crawling by account name/hint or exact account biz, with optional publish-date save filters.
- Staged fast crawl: `crawl --workers --min-delay --max-delay --max-retries` supports a fast first pass, retry pass, and slower cleanup pass.
- Account guard: when crawling by exact biz, parsed articles whose biz does not match the target are marked `mismatch` instead of being saved into the wrong account.
- Article parse: extract account identity, title, author, publish time, body HTML/text, image URLs, cover image, raw HTML, and content hash.
- SQLite archive: store accounts, discovered URLs, articles, crawl runs, source records, and crawl logs.
- Markdown export: group articles by account and write `metadata.json`, `account_metadata.json`, and one Markdown file per article.
- Public search discovery: optional, conservative discovery through public search results.
- Local website: `main.py web` starts a local UI for URL submission, fixed URL-history-first archive jobs, job progress/ETA, account folder browsing, article lists, and Markdown reading.
- Local website date selection: the task form accepts an optional start date and keeps that exact boundary through discovery, staged retries, and progress reporting.
- Local website session recovery: saved article URL and exact-`biz` discovery remain the fast path; when the signed history session is stale, the foreground helper clicks the main WeChat window's fresh global search field (never `Command-F` or chat-history search), opens a new public `搜一搜` window, replaces any related suggestion with the exact account name, selects the public-account result, tries the `文章` column at bounded profile-header positions, waits for that exact account's opaque signed-session fingerprint to change, and only then retries exact-`biz` history.
- Automation newest-first boundary: every weekly/manual automation cycle starts history pagination at offset `0`, then keeps the stored inclusive `since_date` through discovery and every crawl pass, so it collects the newest items back to the prior cutoff instead of resuming from a stale CLI offset.
- Local website staged crawl: web archive jobs run a fast first body crawl, low-concurrency failed retry, conservative `unavailable` retry, unresolved-reason summary, and exact-account Markdown export.
- Per-run URL scope: staged crawl, conservative retries, and unresolved counts are limited to URLs returned by that run's history pagination, so older account failures remain auditable without polluting or slowing the current increment.
- Completed job summaries distinguish already archived URLs from newly saved articles, making overlap de-duplication explicit without repeating old articles in Word output.
- Local website article lists are paginated so large accounts do not render thousands of rows at once.
- Local website list APIs use count/summary SQL queries and load full article bodies only for single-article detail views.
- Persistent automation subscriptions: existing accounts adopt current articles as a no-output baseline; new accounts start with one full-history Word and then switch to weekly incremental mode.
- Exactly-once Word batches: export membership is stored per article in SQLite, so retries, restarts, and catch-up runs cannot repeat articles in later product Word documents.
- Pending-export recovery: `automation-export-pending` and the website action finalize already-captured articles after a verified visible-session/public-URL fallback without rerunning discovery or duplicating prior Word membership.
- Weekly scheduler: a catch-up scheduler reads `next_run_at` from SQLite; macOS LaunchAgent support starts the local website and scheduler at login.
- Product cleanup: after a Word batch is verified and registered, parsed articles may drop reproducible `raw_html`; clean text, clean HTML, metadata, Markdown, failures, and final Word remain.
- Automation website: subscription enable/pause, run now, schedule editing, persistent progress/ETA, session state, cleanup preview, Word downloads, and the preserved folder/article reader.
- Automation website output access: the header shows the fixed Word and database locations and opens only the configured product output directory in Finder.
- Native macOS status item: an independently supervised, unprivileged menu-bar app shows schedule, progress, ETA, session attention, and latest Word through a compact local-only API. It never requests Accessibility access; visible WeChat preparation is delegated to the already-authorized Python background runtime.
- Stable macOS permission identity: product updates preserve an existing valid `WeChatArchiveStatus.app` bundle instead of recompiling it on every runtime update, preventing avoidable repeated Accessibility prompts.
- Renderer-aware WeChat targeting: process selection uses `com.tencent.xinWeChat`, checks the `WeChatAppEx` renderer, falls back from missing AX windows to title/bounds-only WindowServer metadata, and never depends on protected window pixels.
- Lock-screen guard: background and menu-bar helpers defer visible UI work while the Mac is locked and never interact with the login window.
- Interrupted-run recovery and retry backoff: stale in-progress crawl/sync rows are closed with an audit reason, while `needs_session` and transient errors receive bounded retry times instead of being retried every minute.
- Exit-path cleanup: success, no-update, failure, and session-wait paths remove product-owned temporary files; reproducible raw pages are dropped after parsed content is retained, without touching WeChat caches.

## Experimental

- Browser-assisted public page discovery.
- Local WeChat visible-list/history session discovery. This is opt-in and may read local WeChat web/cache files generated by the user's visible WeChat session. It must not persist sensitive URL parameters such as `key`, `pass_ticket`, `uin`, `exportkey`, or session identifiers.
- WeChat content-flow cache import. When `profile_ext` history pagination returns `ret=-6` for an already-open visible history page, `history-cache-sync` can decode the local `__batch_get_appmsg_data` cache, filter by exact biz/微信号, save sanitized article records, and optionally PageDown the visible WeChat window to load more public history items.
- WeChat history session recovery detection classifies missing `appmsg_token`, verification prompts, missing signed article URLs, `ret=-3`, and `ret=-6` as recoverable session states.
- History discovery resume: CLI and web config support a `start_offset` so interrupted `profile_ext` pagination can continue from the last logged `next_offset`.
- History pagination catches transient `profile_ext` network failures such as connect/read timeout, retries the current page once, and records a blocked run instead of crashing if the retry also fails.

## Preserved Boundaries

- No CAPTCHA solving.
- No login bypass.
- No paid-wall bypass.
- No private database decryption.
- No raw signed URL/token material in ordinary reports.
- No screen-protection bypass; protected WeChat pixels are neither captured nor persisted.
- No search or reading of the user's historical chat messages; visible recovery uses only the main-window global search field and public-account pages.
- No automatic Mac unlock or password entry.
- Local website APIs must not expose raw signed URL/token material.
- Date-limited runs must not delete previously archived older articles; a later full-history run can still process URLs previously marked out of range.
