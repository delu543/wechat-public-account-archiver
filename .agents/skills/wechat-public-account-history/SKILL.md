---
name: wechat-public-account-history
description: Use when archiving WeChat Official Account articles from one public article URL, especially when history discovery is slow, incomplete, missing appmsg_token, ret=-3, ret=-6, captcha, unavailable, or local website jobs need session recovery.
---

# WeChat Public Account History

Use this skill in this repository when one public article URL should expand into that account's public history archive.

## Boundaries

- Use the original article URL, exact `biz`, and the user's visible Mac WeChat public session.
- Do not use third-party search as a substitute, solve CAPTCHA, bypass login/paywalls, decrypt private stores, or persist/report raw `key`, `pass_ticket`, `uin`, `exportkey`, session ids, or token-like values.
- Keep accounts separated by exact `biz`; account name is not enough.
- Never search or read the user's historical chats. Visible recovery must start from a fresh main-window global search field and public-account pages.

## Mandatory Order

1. Before every new account crawl, establish the time range in the Codex conversation. If the user did not already provide it, ask: `抓取全部历史，还是从某个日期开始？` Wait for the answer. For a bounded crawl, require a valid `YYYY-MM-DD` start date; never guess one. An explicit `全部/全量/所有历史` answer means no date flag.
2. Parse the seed article and identify `account_name`, `wechat_id`, and `biz`.
3. Always probe URL history discovery with the saved seed URL and exact `biz` before opening or searching WeChat. An old public article URL is sufficient as the durable seed; the account homepage is not a required input. For a bounded crawl, pass the same `--since YYYY-MM-DD` to discovery, every crawl/retry pass, and any resumed command.
4. If the probe reports `missing appmsg_token`, `ret=-3`, `ret=-6`, `requires verification`, `invalid appmsg_token`, or `no signed article URL found`, treat it as a visible WeChat session problem. Close only stale product-owned public-search/profile windows, click the fresh global search field in the WeChat main window, submit the exact account name in a new public `搜一搜` window, select the public-account result, switch to the `文章` column, and open a formal public article. Do not use `Command-F`, because current WeChat versions route it to chat-history search. Confirm success only after the requested exact account's opaque signed-session fingerprint changes, then rerun the same URL/history command with the same date range. Ask the user only for native CAPTCHA or login confirmation that cannot be automated. Do not disable the user's VPN. If an externally launched `weixin://` or article link opens a `提示/当前页面无法访问` shell while ordinary public article HTTP requests succeed, stop retrying the external launch and use the normal in-WeChat entry instead.
5. Use URL history pagination as the primary path. It is the fast path: one valid session can discover hundreds or thousands of URL records. Every weekly/manual automation cycle begins at offset `0` and scans newest-first back to the stored inclusive cutoff; a CLI resume offset must never leak into incremental automation. A start date only stops pagination after the date boundary; it does not change the discovery method.
6. Use `history-cache-sync` only as a fallback after the article-detail session path has been retried and still cannot paginate. Cache import only sees complete article bodies already written by WeChat; visible list cards alone are not proof that full bodies are cached.
7. Crawl in stages: fast first pass, low-concurrency failed retry, then one conservative retry for `unavailable` if needed. Stop and log `captcha`, login, rate-limit, paywall, and still-unavailable records. Keep the selected date range identical across all passes.
8. Export only the target account by exact `biz`.

## Mac WeChat Window Recovery

- Target WeChat by bundle identifier `com.tencent.xinWeChat`, never by the localized process name alone. WeChat 4.x may expose an outer shell process while the real content renderer runs as `com.tencent.flue.WeChatAppEx`.
- Before any UI action, verify that the Mac session is unlocked, the outer process is running, and the renderer process exists. If the screen is locked or WeChat only exposes the compact login shell, defer the task without clicking the login window.
- A blank or white capture is not proof that WeChat is logged out. First inspect the renderer process and visible WindowServer title/bounds metadata. Use those signals plus exact-`biz` history/API results; do not weaken macOS privacy controls or attempt to capture protected pixels.
- Activate the main WeChat window through the bundle identifier. Search the exact account name, open the result, and validate success by observing the requested account's exact `biz` in newly available article/history data. A click attempt without this validation is not success.
- Use the main-window global search field and a newly opened public-search window for every recovery attempt. Do not reuse or inspect a chat-history search window.
- Prefer native foreground keyboard/mouse events against freshly read window bounds because WeChat 4.x renderer cards may not expose ordinary AX controls. Never reuse an arbitrary stale `微信 (窗口)` or `公众号` window without validating the target account.
- Scan a bounded newest-first set of Chromium `Cache/Cache_Data` files in addition to `Share Data`, IndexedDB, and other existing stores. This is read-only; do not copy the cache or persist signed parameters.
- Discover the currently active `multitab_*` profile from the public open-file metadata of `WeChatAppEx`; do not enumerate the protected historical `profiles` parent. Put short killable timeouts around protected directory/file reads so an unavailable cache returns `needs_session` instead of hanging.
- Preserve and restore the clipboard after temporary search text. Never persist or print session tokens.
- Compare only an opaque in-memory hash of exact-account signed URLs before and after the visible article open. Never return or log the signed URLs themselves.

