---
name: wechat-public-account-subscriptions
description: Bind a WeChat Official Account from one public article URL, or update registered accounts to today, with exact-account discovery, incremental deduplication and one Word per account. Use for 添加公众号 and 更新全部公众号. Not for private chat export.
---

# WeChat Public Account Subscriptions

Use the existing local archive for two actions: adding an account and updating registered accounts. Adapt the source to the installed client and actual data; an old interface or UI sequence is not a required step.

## Workspace and current authority

- Locate the active product workspace from project guidance. This source checkout is a publication repository, not permission to overwrite or initialize an existing product database.
- Read the project guidance and current task checkpoint. Preserve SQLite data, historical Word files, export membership and active user changes. Ordinary authorized public requests, new local staging and exports do not require another permission merely because a prior diagnostic was read-only.
- Keep the background master pause as the user requested. A separately authorized one-off collection may write its own staging area without resuming the scheduler.
- Source selection, pacing, file-edit counts and old tool preferences are methods, not new permission gates. Correct a stale method with evidence and regression tests; do not keep asking for routine edit-count exceptions.
- Private database/key setup, credential use, external transmission, paid services, deletion and production changes remain separately scoped. Do not extract or replay client credentials just because an old skill mentions them.

## Choose the action and interval

- **添加公众号**: use one public article URL to establish exact `biz`, account name and WeChat ID. Use the user's supplied start date or explicit all-history choice; ask only if the requested interval cannot be determined.
- **更新全部公众号到今天**: use saved subscriptions and public seeds; do not ask for links already present. Derive each account's start from verified article/export progress plus known unresolved gaps, not a stale `last_success` alone.
- Keep one inclusive date interval throughout discovery, body retrieval, retries, deduplication and Word naming. Use Beijing publication dates, not local cache-receipt times.
- Full history and increments share the discovery mechanism; only the stopping boundary changes. Do not assume full history requires the user to scroll every article.

## Select and verify a source

Inspect the available project CLI/modules before choosing exact commands. Prefer an already working, least-disruptive source:

1. A currently usable complete public listing/export with exact identity and pagination evidence.
2. An already initialized, separately authorized helper for public-account push records. Check binding/freshness; do not auto-initialize or acquire keys. Push records may omit unbroadcast publications.
3. Passive native public homepage/article caches, using the bounded shared reader and cross-batch accumulator. Include long articles and short content. Snapshot before parsing, retain sanitized public records, merge by exact identity, and preserve earlier batches when the live cache changes.
4. Low-frequency public search or another user-approved public catalog to find gaps, followed by exact identity validation. Search results, push rows and a catalog omitting short content do not independently prove all publications.
5. Legacy history/session or visible-UI recovery only if current evidence supports it and the current user scope permits it. Do not always probe `profile_ext`, wake WeChat, or cycle through UI recovery before reading an already useful cache.

Once the user has refreshed a homepage, inspect the reader and retained batches before requesting any repeat. Diagnose compression, inline/blob layouts, size bounds and identity fields. If specific manual input is genuinely needed, name only that gap and start a bounded collector before the user refreshes it.

For an authorized visible recovery, use a saved public seed and a freshly verified public search control; never inspect chat history or send a message. Protected windows, lock/logout and unsafe focus stop that interaction. Do not bypass screenshot protection or infer successful loading from a launch command.

## Discover, retrieve and retain

- Verify every candidate's exact `biz` and permanent article identity (`mid/idx/sn` where available). Do not archive another account's original article under a reposting account.
- Start a verified paginated listing at its newest page and continue to the requested old boundary/end. Retain page ordering/cursors and stop evidence; an old offset must not skip new items.
- Save sanitized public URLs, title or labeled short-content first line, publication time, structured body and source evidence to task-owned staging. Do not persist session parameters or unrelated private data.
- Short content is valid content; body length alone is not a truncation test. Explicit preview/truncation flags or a known incomplete baseline require repair. Preserve visible repost text without following a cross-account original as a substitute.
- Fetch public bodies conservatively. Log missing/unavailable/empty/mismatched/restricted items separately. On CAPTCHA, login, paywall or access/rate restriction, stop affected requests and shared dependents; continue independent local work. No alternate identity, route or proxy to evade the restriction.
- A transient transport error may receive a bounded retry. A verified code/input change may justify another test; unchanged failed attempts are not progress.

