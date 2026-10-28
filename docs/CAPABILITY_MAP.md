# Capability Map

## Current recommended workflow

- `main.py cache-update` defaults to a read-only retained-stage/delivery check, dispatched before product database initialization. See [CACHE_UPDATE.md](CACHE_UPDATE.md).
- Explicit bounded shared public-cache collection, dated per-account discovered-only Word, text/link verification, immutable pending batches and rendered-review receipts. Verified membership suppresses repeats without advancing full-coverage checkpoints or closing historical gaps.
- No client UI, process attachment, key acquisition, network requests, product writes or background resume in this entry. Missing retained data are unknown, not zero updates. Human homepage loading is needed only when a usable current listing/cache is unavailable.
- `weekly-run` remains conditional on a working, separately authorized push binding; successful end-to-end initialization and complete-publication coverage are not established on the current machine. No unattended/source or installed-plugin guarantee follows from offline tests.

## Existing capabilities (some legacy/client-dependent)

The following list records implemented interfaces, not proof that each source is currently usable. In particular, old signed-session and foreground-recovery paths are **not** automatic fallbacks for `cache-update`; do not invoke them merely because a cache lacks entries.

- URL import: read public WeChat article URLs from text files and store de-duplicated `article_urls`.
- Reverse account archive: parse a public input article URL, identify its account, batch-discover history URLs, crawl pending bodies, and export Markdown.
- Account discovery: `discover-account --url` parses one article and fills the URL pool through account history discovery without requiring manual per-article input.
- Optional start-date boundary: `discover-account`, `reverse`, `history-sync`, staged `crawl`, and local website jobs accept an inclusive `YYYY-MM-DD` start date. History pagination stops after crossing that boundary; leaving it empty preserves full-history behavior.
- Article crawl: fetch public article pages conservatively, stop on CAPTCHA/login/rate-limit/paywall signals, and log failures.
- Account-scoped crawl: limit crawling by account name/hint or exact account biz, with optional publish-date save filters.
- Staged fast crawl: `crawl --workers --min-delay --max-delay --max-retries` supports a fast first pass, retry pass, and slower cleanup pass.
- Account guard: when crawling by exact biz, parsed articles whose biz does not match the target are marked `mismatch` instead of being saved into the wrong account.
- Article parse: extract account identity, title, author, publish time, body HTML/text, image URLs, cover image, raw HTML, and content hash.
- Article body layout: preserve breaks at HTML block elements while joining inline spans/font runs; Word export can reconstruct that layout from retained clean HTML when its text matches already-archived content, without rewriting the SQLite article.
- Public-account publication dates: script epoch timestamps are stored in Beijing time; Word date labels and article-range headers convert already-archived timezone-aware timestamps to Beijing time while leaving timezone-naive page dates unchanged.
- SQLite archive: store accounts, discovered URLs, articles, crawl runs, source records, and crawl logs.
- Markdown export: group articles by account and write `metadata.json`, `account_metadata.json`, and one Markdown file per article.
- Public search discovery: optional, conservative discovery through public search results.
- Local website: `main.py web` starts a local UI for URL submission, fixed URL-history-first archive jobs, job progress/ETA, account folder browsing, article lists, and Markdown reading.
- Local website date selection: the task form accepts an optional start date and keeps that exact boundary through discovery, staged retries, and progress reporting.
- Local website session recovery: the saved public article URL is the primary visible recovery input. The foreground helper always raises and focuses the main WeChat window—even when it is the only window—before pasting that URL into a fresh search field (never `Command-F` or chat-history search). If the main window is protected, it raises a retained public-search window from WeChat's Window menu, selects its first search tab, clicks the green `搜索` button and the top `访问网页` result, waits for that exact account's signed-session fingerprint to change, and only then retries exact-`biz` history. Account-name `搜一搜` remains a legacy compatibility fallback only when no valid `mp.weixin.qq.com/s` seed URL exists.
- Automation newest-first boundary: every weekly/manual automation cycle starts history pagination at offset `0`, then keeps the stored inclusive `since_date` through discovery and every crawl pass, so it collects the newest items back to the prior cutoff instead of resuming from a stale CLI offset.
- Local website staged crawl: web archive jobs run a fast first body crawl, low-concurrency failed retry, conservative `unavailable` retry, unresolved-reason summary, and exact-account Markdown export.
- Reprint-panel parsing: when a public repost page exposes an empty `#js_content` but embeds its visible target-account preview in `content_noencode`, the parser preserves that text under the requested account; it does not follow the original-account link across `biz` boundaries. Generic “文章内容无法查看” pages are recorded as `unavailable` instead of parser failures.
- Per-run URL scope: staged crawl, conservative retries, and unresolved counts are limited to URLs returned by that run's history pagination, so older account failures remain auditable without polluting or slowing the current increment.
- Completed job summaries distinguish already archived URLs from newly saved articles, making overlap de-duplication explicit without repeating old articles in Word output.
- Local website article lists are paginated so large accounts do not render thousands of rows at once.
- Local website list APIs use count/summary SQL queries and load full article bodies only for single-article detail views.
- Large-archive startup avoids repeating the one-time account-adoption scan once subscriptions exist, and a covering account/publish-date index keeps schedule/status queries away from multi-gigabyte article bodies.
- Persistent automation subscriptions: existing accounts adopt current articles as a no-output baseline; an explicit current-baseline activation safely absorbs only articles not already assigned to a historical Word batch, enables all populated accounts, and supersedes stale unfinished jobs. New accounts start with one full-history Word and then switch to weekly incremental mode.
- Exactly-once Word batches: export membership is stored per article in SQLite, so retries, restarts, and catch-up runs cannot repeat articles in later product Word documents.
- Word range labeling: manual bounded exports can use an explicit `range_incremental` mode so the document says “区间增量”, while scheduled exports keep their “本周增量” wording and add the Monday-to-Sunday cycle range to every account and summary Word filename.
- Pending-export recovery: `automation-export-pending` and the website action finalize already-captured articles after a verified visible-session/public-URL fallback without rerunning discovery or duplicating prior Word membership.
- Weekly scheduler: a catch-up scheduler reads `next_run_at` from SQLite; the default schedule is Sunday 21:00, and macOS LaunchAgent support starts the local website and scheduler at login.
- Persistent global pause: one SQLite-backed master switch blocks new scheduled/manual crawls and visible WeChat session recovery without unloading the local service or changing per-account enablement; a running archive job stops at its next safe page/article checkpoint and stays resumable. The switch survives process and Mac restarts until explicitly resumed.
- Product cleanup: after a Word batch is verified and registered, parsed articles may drop reproducible `raw_html`; clean text, clean HTML, metadata, Markdown, failures, and final Word remain.
- Automation website: subscription enable/pause, run now, schedule editing, persistent progress/ETA, session state, cleanup preview, Word downloads, and the preserved folder/article reader.
- Automation website output access: the header shows the fixed Word and database locations and opens only the configured product output directory in Finder.
- Native macOS status item: an independently supervised, unprivileged menu-bar app uses a compact icon at idle, expands only for progress/attention, reasserts visibility after wake and during health polling, and shows schedule, progress, ETA, session attention, and latest Word through a local-only API. Its single master button toggles “暂停全部爬取 / 继续全部爬取”; pausing immediately suppresses or terminates foreground WeChat recovery while keeping the plugin and local status service available. A durable whole-cycle completion event produces one “已经弄好了” notification; clicking opens that cycle's folder, with a native alert fallback if notifications are disabled. It never requests Accessibility access or reads the WeChat container directly.
- Direct-first foreground recovery: the status App launches one foreground helper for the exact waiting job. That helper first retries the saved exact-`biz` session, opens the persisted seed once only on `needs_session`, and then completes the same scheduled job; it does not split preparation and recovery across two workflows.
- Stable macOS permission identity: product updates compare the status App source hash and preserve the existing `WeChatArchiveStatus.app` when unchanged; changed notification/UI code is upgraded once in place instead of leaving a stale binary installed.
- Renderer-aware WeChat targeting: process selection uses `com.tencent.xinWeChat`, checks the `WeChatAppEx` renderer, and falls back from missing AX windows to title/bounds-only WindowServer metadata. Native mouse events operate the public web renderer while in-process keyboard events paste the saved URL; protected window pixels are never required.
- Verified recovery gate: `article_open_attempted` records input attempts only. Scheduled and web history retries require `session_verified`, an observed change in the exact account's opaque cache fingerprint. Unchanged fingerprints or native login/lock interruption remain waiting, not success; the web entry retains its original public seed URL.
- Access-restriction hard stop: signed-article and history responses requiring CAPTCHA/verification or returning HTTP 401/403/429 stop immediately, without trying another cached signature, reloading the session, retrying the page, or starting visible WeChat recovery. Web jobs expose `blocked`, not `needs_session`. The current product's subscriptions share one local WeChat session, so automation blocks related enabled subscriptions and unstarted cycle jobs until an explicit retry after normal verification; it preserves enablement, master pause, articles, export membership and successful cutoffs.
- Fresh-target input guards: each native input checks runtime readiness; mouse targets are resolved again by exact window title and current bounds. Main-window paste additionally requires an exposed search-control focus, without reading chat text or input values. Missing/minimized targets, loss of focus, lock or logout stop the sequence and restore the clipboard.
- Bounded session-only cache reads: fingerprint/session scans have an 8-second soft total budget plus existing per-operation I/O limits; normal public-URL discovery retains its full bounded-file scan. Budget exhaustion means unverified, not proof that no new articles exist.
- Hidden-window recovery: when the logged-in WeChat process exists but exposes no usable main window, the foreground helper activates WeChat once and rechecks before opening the saved public seed URL.
- Bounded foreground retries: a failed session refresh is not recorded as success; the status App retries each affected account after a one-minute cooldown, at most three times per day, then leaves the auditable `needs_session` state instead of clicking indefinitely.
- Lock-screen guard: background and menu-bar helpers defer visible UI work while the Mac is locked and never interact with the login window.
- Interrupted-run recovery and retry backoff: stale in-progress crawl/sync rows are closed with an audit reason, while `needs_session` and transient errors receive bounded retry times instead of being retried every minute.
- Exit-path cleanup: success, no-update, failure, and session-wait paths remove product-owned temporary files; reproducible raw pages are dropped after parsed content is retained, without touching WeChat caches.