## Completion Accounting

- Report discovery and export with an explicit equation: `discovered = already archived overlap + newly crawled + failed + restricted`.
- `exported` must equal the newly eligible, successfully parsed articles not already present in a completed export batch. Previously archived overlap is not a failed export.
- Require zero duplicate normalized URLs, zero duplicate non-empty content hashes within an account, and zero article membership in multiple Word batches before declaring completion.
- Generate Word through a preparing/completed batch record so an interrupted run can resume without exporting the same article twice.

## Date Range Contract

- No date means the existing full-history behavior. Do not add a date implicitly.
- `--since YYYY-MM-DD` means include articles published on that date or later.
- Automatic weekly/manual increments always start discovery at the newest page, then stop after reaching the stored inclusive cutoff.
- Date selection changes only which history records and bodies are processed. It must not change URL normalization, exact-`biz` account separation, session recovery, request pacing, retry classes, deduplication, storage, or export ordering.
- Previously archived older articles remain in SQLite and existing account exports. A bounded crawl does not delete older archive data.
- The local website exposes the same optional start-date field and sends it with the archive job.

## Commands

Fast URL-history path:

```bash
.venv/bin/python main.py discover-account --url "https://mp.weixin.qq.com/s/..." --delay 0.1 --max-items 1300
.venv/bin/python main.py crawl --account-biz "BIZ" --fast --workers 6 --min-delay 0 --max-delay 0 --max-retries 0
.venv/bin/python main.py crawl --account-biz "BIZ" --retry-failed --workers 3 --min-delay 0.2 --max-delay 0.5 --max-retries 1
.venv/bin/python main.py export --account-biz "BIZ" --force
```

Start-date variant (use the same date in every pass):

```bash
.venv/bin/python main.py discover-account --url "https://mp.weixin.qq.com/s/..." --since 2026-01-01 --delay 0.1 --max-items 1300
.venv/bin/python main.py crawl --account-biz "BIZ" --since 2026-01-01 --fast --workers 6 --min-delay 0 --max-delay 0 --max-retries 0
.venv/bin/python main.py crawl --account-biz "BIZ" --since 2026-01-01 --retry-failed --workers 3 --min-delay 0.2 --max-delay 0.5 --max-retries 1
```

Known account path:

```bash
.venv/bin/python main.py history-sync --biz "BIZ" --wechat-id "gh_xxx" --account-name "NAME" --delay 0.08 --max-items 1300
```

Cache fallback:

```bash
.venv/bin/python main.py history-cache-sync --biz "BIZ" --wechat-id "gh_xxx" --account-name "NAME" --max-cache-file-mb 220 --export
```

## Failure Meanings

- `missing appmsg_token`, `ret=-3`, `ret=-6`: open a public article detail page in Mac WeChat and rerun URL history.
- `提示/当前页面无法访问` only after external launch: verify public article HTTP reachability and proxy routing without changing network settings. If reachable, keep the VPN running and reopen the article from WeChat search or an in-WeChat link.
- visible history list but no cache records: list cards are rendered but full bodies were not written to `__batch_get_appmsg_data`.
- many timeouts: finish the fast pass, then retry failed URLs with lower concurrency.
- `captcha`: stop that source until the user handles normal WeChat verification; do not bypass.
- `unavailable`: retry once conservatively, then keep the failure reason.
- `missing article title` with a tiny shell page: record as parse failure; do not fabricate content.

## Local Website Contract

The website should call the same fixed workflow: accept an optional start date, parse and retain the seed URL, start exact-`biz` URL history at the newest page with that boundary, automatically attempt fresh-main-search visible-WeChat recovery only after `needs_session`, require an exact-account session-fingerprint change before claiming recovery, pause only if the final history retry fails, crawl all staged passes with the same boundary, show ETA and final unresolved counts, and export only the target `biz` folder.