## Export and completion

- Deduplicate normalized URL, article identity, non-empty body hash and completed Word membership. Keep an auditable account-scoped batch and resume it after interruption.
- Export each account independently when its requested checks pass; one missing account does not invalidate the other accounts' evidence. Never silently lower a complete-only request to a partial export.
- Complete coverage needs a genuine interval-wide listing, all requested content types, reconciled identities/dates and valid bodies. Consecutive cache windows and newest/oldest matches are useful evidence, not a complete-list guarantee.
- Machine-verifiable listing evidence should be checked by code, not subjected to an unconditional human-signature requirement. Human confirmation is needed only for evidence that actually depends on a human assertion. Do not invent either kind of evidence.
- If the user accepts discovered-only output, label it clearly and preserve unknown coverage/gaps; do not advance the complete-coverage node.
- Generate no empty Word. Use dated filenames, verify article/link counts and document readability, register completed membership, then advance only the applicable verified progress.
- Report per account: discovered, already exported overlap, newly eligible, unresolved/restricted and exported. Distinguish code tests, real source retrieval, completeness, Word creation and delivered files.
- Calling automation ready additionally requires installed-runtime, scheduler/pause, next-run and real-batch checks. Source tests alone are not automation acceptance.

## Repeatable cache workflow

Read `docs/CACHE_UPDATE.md` in the selected product workspace and inspect `main.py cache-update --help`. Use this tested passive route for retained/manual-refresh cache runs; do not reconstruct the old task-only export script or wake the legacy UI recoverer.

1. Run `main.py cache-update --check`. It is read-only and does not scan the live client. Respect saved `cache_update.excluded_biz`, prior-stage references and verified delivery receipts. A frozen old product node alone must not cause delivered articles to be exported again.
2. If usable retained text exists and discovered-only delivery is accepted, run `main.py cache-update --export --accept-discovered`. New batches resume pending output before accepting later discoveries. Zero eligible retained articles means no empty Word, **not** proof that the account published nothing.
3. For an authorized update, first try one passive live snapshot with `--collect --seconds 1`, then recheck retained data before asking for manual work. If genuinely new source data are still needed, start bounded `--collect --seconds 600` before normal homepage browsing, and name only affected accounts and the reported `suggested_refresh_through`. `--collect --export --accept-discovered` chains collection and export. Use `--allow-local-stage-while-paused` only for an authorized one-off staging run; never resume the scheduler. Retain older historical gaps separately; do not ask for a repeated completed refresh because the reader failed.
4. Inspect the generated Word text/link checks and actually render/review it under the document workflow. Then use `--confirm-delivery '<returned batch folder>' --render-reviewed`. This is an operator attestation, not a renderer: do not use it before QA. The user need not click another confirmation for routine artifact review.
5. The next run automatically verifies Word/manifest hashes and excludes delivered identities/body hashes; changed previously delivered bodies remain explicit version gaps. Only independent full-publication evidence can advance full-coverage progress.

This reduces manual work to missing list loading, not a verified zero-manual discovery source. Push initialization remains conditional and separately authorized; do not silently retry setup, fall back to WeRead that omits short content, or publish private config/receipts when syncing source to GitHub.

## Direct invocations

- `$wechat-public-account-subscriptions 添加公众号：https://mp.weixin.qq.com/s/...；从 2026-08-04 开始。`
- `$wechat-public-account-subscriptions 更新全部公众号到今天。`