## Experimental

- Passive multi-account cache staging retains exact-biz long articles and short content across live cache replacements, with bounded Snappy/LevelDB decoding and read-only review against the existing export baseline; it does not resume paused background tasks or prove complete coverage from endpoints alone.
- Machine-verifiable independent publication catalogs can pass the complete Word gate without a human confirmation file or a separate cache-overlap chain. The normalized evidence must include all content types, current interval coverage, uninterrupted newest-first pagination and explicit end/boundary termination; page integrity, identity/date membership and healthy bodies are reconciled. Human-supplied catalogs retain their explicit confirmation path. The validator does not create such evidence or establish that a real source provides it.
- Browser-assisted public page discovery.
- Local WeChat visible-list/history session discovery. This is opt-in and may read local WeChat web/cache files generated by the user's visible WeChat session. It must not persist sensitive URL parameters such as `key`, `pass_ticket`, `uin`, `exportkey`, or session identifiers.
- WeChat content-flow cache import. When `profile_ext` history pagination returns `ret=-6` for an already-open visible history page, `history-cache-sync` can decode the local `__batch_get_appmsg_data` cache, filter by exact biz/微信号, save sanitized article records, and optionally PageDown the visible WeChat window to load more public history items.
- WeChat history session recovery detection classifies missing `appmsg_token`, missing signed article URLs, `ret=-3`, and `ret=-6` as recoverable session states only when there is no explicit verification/access restriction. CAPTCHA is a hard stop, not an automatic refresh instruction.
- History discovery resume: CLI and web config support a `start_offset` so interrupted `profile_ext` pagination can continue from the last logged `next_offset`.
- History pagination catches transient `profile_ext` network failures such as connect/read timeout, retries the current page once, and records a blocked run instead of crashing if the retry also fails.

## Preserved Boundaries

- No CAPTCHA solving.
- No login bypass.
- No paid-wall bypass.
- No private database decryption.
- No raw signed URL/token material in ordinary reports.
- No screen-protection bypass; protected WeChat pixels are neither captured nor persisted.
- Window capture availability and public history authorization are independent: a valid exact-account session uses the direct data path without querying or operating the window. Protected pixels alone do not prove logout. If the session is missing/expired and neither a verifiable public-search control nor another supported normal entry exists, recovery remains unavailable; `biz`, process existence and old cached links cannot manufacture a new signed session.
- A successful macOS `open -b` request proves only that LaunchServices accepted it, not that WeChat loaded an article or refreshed its session. On this machine's 2026-10-26 probe it only raised the main window; it is not an enabled automatic recovery fallback.
- No search or reading of the user's historical chat messages; visible recovery only pastes the account's persisted public seed URL into a fresh main-window or public-search field and opens its public “访问网页” result once.
- No automatic Mac unlock or password entry.
- Local website APIs must not expose raw signed URL/token material.
- Date-limited runs must not delete previously archived older articles; a later full-history run can still process URLs previously marked out of range.
