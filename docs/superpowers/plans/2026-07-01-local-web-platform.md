# Local Web Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local website that starts WeChat archive jobs, shows progress with ETA, and browses exported account folders and articles.

**Architecture:** Add a standard-library HTTP server in `wechat_crawler/web_app.py`, with JSON API endpoints and static HTML/CSS/JS. The server reuses `Storage`, `ArticleFetcher`, `WechatArticleParser`, `WechatHistoryDiscoverer`, and `MarkdownExporter`.

**Tech Stack:** Python standard library HTTP server, SQLite via existing `Storage`, vanilla HTML/CSS/JavaScript.

---

### Task 1: Web Service Core

**Files:**
- Create: `wechat_crawler/web_app.py`
- Test: `tests/test_web_app.py`

- [ ] Write tests for listing accounts, listing articles, reading article content, and ETA calculation.
- [ ] Run `pytest tests/test_web_app.py -q` and confirm failure because `wechat_crawler.web_app` does not exist.
- [ ] Implement `ArchiveJob`, `ArchiveWebService`, and read-only listing methods.
- [ ] Run `pytest tests/test_web_app.py -q` and confirm pass.

### Task 2: HTTP API

**Files:**
- Modify: `wechat_crawler/web_app.py`

- [ ] Add routes: `GET /api/accounts`, `GET /api/accounts/<id>/articles`, `GET /api/articles/<id>`, `POST /api/jobs`, `GET /api/jobs`, `GET /api/jobs/<id>`.
- [ ] Use JSON responses and never expose signed WeChat URL parameters.
- [ ] Add a `run_server()` function for local startup.

### Task 3: Static UI

**Files:**
- Create: `wechat_crawler/web/static/index.html`
- Create: `wechat_crawler/web/static/app.css`
- Create: `wechat_crawler/web/static/app.js`

- [ ] Build a dense operational UI: URL input, progress bar with ETA, account folder list, article list, article reader.
- [ ] Poll active jobs every two seconds while a job is running.
- [ ] Keep the account folder list as the first-level view so many accounts do not clutter the article list.

### Task 4: CLI and Docs

**Files:**
- Modify: `wechat_crawler/main.py`
- Modify: `README.md`

- [ ] Add `python main.py web --host 127.0.0.1 --port 8765`.
- [ ] Document the local website command, limitations, and browser URL.
- [ ] Run full tests and smoke-test the local server.
