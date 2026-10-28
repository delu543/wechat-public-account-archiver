---
name: wechat-public-account-history
description: Discover and archive a public WeChat account's articles and short content from an article seed, including history pagination, incomplete cache recovery and exact-biz Word export. Adapt source selection to current client capabilities. Not for private chat export.
---

# WeChat Public Account History

Expand a public article seed into the requested account interval using the active product workspace identified by project guidance, not an assumed personal path. Prefer mature project readers and exporters over ad hoc replacements. This is a version-adaptive workflow, not a mandatory old-client click sequence.

## Inputs and existing progress

- Reuse the user's seed links, saved exact `biz`, WeChat ID, archive/export progress and unresolved gaps. An old public seed identifies the account; it need not be the newest article.
- Preserve an already established inclusive start/end interval. For a new request with no determinable range, ask whether it means all history or a start date.
- Full-history discovery and incremental discovery use the same source; full history runs to verified end-of-list, increments to the saved boundary.
- Inspect project guidance, capability docs and current task state. Historical failure notes are evidence, not universal prohibitions. Methods can be repaired under the current user authorization.
- Preserve old databases, outputs and user changes. Authorized network access, new staging and local export are normal task steps; they are not prohibited by an earlier read-only diagnostic.
- Do not start paused automation, initialize keys, access private stores or send private data to a third party under this skill alone.

## Source decision

First determine which source actually works in this environment. Do not require a `profile_ext` probe or WeChat UI operation before using already available public data.

- A working public history listing or authorized official export can support completeness if pagination, exact identity, content types and interval boundaries are verifiable.
- A separately authorized, already bound push helper can provide unattended discovery. It may omit articles that were published but not broadcast; label that boundary.
- Native public caches are a viable passive source. Use the shared bounded reader and persistent per-account accumulator, merging across cache windows rather than scanning only the final page.
- Public search and alternate catalogs can supplement discovery, not prove completeness merely by returning results. Validate every candidate against exact `biz`; a directory with long articles only cannot satisfy an all-content request.
- Use legacy session pagination or visible recovery only after confirming it is still supported and permitted. Do not acquire/replay client keys, inject into WeChat, or repeatedly cause logout to preserve an obsolete method.

Network requests and durable storage are allowed when in the user's task scope. A real CAPTCHA/login/paywall/401/403/429 restriction stops the affected source and shared requests; do not solve it automatically or change routes/identities to evade it. A normal user verification and explicit resumption are different from unrestricted automatic retries.

## Passive cache recovery

1. Locate the active public homepage cache using the existing whitelisted reader; avoid unrelated chats and private stores.
2. Snapshot bounded files, decode known Chromium/LevelDB formats and inspect actual health errors before attributing misses to the user.
3. Validate exact `biz`, WeChat ID and permanent link identity in the inner record even when an outer index omits a field.
4. Retain sanitized public records in an owned stage before the live cache replaces them; keep earlier windows and deduplicate across them.
5. Preserve both article and short-content body fields. Label a derived first-line title. A short text is not necessarily a preview; explicit truncation/restriction and known incomplete baselines remain unresolved.
6. Compare newest/oldest boundaries, page ordering, read errors, conflicting versions and a genuine independent publication index when available.
7. Do not make the user repeat an already completed nine-account refresh because a decoder failed. First repair/retest the reader and consume retained data. For a remaining specific gap, request the smallest input and start collection before the user's action.

Cache contents being readable or spanning both dates does not establish that every intervening publication was retained. Do not manufacture a complete index from the same incomplete set of bodies.

## Conditional visible entry

Only when currently authorized and safe:

- Verify a normal unlocked window and actual input focus. Use a saved public URL in a fresh public search field and open its visible result; do not search chat history or send the link to a conversation.
- Do not require protected screenshots, weaken privacy controls or use blind coordinates. A protected/unusable window stops this step, not independent authorized parsing.
- Preserve the clipboard where relevant. A launch command or visible page is not proof of fresh account data; verify the resulting exact-account list/cache.
- After login/verification/forced logout, stop repeated automation and reassess the source. The old search/click path is not mandatory.

## Retention, bodies and export

- Keep Beijing publication dates and the same interval in every pass. Save permanent public URLs and source evidence, never raw session parameters.
- Fetch missing publicly accessible bodies conservatively. Preserve empty/unavailable/mismatch/verification reasons. Do not follow a repost's original across account boundaries to fill the wrong archive.
- De-duplicate URL identity, non-empty body hash and export membership; preserve per-account isolation and resumable batch state.
- Complete export requires genuine interval coverage and eligible bodies. Prefer machine verification when the source provides sufficient evidence; do not require the user to attest every machine-verified catalog. Human assertions must remain explicit and must never be invented.
- If only discovered content is known, keep that status and any gaps. Export it only when the user accepts partial/discovered output. Do not advance a full-history success node on unknown coverage.
- Export each qualified account without holding it behind an unrelated account. Create no empty Word; verify counts and readability, record membership, and report exact paths and the covered interval.
- Account for discovered = previously exported overlap + newly eligible + unresolved/restricted. Parsing tests, source success, complete coverage and Word delivery are separate claims.

## Project entry points

Inspect current `--help` and module interfaces before execution; commands are not guarantees of source availability:

- `main.py cache-update --check`: preferred repeatable retained-cache workflow; read `docs/CACHE_UPDATE.md`. No client read or product writes. Verified local Word receipts are independent from full-coverage progress and must suppress repeats even when the product node is unchanged.
- `main.py cache-update --collect --seconds 600`: bounded shared collection; start before genuinely necessary human browsing. Only an authorized one-off may add `--allow-local-stage-while-paused`; never change the persistent pause. Saved exclusions and known gaps remain in force.
- `main.py cache-update --export --accept-discovered`: explicitly accepted, dated per-account output; no empty Word and no completeness claim. Resume pending batches, actually render/review, then register with `--confirm-delivery '<batch folder>' --render-reviewed`. Registration is an operator attestation, not an automatic renderer. No user click is needed for routine artifact QA.
- `main.py weekly-run --check`: health/coverage check when supported by the current source configuration.
- `wechat_crawler.cache_capture_all`: shared public-cache discovery/collection for registered accounts.
- `wechat_crawler.cache_capture_review`: read-only reconciliation of retained stages against the product baseline.
- `wechat_crawler.cache_capture_word`: complete-coverage Word path; follow its actual evidence contract.
- `main.py discover-account`, `history-sync`, `history-cache-sync`: legacy/conditional paths, not compulsory fallback chains.

Keep source-specific implementation details and validation evidence in project docs, not permanent assumptions about one WeChat version in this skill.

The cache path still cannot invent uncached historical pages. For all-history requests, first seek a genuinely paginated complete source; explain the unverified boundary if none works, instead of promising that one seed or one opened article yields everything. Do not restart a failed key initialization or a catalog known to omit short content merely to claim automation.
