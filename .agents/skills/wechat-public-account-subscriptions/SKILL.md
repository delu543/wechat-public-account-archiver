---
name: wechat-public-account-subscriptions
description: Bind a WeChat Official Account from one public article URL, or incrementally update every registered account to today. Use for 添加公众号, 更新全部公众号到今天, link-first batch history discovery, and one-Word-per-account incremental archives. Do not use for WeChat chat export.
---

# WeChat Public Account Subscriptions

Provide two fixed actions over the local public-account archive. Prefer the existing project workflow; do not rebuild the crawler ad hoc.

## Project and data boundary

- Use the active installed product workspace for real archive runs. Treat a clean source checkout as publication code, not as the live database workspace, and never copy product data into it.
- Preserve `data/archive.sqlite`, `~/Library/Application Support/WeChatPublicAccountArchive/`, existing Word files, output batches, logs, and task checkpoints.
- Use only public articles and the user's normal visible Mac WeChat session. Never persist or report raw signed session parameters.
- Do not start or resume the background LaunchAgent unless the user explicitly asks.

## Choose the action

- `添加公众号`: require one public `https://mp.weixin.qq.com/s/...` article URL and either a valid `YYYY-MM-DD` start date or an explicit `全部历史` choice. If the range is missing, ask for it before crawling.
- `更新全部公众号到今天`: use the registered subscriptions, saved seed URLs, exact `biz` values, and stored successful checkpoints. Do not ask for new links unless a saved seed is invalid and no other public seed exists.
- For any bounded run, keep the same inclusive start/end dates through discovery, every crawl pass, retries, accounting, and Word export.

## Action 1: 添加公众号

1. Parse the supplied public article first and identify `account_name`, `wechat_id`, and exact `biz`. Reject a result that does not expose or match `biz`.
2. Store the public seed URL with that exact account. Keep any existing articles and export membership intact.
3. Probe URL-history discovery before touching WeChat. The article URL is a durable account seed; it does not need to be the newest article.
4. Only when the probe reports a session-recovery condition, open a fresh WeChat global search and use the seed URL to open its `访问网页` result. Never use chat-history search. Do not type the account name while a valid seed URL exists.
5. After the article body loads, validate that the exact account session changed, then paginate `profile_ext?action=getmsg` newest-first from offset `0` back to the requested boundary.
6. Crawl only that run's discovered URLs in stages: fast first pass, low-concurrency failed retry, then one conservative `unavailable` retry.
7. Upsert the subscription. For a genuinely new account, produce its requested initial archive; for an already archived account, adopt existing articles as the baseline unless the user explicitly requests re-export.
8. Export only successfully parsed, eligible articles for the exact `biz`. Do not produce an empty Word file.

## Action 2: 更新全部公众号到今天

1. Load every enabled subscription and its saved `seed_url`, exact `biz`, WeChat ID, last success, and export membership. No new link input is normally required.
2. Set the end date to today's local date. For each account, derive the inclusive start boundary from its latest successful archived publication date minus the configured overlap/lookback window. This overlap is deliberate and is removed by deduplication.
3. Always start history discovery at offset `0`, so new articles ahead of an old resume offset cannot be skipped. Paginate backward only until crossing the derived boundary.
4. Probe the saved link and exact `biz` first. Use visible WeChat only for a bounded session refresh when `needs_session`; open one seed article per affected account, never each discovered article.
5. Keep crawl and unresolved accounting scoped to the URL IDs discovered in the current run. Older failures remain auditable but must not pollute the current increment.
6. Deduplicate by normalized URL, article identity, non-empty content hash, and completed Word membership.
7. Generate at most one incremental Word per account for genuinely new eligible articles. If an account has zero new articles, report zero and create no empty Word.
8. Record the completed batch and only then advance the account's last-success checkpoint.

## Stop and report

- Stop on CAPTCHA, login, paywall, rate limit, access restriction, or a still-invalid signed session. Ask the user only for native WeChat login or verification that cannot be automated.
- A parsed `biz` is account identity, not a signed history session. Treat `missing appmsg_token`, `ret=-3`, `ret=-6`, `invalid appmsg_token`, and `no signed article URL found` as session-recovery states rather than reasons to enumerate articles manually.
- Keep `captcha`, `unavailable`, `missing article title`, empty body, mismatch, and other failures explicit. Never fabricate content or call a partial archive complete.

## Completion contract

Report per account:

`discovered = already archived overlap + newly crawled + failed + restricted`

Before declaring completion, require:

- exact-`biz` isolation;
- zero duplicate normalized URLs;
- zero duplicate non-empty content hashes within an account;
- zero article membership in multiple Word batches;
- verified Word article/link counts and explicit output paths;
- the background LaunchAgent state unchanged unless resuming it was part of the request.

## Direct invocations

- `$wechat-public-account-subscriptions 添加公众号：https://mp.weixin.qq.com/s/...；从 2026-08-04 开始。`
- `$wechat-public-account-subscriptions 更新全部公众号到今天。`
