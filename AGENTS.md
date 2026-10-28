# Codex Project Guide

This repository installs a local macOS WeChat Official Account archiver.

## Current Workflow

Read `docs/CACHE_UPDATE.md` first for repeatable public-cache increments. Prefer
`main.py cache-update --check`; it does not initialize or modify the product DB.
New collection is bounded and explicit. Reviewed Word membership is independent
of full-coverage checkpoints; unknown coverage and old gaps must remain visible.
Do not re-enable the legacy foreground recoverer or scheduler because a cache
has no entries. The published default disables automatic jobs and UI preparation;
existing installations retain their own pause/config and need separate rollout
acceptance. Key initialization is never implied by an ordinary update request.

## First Run

When the user asks to install or start the project:

1. Read `README.md` and `.agents/skills/wechat-public-account-history/SKILL.md`.
2. Run `./install.sh --check`.
3. If preflight succeeds, run `./install.sh`.
4. Verify `http://127.0.0.1:8765/api/health`.
5. Report the local controller and Word output paths.

Do not run installation solely because the repository was opened. The user must
ask to install or start it.

## Development

- Run tests with `.venv/bin/python -m pytest -q`.
- Keep macOS automation, local website, incremental Word output, deduplication,
  session recovery, and cleanup behavior regression-tested.
- Preserve the stable `WeChatArchiveStatus.app` identity during upgrades.
- Keep the web service bound to `127.0.0.1` unless the user explicitly changes it.

## Data Boundaries

- Never commit `data/`, `output/`, `logs/`, SQLite files, Word files, raw article
  corpora, WeChat cache, signed URLs, or session credentials.
- Use only public content and the user's normal visible WeChat session.
- Pause the affected source and dependent steps on CAPTCHA, login, paywall, rate limit or access restriction; record the reason and continue independent authorized work. A shared session/platform restriction pauses all affected requests; do not switch identities or routes to evade it.
- Do not disable the user's VPN or weaken macOS privacy controls.

## Release Gate

Before publishing:

1. Run the full test suite.
2. Run `./install.sh --check`.
3. Scan tracked files for secrets and absolute user paths.
4. Confirm generated and local runtime data remain ignored.
