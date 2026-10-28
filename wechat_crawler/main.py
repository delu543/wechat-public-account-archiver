from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from .crawler.discoverers import (
    BrowserDiscoverer,
    SearchConfig,
    SearchDiscoverer,
    UrlDiscoverer,
    WechatContentFlowCacheDiscoverer,
    WechatHistoryDiscoverer,
)
from .crawler.discoverers.history_discoverer import (
    HistoryDiscoveryError,
    is_session_recovery_error,
    session_recovery_guidance,
)
from .crawler.date_range import (
    article_in_range,
    extract_publish_date,
    publish_timestamp_to_date,
    validate_date_range,
)
from .crawler.exporter import MarkdownExporter
from .crawler.fetcher import ArticleFetcher, FetchConfig
from .crawler.logger import setup_logging
from .crawler.models import ParsedArticle
from .crawler.normalizer import is_wechat_article_url
from .crawler.parser import ParseError, WechatArticleParser
from .crawler.storage import Storage

LOG = logging.getLogger(__name__)


@dataclass
class CrawlOutcome:
    article_url_id: int
    status: str
    article: ParsedArticle | None = None
    error: str | None = None


DEFAULT_CONFIG: dict[str, Any] = {
    "database": {"path": "data/archive.sqlite"},
    "output": {"path": "output"},
    "fetch": {
        "timeout_seconds": 20,
        "min_delay_seconds": 2.0,
        "max_delay_seconds": 5.0,
        "max_retries": 2,
    },
    "discoverers": {
        "url": {"enabled": True},
        "search": {"enabled": True, "max_results_per_clue": 10, "min_delay_seconds": 2.0, "max_delay_seconds": 5.0},
        "browser": {"enabled": False},
        "official_api": {"enabled": False},
    },
    "web": {"host": "127.0.0.1", "port": 8765},
    "automation": {
        "enabled": True,
        "weekday": 6,
        "hour": 21,
        "minute": 0,
        "check_interval_seconds": 60,
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        normalize_date_arguments(args)
    except ValueError as exc:
        parser.error(str(exc))
    setup_logging(args.verbose)
    config = load_config(args.config)
    if args.command == "cache-update":
        from .cache_update import command
        return command(args, config)
    if args.command == "weekly-run":
        return cmd_weekly_run(args, config)
    storage = Storage(config["database"]["path"])
    storage.init_db()

    if args.command == "discover":
        return cmd_discover(args, config, storage)
    if args.command == "discover-account":
        return cmd_discover_account(args, config, storage)
    if args.command == "reverse":
        return cmd_reverse(args, config, storage)
    if args.command == "import-urls":
        return cmd_import_urls(args, storage)
    if args.command == "crawl":
        return cmd_crawl(args, config, storage)
    if args.command == "history-sync":
        return cmd_history_sync(args, config, storage)
    if args.command == "history-cache-sync":
        return cmd_history_cache_sync(args, config, storage)
    if args.command == "export":
        return cmd_export(args, config, storage)
    if args.command == "stats":
        return cmd_stats(storage)
    if args.command == "web":
        return cmd_web(args, config, storage)
    if args.command == "automation-adopt-existing":
        return cmd_automation_adopt_existing(config, storage)
    if args.command == "automation-activate-current-baseline":
        return cmd_automation_activate_current_baseline(config, storage)
    if args.command == "automation-status":
        return cmd_automation_status(storage)
    if args.command == "automation-run":
        return cmd_automation_run(args, config, storage)
    if args.command == "automation-prepare-session":
        return cmd_automation_prepare_session(args, config, storage)
    if args.command == "automation-export-pending":
        return cmd_automation_export_pending(args, config, storage)
    if args.command == "automation-cleanup":
        return cmd_automation_cleanup(args, config, storage)
    if args.command == "automation-install-launch-agent":
        return cmd_automation_install_launch_agent(args, config, storage)
    if args.command == "automation-uninstall-launch-agent":
        return cmd_automation_uninstall_launch_agent()
    if args.command == "automation-launch-agent-status":
        return cmd_automation_launch_agent_status()
    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive public WeChat Official Account articles.")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML.")
    parser.add_argument("--verbose", action="store_true", help="Print debug logs.")
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="Discover article URLs from account clues.")
    discover.add_argument("input_file", help="Text file with one clue per line.")
    discover.add_argument("--no-search", action="store_true", help="Disable public search discoverer for this run.")
    discover.add_argument("--browser", action="store_true", help="Enable browser-assisted discovery for public pages.")

    discover_account = sub.add_parser("discover-account", help="Parse one article URL and batch-discover its account history URLs.")
    discover_account.add_argument("--url", required=True, help="Public WeChat article URL used to identify the account.")
    add_history_options(discover_account)

    reverse = sub.add_parser("reverse", help="From article URL(s), identify account, discover history URLs, crawl, and export.")
    reverse.add_argument("input", help="A public WeChat article URL or a text file containing URLs.")
    add_history_options(reverse)
    reverse.add_argument("--no-history", action="store_true", help="Only parse/import the input article URL(s), without history discovery.")
    reverse.add_argument("--no-crawl", action="store_true", help="Do not crawl pending URLs after discovery.")
    reverse.add_argument("--no-export", action="store_true", help="Do not export Markdown after crawl.")
    reverse.add_argument("--crawl-limit", type=int, help="Maximum URLs to crawl per discovered account.")
    reverse.add_argument("--retry-failed", action="store_true", help="Retry failed URLs during the crawl step.")

    import_urls = sub.add_parser("import-urls", help="Import known article URLs from a text file.")
    import_urls.add_argument("input_file", help="Text file with one or more article URLs.")

    crawl = sub.add_parser("crawl", help="Fetch and parse pending article URLs.")
    crawl.add_argument("--account", help="Limit crawl to account name, biz, or account hint.")
    crawl.add_argument("--account-biz", help="Limit crawl to an exact account biz value.")
    crawl.add_argument("--since", help="Only save articles published on or after YYYY-MM-DD.")
    crawl.add_argument("--until", help="Only save articles published on or before YYYY-MM-DD.")
    crawl.add_argument("--limit", type=int, help="Maximum URLs to crawl.")
    crawl.add_argument("--retry-failed", action="store_true", help="Retry URLs marked as failed.")
    crawl.add_argument("--fast", action="store_true", help="Fast first pass: low delay and no retries.")
    crawl.add_argument("--min-delay", type=float, help="Override minimum delay between requests.")
    crawl.add_argument("--max-delay", type=float, help="Override maximum delay between requests.")
    crawl.add_argument("--max-retries", type=int, help="Override fetch retry count.")
    crawl.add_argument("--workers", type=int, default=1, help="Number of concurrent fetch workers for fast first-pass crawling.")

    history = sub.add_parser("history-sync", help="Discover article URLs through the visible WeChat account history session.")
    history.add_argument("--biz", required=True, help="Target account biz, for example MzA3OTQzNjk1MA==.")
    history.add_argument("--wechat-id", required=True, help="Target WeChat ID, for example gh_69e1edf8c365.")
    history.add_argument("--account-name", required=True, help="Human-readable account name.")
    add_history_options(history)
    history.add_argument("--crawl", action="store_true", help="Crawl discovered URLs after discovery.")
    history.add_argument("--export", action="store_true", help="Export Markdown after discovery/crawl.")

    cache_history = sub.add_parser(
        "history-cache-sync",
        help="Import articles from the visible WeChat account history content-flow cache.",
    )
    cache_history.add_argument("--biz", required=True, help="Target account biz.")
    cache_history.add_argument("--wechat-id", required=True, help="Target WeChat ID, for example gh_69e1edf8c365.")
    cache_history.add_argument("--account-name", required=True, help="Human-readable account name.")
    cache_history.add_argument(
        "--app-data-dir",
        default="~/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data",
        help="Read-only local WeChat app data directory containing the visible history page cache.",
    )
    cache_history.add_argument("--target-count", type=int, help="Stop when this account has at least this many articles.")
    cache_history.add_argument("--max-rounds", type=int, default=1, help="Maximum decode/scroll rounds.")
    cache_history.add_argument(
        "--page-downs-per-round",
        type=int,
        default=0,
        help="After each decode round, send this many scroll events to the visible WeChat history window.",
    )
    cache_history.add_argument(
        "--page-down-interval",
        type=float,
        default=0.15,
        help="Delay between PageDown keys while loading more public history items.",
    )
    cache_history.add_argument(
        "--settle-seconds",
        type=float,
        default=2.0,
        help="Seconds to wait after scrolling before decoding the cache again.",
    )
    cache_history.add_argument(
        "--max-cache-file-mb",
        type=int,
        default=120,
        help="Maximum local WeChat content-flow cache file size to decode.",
    )
    cache_history.add_argument("--export", action="store_true", help="Export Markdown after cache import.")
    cache_history.add_argument("--force-export", action="store_true", help="Rewrite existing Markdown files during export.")

    export = sub.add_parser("export", help="Export parsed articles as Markdown.")
    export.add_argument("--force", action="store_true", help="Rewrite existing Markdown files.")
    export.add_argument("--account-biz", help="Export only one account by exact biz.")

    sub.add_parser("stats", help="Print archive statistics.")

    web = sub.add_parser("web", help="Start the local archive website.")
    web.add_argument("--host", help="Host to bind, default from config or 127.0.0.1.")
    web.add_argument("--port", type=int, help="Port to bind, default from config or 8765.")

    sub.add_parser(
        "automation-adopt-existing",
        help="Adopt current account articles as the no-repeat automation baseline.",
    )
    sub.add_parser(
        "automation-activate-current-baseline",
        help="Use every currently imported article as the baseline and enable weekly automation.",
    )
    sub.add_parser("automation-status", help="Print persistent subscription and weekly job status.")

    automation_run = sub.add_parser("automation-run", help="Run one or all subscriptions immediately.")
    automation_run.add_argument("--account-biz", help="Run only the exact account biz; default runs all enabled accounts.")
    automation_run.add_argument(
        "--resume-needs-session",
        action="store_true",
        help="Resume the current scheduled job instead of creating a separate manual cycle.",
    )

    cache = sub.add_parser("cache-update", help="Passive retained cache → de-duplication → discovered Word; default is read-only.")
    from .cache_update import add_arguments
    add_arguments(cache)

    passive = sub.add_parser("weekly-run", help="Experimental bound biz-push increment; no automatic setup. For retained cache use cache-update.")
    passive.add_argument("--until", help="Inclusive publication end date; default today.")
    passive.add_argument("--account-biz", help="Limit to one exact enabled subscription.")
    checks = passive.add_mutually_exclusive_group()
    checks.add_argument("--plan-only", action="store_true", help="Read product boundaries only; no WeChat/helper/network/writes.")
    checks.add_argument("--check", action="store_true", help="Read-only helper/coverage health check; no public requests or Word.")
    passive.add_argument("--allow-partial", action="store_true", help="Explicitly export available bodies without advancing incomplete checkpoints.")
    passive.add_argument("--search-gaps", action="store_true", default=True, help="One ordinary PC query/account (default); never proves completeness.")
    passive.add_argument("--no-search", dest="search_gaps", action="store_false", help="Skip optional PC cross-check for this run.")
    passive.add_argument("--require-complete", action="store_true", help="Keep strict full-publication acceptance; otherwise export explicitly labelled observed increments.")
    passive.add_argument("--source", choices=("auto", "cache", "biz"), default="auto",
                         help="auto (default): bound biz DB or one setup notice; cache: explicit manual-refresh fallback; never automatic setup.")

    automation_prepare = sub.add_parser(
        "automation-prepare-session",
        help="Open one saved public seed in visible WeChat without reading WeChat app data.",
    )
    automation_prepare.add_argument(
        "--account-biz",
        required=True,
        help="Exact account biz whose saved public seed should be opened.",
    )

    automation_export = sub.add_parser(
        "automation-export-pending",
        help="Export already-captured, not-yet-exported articles without rerunning discovery.",
    )
    automation_export.add_argument("--account-biz", required=True, help="Exact account biz to export.")

    automation_cleanup = sub.add_parser(
        "automation-cleanup",
        help="Preview or remove reproducible raw HTML and product temporary files.",
    )
    automation_cleanup.add_argument("--execute", action="store_true", help="Apply cleanup; default is a dry run.")

    install_agent = sub.add_parser(
        "automation-install-launch-agent",
        help="Install and start the macOS login LaunchAgent.",
    )
    install_agent.add_argument("--host", help="Local web host; default from config.")
    install_agent.add_argument("--port", type=int, help="Local web port; default from config.")
    sub.add_parser("automation-uninstall-launch-agent", help="Stop and remove the macOS LaunchAgent.")
    sub.add_parser("automation-launch-agent-status", help="Print macOS LaunchAgent status.")
    return parser


def add_history_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--app-data-dir",
        default="~/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data",
        help="Read-only local WeChat app data directory used to find a visible public history session.",
    )
    parser.add_argument("--count", type=int, default=10, help="History page size.")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between history page requests.")
    parser.add_argument("--max-pages", type=int, help="Stop after this many history pages.")
    parser.add_argument("--max-items", type=int, help="Stop after this many discovered article URLs.")
    parser.add_argument("--start-offset", type=int, default=0, help="Resume history discovery from this profile_ext offset.")
    parser.add_argument("--since", help="Only discover and crawl articles published on or after YYYY-MM-DD.")
    parser.add_argument("--until", help="Only discover and crawl articles published on or before YYYY-MM-DD.")


def normalize_date_arguments(args: argparse.Namespace) -> None:
    if not hasattr(args, "since") and not hasattr(args, "until"):
        return
    since, until = validate_date_range(getattr(args, "since", None), getattr(args, "until", None))
    if hasattr(args, "since"):
        args.since = since
    if hasattr(args, "until"):
        args.until = until


def load_config(path: str | Path) -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config_path = Path(path)
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        _deep_update(config, loaded)
    for section in ("database", "output"):
        value = config.get(section, {}).get("path")
        if value:
            config[section]["path"] = str(Path(os.path.expandvars(str(value))).expanduser())
    return config


def cmd_weekly_run(args, config):
    from datetime import datetime
    from .passive_weekly import PassiveWeeklyRunner
    from .weekly_source_router import WeeklySourceRouter, public_source_health

    settings = config.get("passive_weekly", {})
    mode = getattr(args, "source", "auto")
    runner = PassiveWeeklyRunner(config["database"]["path"], config["output"]["path"], settings,
                                 sources=WeeklySourceRouter(settings, mode))
    until = args.until or datetime.now().astimezone().date().isoformat()
    try:
        if args.plan_only or args.check:
            result = runner.inspect(until, biz=args.account_biz, source_check=args.check)
        else:
            result = runner.run(until, biz=args.account_biz, allow_partial=args.allow_partial,
                                search_gaps=args.search_gaps, observed=not args.require_complete)
    except Exception:
        result = {"status": "blocked", "reason": "weekly_pipeline_local_check_failed"}
    public_source_health(result, mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("plan_only", "completed", "observed_completed") or (
        result["status"] == "health_checked" and result.get("source_status") == "read") else 2


def cmd_discover(args: argparse.Namespace, config: dict[str, Any], storage: Storage) -> int:
    clues = read_input_lines(args.input_file)
    run_id = storage.start_run("discover", notes=f"input={args.input_file}")
    discoverers = [UrlDiscoverer()]
    search_cfg = config["discoverers"].get("search", {})
    if search_cfg.get("enabled", True) and not args.no_search:
        discoverers.append(
            SearchDiscoverer(
                SearchConfig(
                    max_results_per_clue=int(search_cfg.get("max_results_per_clue", 10)),
                    min_delay_seconds=float(search_cfg.get("min_delay_seconds", 2.0)),
                    max_delay_seconds=float(search_cfg.get("max_delay_seconds", 5.0)),
                    timeout_seconds=float(search_cfg.get("timeout_seconds", 20.0)),
                    user_agent=config.get("fetch", {}).get("user_agent", FetchConfig.user_agent),
                )
            )
        )
    browser_enabled = bool(args.browser or config["discoverers"].get("browser", {}).get("enabled", False))
    discoverers.append(BrowserDiscoverer(enabled=browser_enabled))

    discovered_count = 0
    try:
        for clue in clues:
            clue_count = 0
            for discoverer in discoverers:
                for item in discoverer.discover(clue):
                    if item.status == "discovered":
                        storage.upsert_article_url(
                            item.url,
                            discoverer=item.discoverer,
                            run_id=run_id,
                            source_clue=item.source_clue,
                            account_hint=item.account_hint,
                        )
                        discovered_count += 1
                        clue_count += 1
                    else:
                        storage.log(
                            "warning",
                            "discover_skipped",
                            item.error_message or item.status,
                            run_id=run_id,
                        )
            if clue_count == 0:
                storage.log("info", "discover_no_urls", f"no public article URLs found for clue: {clue}", run_id)
        storage.finish_run(run_id, "completed", notes=f"discovered={discovered_count}")
    except Exception as exc:
        storage.finish_run(run_id, "failed", notes=str(exc))
        raise
    print(f"discovered {discovered_count} article URL(s)")
    return 0


def cmd_import_urls(args: argparse.Namespace, storage: Storage) -> int:
    clues = read_input_lines(args.input_file)
    run_id = storage.start_run("import-urls", notes=f"input={args.input_file}")
    discoverer = UrlDiscoverer()
    count = 0
    for clue in clues:
        items = discoverer.discover(clue)
        if not items:
            storage.log("warning", "import_url_skipped", f"no supported article URL in line: {clue}", run_id)
        for item in items:
            storage.upsert_article_url(item.url, discoverer=item.discoverer, run_id=run_id, source_clue=clue)
            count += 1
    storage.finish_run(run_id, "completed", notes=f"imported={count}")
    print(f"imported {count} article URL(s)")
    return 0


def cmd_discover_account(args: argparse.Namespace, config: dict[str, Any], storage: Storage) -> int:
    run_id = storage.start_run("discover-account-input", notes=f"input_url={args.url}")
    try:
        article = parse_entry_article(
            args.url,
            config,
            storage,
            run_id,
            discoverer="discover_account_input",
            since=args.since,
            until=args.until,
        )
        storage.finish_run(run_id, "completed", notes=f"account={article.account_name} biz={article.account_id or ''}")
    except (ParseError, RuntimeError) as exc:
        storage.finish_run(run_id, "failed", notes=str(exc))
        print(f"discover-account failed: {exc}")
        return 2

    if not article.account_id:
        storage.log("warning", "history_blocked", "input article did not expose __biz", run_id=run_id)
        print(f"account={article.account_name} biz= unavailable; history discovery skipped")
        return 2

    account_id = storage.upsert_account(
        article.account_name,
        biz=article.account_id,
        wechat_id=article.wechat_id,
        source_url=article.source_url,
    )
    try:
        pages, discovered = discover_history_for_account(
            storage=storage,
            mode="discover-account",
            account_id=account_id,
            account_name=article.account_name,
            biz=article.account_id,
            wechat_id=article.wechat_id,
            args=args,
        )
    except HistoryDiscoveryError as exc:
        storage.log("warning", "history_blocked", str(exc), account_id=account_id)
        print(f"account={article.account_name} biz={article.account_id} history discovery blocked: {exc}")
        if is_session_recovery_error(exc):
            print(session_recovery_guidance(article.account_name))
        return 2

    print(
        f"account={article.account_name} biz={article.account_id} "
        f"wechat_id={article.wechat_id or ''} history pages={pages} discovered={discovered}"
    )
    return 0


def cmd_reverse(args: argparse.Namespace, config: dict[str, Any], storage: Storage) -> int:
    clues = read_input_values(args.input)
    url_items: list[str] = []
    url_discoverer = UrlDiscoverer()
    for clue in clues:
        items = url_discoverer.discover(clue)
        url_items.extend(item.url for item in items if item.status == "discovered")
        if not items and is_wechat_article_url(clue):
            url_items.append(clue)
    if not url_items:
        print(f"no supported WeChat article URL found in input: {args.input}")
        return 2

    account_targets: dict[str, tuple[int, str, str | None]] = {}
    parsed_count = 0
    discovered_total = 0
    blocked = 0

    for url in url_items:
        run_id = storage.start_run("reverse-input", notes=f"input_url={url}")
        try:
            article = parse_entry_article(
                url,
                config,
                storage,
                run_id,
                discoverer="reverse_input",
                since=args.since,
                until=args.until,
            )
            storage.finish_run(run_id, "completed", notes=f"account={article.account_name} biz={article.account_id or ''}")
            parsed_count += 1
        except (ParseError, RuntimeError) as exc:
            storage.finish_run(run_id, "failed", notes=str(exc))
            storage.log("error", "reverse_input_failed", str(exc), run_id=run_id)
            blocked += 1
            continue

        if not article.account_id:
            storage.log("warning", "history_blocked", "input article did not expose __biz", run_id=run_id)
            blocked += 1
            continue

        account_id = storage.upsert_account(
            article.account_name,
            biz=article.account_id,
            wechat_id=article.wechat_id,
            source_url=article.source_url,
        )
        account_targets[article.account_id] = (account_id, article.account_name, article.wechat_id)

        if args.no_history:
            continue

        try:
            pages, discovered = discover_history_for_account(
                storage=storage,
                mode="reverse-discover",
                account_id=account_id,
                account_name=article.account_name,
                biz=article.account_id,
                wechat_id=article.wechat_id,
                args=args,
            )
            discovered_total += discovered
            print(f"reverse discovered account={article.account_name} pages={pages} urls={discovered}")
        except HistoryDiscoveryError as exc:
            blocked += 1
            storage.log("warning", "history_blocked", str(exc), account_id=account_id)
            print(f"history discovery blocked for {article.account_name}: {exc}")
            if is_session_recovery_error(exc):
                print(session_recovery_guidance(article.account_name))

    crawl_exit = 0
    if not args.no_crawl:
        for biz in account_targets:
            crawl_args = argparse.Namespace(
                account=None,
                account_biz=biz,
                since=args.since,
                until=args.until,
                limit=args.crawl_limit,
                retry_failed=args.retry_failed,
            )
            crawl_exit = max(crawl_exit, cmd_crawl(crawl_args, config, storage))

    if not args.no_export:
        export_args = argparse.Namespace(force=True)
        cmd_export(export_args, config, storage)

    print(
        f"reverse parsed={parsed_count} discovered={discovered_total} "
        f"accounts={len(account_targets)} blocked={blocked}"
    )
    return 2 if blocked else crawl_exit


def cmd_crawl(args: argparse.Namespace, config: dict[str, Any], storage: Storage) -> int:
    fetcher = build_fetcher(config, args)
    parser = WechatArticleParser()
    account_filter = getattr(args, "account_biz", None) or getattr(args, "account", None)
    rows = storage.pending_urls(
        account=account_filter,
        limit=args.limit,
        retry_failed=args.retry_failed,
        since=getattr(args, "since", None),
        until=getattr(args, "until", None),
    )
    run_id = storage.start_run(
        "crawl",
        notes=(
            f"account={account_filter or '*'} limit={args.limit or ''} "
            f"since={getattr(args, 'since', None) or ''} until={getattr(args, 'until', None) or ''}"
        ),
    )
    crawled = 0
    failed = 0
    restricted = 0
    skipped = 0
    try:
        workers = max(1, int(getattr(args, "workers", 1) or 1))
        if workers > 1:
            for outcome in crawl_rows_parallel(rows, args, config, workers):
                article_url_id = outcome.article_url_id
                if outcome.status == "restricted":
                    restricted += 1
                    storage.mark_url_status(article_url_id, "restricted", outcome.error)
                    storage.log("warning", "restricted", outcome.error or "restricted", run_id, article_url_id=article_url_id)
                elif outcome.status == "failed":
                    failed += 1
                    storage.mark_url_status(article_url_id, "failed", outcome.error)
                    storage.log("error", "fetch_failed", outcome.error or "fetch failed", run_id, article_url_id=article_url_id)
                elif outcome.status == "parse_failed":
                    failed += 1
                    storage.mark_url_status(article_url_id, "failed", outcome.error)
                    storage.log("error", "parse_failed", outcome.error or "parse failed", run_id, article_url_id=article_url_id)
                elif outcome.status == "mismatch":
                    failed += 1
                    storage.mark_url_status(article_url_id, "mismatch", outcome.error)
                    storage.log("error", "account_mismatch", outcome.error or "account mismatch", run_id, article_url_id=article_url_id)
                elif outcome.status == "skipped":
                    skipped += 1
                    storage.update_article_url_metadata(
                        article_url_id,
                        publish_date_hint=extract_publish_date(outcome.article.publish_time if outcome.article else None) or "",
                    )
                    storage.mark_url_status(article_url_id, "out_of_range", outcome.error)
                    storage.log("info", "crawl_skipped_out_of_range", outcome.error or "", run_id, article_url_id=article_url_id)
                elif outcome.article is not None:
                    storage.save_article(outcome.article, article_url_id=article_url_id, run_id=run_id)
                    crawled += 1
            storage.finish_run(
                run_id,
                "completed",
                notes=f"crawled={crawled} failed={failed} restricted={restricted} skipped={skipped}",
            )
            print(f"crawled={crawled} failed={failed} restricted={restricted} skipped={skipped}")
            return 0 if failed == 0 else 2

        for row in rows:
            article_url_id = int(row["id"])
            url = row["source_url"]
            result = fetcher.fetch(url)
            if result.status == "restricted":
                restricted += 1
                storage.mark_url_status(article_url_id, "restricted", result.error_message)
                storage.log("warning", "restricted", result.error_message or "restricted", run_id, article_url_id=article_url_id)
                continue
            if result.status != "ok":
                failed += 1
                storage.mark_url_status(article_url_id, "failed", result.error_message)
                storage.log("error", "fetch_failed", result.error_message or "fetch failed", run_id, article_url_id=article_url_id)
                continue
            try:
                article = parser.parse(result.text, result.final_url or url)
            except ParseError as exc:
                failed += 1
                storage.mark_url_status(article_url_id, "failed", str(exc))
                storage.log("error", "parse_failed", str(exc), run_id, article_url_id=article_url_id)
                continue
            if not article_matches_account_target(article, args):
                failed += 1
                expected = getattr(args, "account_biz", None) or getattr(args, "account", None) or ""
                actual = article.account_id or article.account_name or ""
                message = f"account mismatch: expected {expected}, got {actual}"
                storage.mark_url_status(article_url_id, "mismatch", message)
                storage.log("error", "account_mismatch", message, run_id, article_url_id=article_url_id)
                continue
            if not article_in_range(article.publish_time, getattr(args, "since", None), getattr(args, "until", None)):
                skipped += 1
                storage.update_article_url_metadata(
                    article_url_id,
                    publish_date_hint=extract_publish_date(article.publish_time) or "",
                )
                storage.mark_url_status(article_url_id, "out_of_range", f"publish_time={article.publish_time or ''}")
                storage.log(
                    "info",
                    "crawl_skipped_out_of_range",
                    f"publish_time={article.publish_time or ''}",
                    run_id,
                    article_url_id=article_url_id,
                )
                continue
            storage.save_article(article, article_url_id=article_url_id, run_id=run_id)
            crawled += 1
        storage.finish_run(
            run_id,
            "completed",
            notes=f"crawled={crawled} failed={failed} restricted={restricted} skipped={skipped}",
        )
    except Exception as exc:
        storage.finish_run(run_id, "failed", notes=str(exc))
        raise
    except KeyboardInterrupt:
        storage.finish_run(run_id, "interrupted", notes="keyboard interrupt")
        raise
    print(f"crawled={crawled} failed={failed} restricted={restricted} skipped={skipped}")
    return 0 if failed == 0 else 2


def crawl_rows_parallel(
    rows: list[Any],
    args: argparse.Namespace,
    config: dict[str, Any],
    workers: int,
):
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(crawl_one_row, row, args, config) for row in rows]
        for future in as_completed(futures):
            yield future.result()


def crawl_one_row(row: Any, args: argparse.Namespace, config: dict[str, Any]) -> CrawlOutcome:
    article_url_id = int(row["id"])
    url = row["source_url"]
    result = build_fetcher(config, args).fetch(url)
    if result.status == "restricted":
        return CrawlOutcome(article_url_id, "restricted", error=result.error_message)
    if result.status != "ok":
        return CrawlOutcome(article_url_id, "failed", error=result.error_message)
    try:
        article = WechatArticleParser().parse(result.text, result.final_url or url)
    except ParseError as exc:
        return CrawlOutcome(article_url_id, "parse_failed", error=str(exc))
    if not article_matches_account_target(article, args):
        expected = getattr(args, "account_biz", None) or getattr(args, "account", None) or ""
        actual = article.account_id or article.account_name or ""
        return CrawlOutcome(article_url_id, "mismatch", error=f"account mismatch: expected {expected}, got {actual}")
    if not article_in_range(article.publish_time, getattr(args, "since", None), getattr(args, "until", None)):
        return CrawlOutcome(article_url_id, "skipped", article=article, error=f"publish_time={article.publish_time or ''}")
    return CrawlOutcome(article_url_id, "ok", article=article)


def build_fetcher(config: dict[str, Any], args: argparse.Namespace | None = None) -> ArticleFetcher:
    return ArticleFetcher(fetch_config_from_args(config, args))


def fetch_config_from_args(config: dict[str, Any], args: argparse.Namespace | None = None) -> FetchConfig:
    fetch_cfg = config.get("fetch", {})
    min_delay = float(fetch_cfg.get("min_delay_seconds", 2.0))
    max_delay = float(fetch_cfg.get("max_delay_seconds", 5.0))
    max_retries = int(fetch_cfg.get("max_retries", 2))
    if args and getattr(args, "fast", False):
        min_delay = 0.15
        max_delay = 0.45
        max_retries = 0
    if args and getattr(args, "min_delay", None) is not None:
        min_delay = float(args.min_delay)
    if args and getattr(args, "max_delay", None) is not None:
        max_delay = float(args.max_delay)
    if args and getattr(args, "max_retries", None) is not None:
        max_retries = int(args.max_retries)
    return FetchConfig(
        timeout_seconds=float(fetch_cfg.get("timeout_seconds", 20.0)),
        min_delay_seconds=min_delay,
        max_delay_seconds=max_delay,
        max_retries=max_retries,
        user_agent=fetch_cfg.get("user_agent", FetchConfig.user_agent),
    )


def parse_entry_article(
    url: str,
    config: dict[str, Any],
    storage: Storage,
    run_id: int,
    discoverer: str,
    since: str | None = None,
    until: str | None = None,
) -> ParsedArticle:
    article_url_id = storage.upsert_article_url(url, discoverer=discoverer, run_id=run_id, source_clue=url)
    result = build_fetcher(config).fetch(url)
    if result.status == "restricted":
        storage.mark_url_status(article_url_id, "restricted", result.error_message)
        storage.log("warning", "restricted", result.error_message or "restricted", run_id, article_url_id=article_url_id)
        raise RuntimeError(result.error_message or "input article restricted")
    if result.status != "ok":
        storage.mark_url_status(article_url_id, "failed", result.error_message)
        storage.log("error", "fetch_failed", result.error_message or "fetch failed", run_id, article_url_id=article_url_id)
        raise RuntimeError(result.error_message or "input article fetch failed")

    try:
        article = WechatArticleParser().parse(result.text, result.final_url or url)
    except ParseError as exc:
        storage.mark_url_status(article_url_id, "failed", str(exc))
        storage.log("error", "parse_failed", str(exc), run_id, article_url_id=article_url_id)
        raise
    if article_in_range(article.publish_time, since, until):
        storage.save_article(article, article_url_id=article_url_id, run_id=run_id)
    else:
        account_id = storage.upsert_account(
            article.account_name,
            biz=article.account_id,
            wechat_id=article.wechat_id,
            source_url=article.source_url,
        )
        storage.update_article_url_metadata(
            article_url_id,
            account_id=account_id,
            publish_date_hint=extract_publish_date(article.publish_time) or "",
        )
        storage.mark_url_status(article_url_id, "out_of_range", f"publish_time={article.publish_time or ''}")
        storage.log(
            "info",
            "entry_article_out_of_range",
            f"publish_time={article.publish_time or ''}",
            run_id=run_id,
            account_id=account_id,
            article_url_id=article_url_id,
        )
    return article


def discover_history_for_account(
    storage: Storage,
    mode: str,
    account_id: int,
    account_name: str,
    biz: str,
    wechat_id: str | None,
    args: argparse.Namespace,
) -> tuple[int, int]:
    since = getattr(args, "since", None)
    until = getattr(args, "until", None)
    run_id = storage.start_run(
        mode,
        notes=f"biz={biz} wechat_id={wechat_id or ''} since={since or ''} until={until or ''}",
    )
    discoverer = WechatHistoryDiscoverer(
        args.app_data_dir,
        biz=biz,
        wechat_id=wechat_id or "",
        delay_seconds=args.delay,
        count=args.count,
    )
    discovered = 0
    pages = 0
    try:
        for offset, items, page in discoverer.discover_pages(
            max_pages=args.max_pages,
            max_items=args.max_items,
            start_offset=getattr(args, "start_offset", 0) or 0,
            since_date=since,
            until_date=until,
        ):
            pages += 1
            for item in items:
                storage.upsert_article_url(
                    item.public_url,
                    discoverer=discoverer.name,
                    run_id=run_id,
                    source_clue=f"history:{wechat_id or biz}:offset:{offset}",
                    account_hint=account_name,
                    account_id=account_id,
                    publish_date_hint=publish_timestamp_to_date(item.publish_timestamp),
                )
                discovered += 1
            storage.log(
                "info",
                "history_page",
                f"offset={offset} items={len(items)} next_offset={page.next_offset} can_continue={page.can_continue}",
                run_id=run_id,
                account_id=account_id,
            )
        storage.finish_run(run_id, "completed", notes=f"pages={pages} discovered={discovered}")
    except HistoryDiscoveryError as exc:
        storage.finish_run(run_id, "blocked", notes=str(exc))
        storage.log("warning", "history_blocked", str(exc), run_id=run_id, account_id=account_id)
        raise
    except Exception as exc:
        storage.finish_run(run_id, "failed", notes=str(exc))
        raise
    return pages, discovered


def article_matches_account_target(article: ParsedArticle, args: argparse.Namespace) -> bool:
    account_biz = getattr(args, "account_biz", None)
    if account_biz:
        return article.account_id == account_biz
    account = getattr(args, "account", None)
    if not account:
        return True
    return account in {article.account_name, article.account_id, article.wechat_id}


def cmd_history_sync(args: argparse.Namespace, config: dict[str, Any], storage: Storage) -> int:
    account_id = storage.upsert_account(
        args.account_name,
        biz=args.biz,
        wechat_id=args.wechat_id,
        source_url="mp.weixin.qq.com",
    )
    try:
        pages, discovered = discover_history_for_account(
            storage=storage,
            mode="history-sync",
            account_id=account_id,
            account_name=args.account_name,
            biz=args.biz,
            wechat_id=args.wechat_id,
            args=args,
        )
    except HistoryDiscoveryError as exc:
        print(f"history discovery blocked: {exc}")
        if is_session_recovery_error(exc):
            print(session_recovery_guidance(args.account_name))
        return 2

    print(f"history pages={pages} discovered={discovered} article URL(s)")
    exit_code = 0
    if args.crawl:
        crawl_args = argparse.Namespace(
            account=None,
            account_biz=args.biz,
            since=args.since,
            until=args.until,
            limit=None,
            retry_failed=False,
        )
        exit_code = cmd_crawl(crawl_args, config, storage)
    if args.export:
        export_args = argparse.Namespace(force=True)
        cmd_export(export_args, config, storage)
    return exit_code


def cmd_history_cache_sync(args: argparse.Namespace, config: dict[str, Any], storage: Storage) -> int:
    account_id = storage.upsert_account(
        args.account_name,
        biz=args.biz,
        wechat_id=args.wechat_id,
        source_url="mp.weixin.qq.com",
    )
    run_id = storage.start_run(
        "history-cache-sync",
        notes=(
            f"biz={args.biz} wechat_id={args.wechat_id} target_count={args.target_count or ''} "
            f"rounds={args.max_rounds} page_downs={args.page_downs_per_round}"
        ),
    )
    discoverer = WechatContentFlowCacheDiscoverer(
        args.app_data_dir,
        target_biz=args.biz,
        target_wechat_id=args.wechat_id,
        max_file_bytes=max(1, int(args.max_cache_file_mb)) * 1024 * 1024,
    )
    start_count = storage.article_count_for_account(account_id)
    final_count = start_count
    imported_this_run = 0
    rounds = max(1, int(args.max_rounds or 1))
    try:
        for round_number in range(1, rounds + 1):
            before_round_count = storage.article_count_for_account(account_id)
            articles = discoverer.discover()
            for article in articles:
                article_url_id = storage.upsert_article_url(
                    article.source_url,
                    discoverer=discoverer.name,
                    run_id=run_id,
                    source_clue=f"content-flow-cache:round:{round_number}",
                    account_hint=args.account_name,
                    account_id=account_id,
                )
                storage.save_article(article, article_url_id=article_url_id, run_id=run_id)
            final_count = storage.article_count_for_account(account_id)
            imported_this_run = max(0, final_count - start_count)
            added_round = max(0, final_count - before_round_count)
            stats = discoverer.last_stats
            storage.log(
                "info",
                "history_cache_round",
                (
                    f"round={round_number} files={stats.files_read} batches={stats.decoded_batches} "
                    f"items={stats.decoded_items} matched={stats.matched_articles} "
                    f"added={added_round} total={final_count}"
                ),
                run_id=run_id,
                account_id=account_id,
            )
            print(
                f"round={round_number}/{rounds} cache_matched={stats.matched_articles} "
                f"added={added_round} account_total={final_count}"
            )
            if args.target_count and final_count >= args.target_count:
                break
            if round_number == rounds or args.page_downs_per_round <= 0:
                break
            if not scroll_wechat_history(args.page_downs_per_round, args.page_down_interval):
                storage.log(
                    "warning",
                    "history_cache_scroll_failed",
                    "failed to scroll WeChat history window; imported already-loaded cache only",
                    run_id=run_id,
                    account_id=account_id,
                )
                break
            time.sleep(max(0.0, float(args.settle_seconds)))
        status = "completed" if final_count > start_count or final_count > 0 else "blocked"
        storage.finish_run(
            run_id,
            status,
            notes=f"account_total={final_count} imported_this_run={imported_this_run} target_reached={bool(args.target_count and final_count >= args.target_count)}",
        )
    except Exception as exc:
        storage.finish_run(run_id, "failed", notes=str(exc))
        raise

    if args.export or args.force_export:
        output_dir = config.get("output", {}).get("path", "output")
        exported = MarkdownExporter(storage, output_dir).export_all(
            force=bool(args.force_export or args.export),
            account_id=account_id,
        )
        print(f"exported {exported} article file(s) for {args.account_name} to {output_dir}")
    print(
        f"history-cache-sync account={args.account_name} imported_this_run={imported_this_run} "
        f"account_total={final_count}"
    )
    return 0 if final_count > start_count or final_count > 0 else 2


def scroll_wechat_history(scroll_events: int, interval: float) -> bool:
    if scroll_events <= 0:
        return True
    if scroll_wechat_history_with_wheel(scroll_events, interval):
        return True
    return page_down_wechat_history(scroll_events, interval)


def scroll_wechat_history_with_wheel(scroll_events: int, interval: float) -> bool:
    try:
        import ctypes
    except ImportError:
        return False
    try:
        bounds = wechat_history_window_bounds()
        subprocess.run(["osascript", "-e", 'tell application "微信" to activate'], check=False, capture_output=True)
        time.sleep(0.3)
        app = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

        class CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        x, y, width, height = bounds
        point = CGPoint(float(x + width * 0.50), float(y + height * 0.58))
        app.CGWarpMouseCursorPosition.argtypes = [CGPoint]
        app.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        app.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
        app.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
        app.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        app.CGWarpMouseCursorPosition(point)
        time.sleep(0.1)
        for event_type in (1, 2):
            event = app.CGEventCreateMouseEvent(None, event_type, point, 0)
            if not event:
                return False
            app.CGEventPost(0, event)
            cf.CFRelease(event)
            time.sleep(0.03)
        for _ in range(int(scroll_events)):
            event = app.CGEventCreateScrollWheelEvent(None, 1, 1, -8)
            if not event:
                return False
            app.CGEventPost(0, event)
            cf.CFRelease(event)
            time.sleep(max(0.01, float(interval)))
    except Exception:
        return False
    return True


def wechat_history_window_bounds() -> tuple[int, int, int, int]:
    script = '''
tell application "System Events"
    set wechatProcess to first application process whose bundle identifier is "com.tencent.xinWeChat"
    tell wechatProcess to get {position, size} of window "公众号"
end tell
'''
    result = subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=5)
    values = [int(item) for item in re.findall(r"-?\d+", result.stdout)]
    if len(values) < 4:
        raise RuntimeError("unable to read WeChat history window bounds")
    return values[0], values[1], values[2], values[3]


def page_down_wechat_history(page_downs: int, interval: float) -> bool:
    if page_downs <= 0:
        return True
    interval = max(0.05, float(interval))
    script = f"""
tell application id "com.tencent.xinWeChat" to activate
tell application "System Events"
    set wechatProcess to first application process whose bundle identifier is "com.tencent.xinWeChat"
    tell wechatProcess
        set frontmost to true
        repeat {int(page_downs)} times
            key code 121
            delay {interval:.3f}
        end repeat
    end tell
end tell
"""
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=page_downs * interval + 30)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def cmd_export(args: argparse.Namespace, config: dict[str, Any], storage: Storage) -> int:
    output_dir = config.get("output", {}).get("path", "output")
    account_id = None
    if getattr(args, "account_biz", None):
        account_id = storage.account_id_by_biz(args.account_biz)
        if account_id is None:
            print(f"account not found for biz={args.account_biz}")
            return 2
    count = MarkdownExporter(storage, output_dir).export_all(force=args.force, account_id=account_id)
    print(f"exported {count} article file(s) to {output_dir}")
    return 0


def cmd_stats(storage: Storage) -> int:
    print(json.dumps(storage.stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_web(args: argparse.Namespace, config: dict[str, Any], storage: Storage) -> int:
    from .web_app import run_server

    output_dir = config.get("output", {}).get("path", "output")
    web_cfg = config.get("web", {})
    host = args.host or web_cfg.get("host", "127.0.0.1")
    port = int(args.port or web_cfg.get("port", 8765))
    run_server(storage, output_dir, config=config, host=host, port=port)
    return 0


def cmd_automation_adopt_existing(config: dict[str, Any], storage: Storage) -> int:
    from .automation_store import AutomationStore

    automation_cfg = config.get("automation", {})
    automation = AutomationStore(storage)
    automation.init_db()
    adopted = automation.adopt_existing_accounts(
        schedule_weekday=int(automation_cfg.get("weekday", 6)),
        schedule_hour=int(automation_cfg.get("hour", 21)),
        schedule_minute=int(automation_cfg.get("minute", 0)),
    )
    print(json.dumps({"adopted": len(adopted), "subscription_ids": adopted}, ensure_ascii=False, indent=2))
    return 0


def cmd_automation_activate_current_baseline(
    config: dict[str, Any],
    storage: Storage,
) -> int:
    from .automation_store import AutomationStore

    automation_cfg = config.get("automation", {})
    automation = AutomationStore(storage)
    automation.init_db()
    activated = automation.activate_current_baseline(
        schedule_weekday=int(automation_cfg.get("weekday", 6)),
        schedule_hour=int(automation_cfg.get("hour", 21)),
        schedule_minute=int(automation_cfg.get("minute", 0)),
        enabled=True,
    )
    print(
        json.dumps(
            {
                "activated": len(activated),
                "baseline_articles": sum(row["baseline_article_count"] for row in activated),
                "subscriptions": activated,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_automation_status(storage: Storage) -> int:
    from .automation_store import AutomationStore

    automation = AutomationStore(storage)
    automation.init_db()
    subscriptions = [
        {
            "id": int(row["id"]),
            "account_name": row["account_name"],
            "biz": row["biz"],
            "enabled": bool(row["enabled"]),
            "mode": row["onboarding_mode"],
            "article_count": int(row["article_count"] or 0),
            "pending_export_count": int(row["pending_export_count"] or 0),
            "latest_publish_time": row["latest_publish_time"],
            "last_success_at": row["last_success_at"],
            "next_run_at": row["next_run_at"],
            "schedule_weekday": int(row["schedule_weekday"]),
            "schedule_hour": int(row["schedule_hour"]),
            "schedule_minute": int(row["schedule_minute"]),
            "state": row["state"],
        }
        for row in automation.list_subscriptions()
    ]
    jobs = [dict(row) for row in automation.list_jobs(limit=20)]
    latest_completion = automation.latest_completed_cycle()
    print(
        json.dumps(
            {
                "database": str(storage.db_path.resolve()),
                "subscriptions": subscriptions,
                "latest_completion": dict(latest_completion) if latest_completion else None,
                "jobs": jobs,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_automation_run(args: argparse.Namespace, config: dict[str, Any], storage: Storage) -> int:
    from .automation_store import AutomationStore
    from .weekly_sync import WeeklySyncRunner

    automation = AutomationStore(storage)
    automation.init_db()
    if automation.is_automation_paused():
        print("全部爬取已暂停；请先在菜单栏插件中点击“继续”")
        return 3
    automation_cfg = config.get("automation", {})
    if not automation.has_subscriptions():
        automation.adopt_existing_accounts(
            schedule_weekday=int(automation_cfg.get("weekday", 6)),
            schedule_hour=int(automation_cfg.get("hour", 21)),
            schedule_minute=int(automation_cfg.get("minute", 0)),
        )
    subscriptions = automation.list_subscriptions()
    if args.account_biz:
        subscriptions = [row for row in subscriptions if row["biz"] == args.account_biz]
        if not subscriptions:
            print(f"subscription not found for biz={args.account_biz}")
            return 2
    else:
        subscriptions = [row for row in subscriptions if int(row["enabled"])]
    runner = WeeklySyncRunner(storage, config.get("output", {}).get("path", "output"), config)
    run_one = runner.resume_latest_scheduled if args.resume_needs_session else runner.run_subscription_now
    results = [run_one(int(row["id"])).to_dict() for row in subscriptions]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(row["status"] == "completed" for row in results) else 2


def cmd_automation_prepare_session(
    args: argparse.Namespace,
    config: dict[str, Any],
    storage: Storage,
) -> int:
    """Resume one waiting job through the project's normal direct-first path.

    ``WeeklySyncRunner`` already owns the complete recovery contract: retry the
    saved exact-biz session first, open the saved seed once only on
    ``needs_session``, then retry the same scheduled job. Keep this command as a
    thin foreground entrypoint so the status App does not duplicate that logic.
    """
    from .automation_store import AutomationStore
    from .weekly_sync import WeeklySyncRunner

    automation = AutomationStore(storage)
    automation.init_db()
    if automation.is_automation_paused():
        print("全部爬取已暂停；不会打开或操作微信")
        return 3
    subscription = next(
        (row for row in automation.list_subscriptions() if row["biz"] == args.account_biz),
        None,
    )
    if subscription is None:
        print(f"subscription not found for biz={args.account_biz}")
        return 2
    waiting_job = automation.latest_resumable_job(int(subscription["id"]))
    if waiting_job is None or waiting_job["status"] not in ("needs_session", "failed"):
        print(
            json.dumps(
                {
                    "account_name": subscription["account_name"],
                    "status": "no_waiting_job",
                    "message": "当前没有等待微信会话的计划任务",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    resume_config = json.loads(json.dumps(config))
    resume_config.setdefault("automation", {})["prepare_wechat_session"] = True
    result = WeeklySyncRunner(
        storage,
        resume_config.get("output", {}).get("path", "output"),
        resume_config,
    ).resume_latest_scheduled(int(subscription["id"]))
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status == "completed" else 2


def cmd_automation_export_pending(
    args: argparse.Namespace,
    config: dict[str, Any],
    storage: Storage,
) -> int:
    from .automation_store import AutomationStore
    from .weekly_sync import WeeklySyncRunner

    automation = AutomationStore(storage)
    automation.init_db()
    subscriptions = [row for row in automation.list_subscriptions() if row["biz"] == args.account_biz]
    if not subscriptions:
        print(f"subscription not found for biz={args.account_biz}")
        return 2
    result = WeeklySyncRunner(
        storage,
        config.get("output", {}).get("path", "output"),
        config,
    ).export_pending_subscription_now(int(subscriptions[0]["id"]))
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status == "completed" else 2


def cmd_automation_cleanup(args: argparse.Namespace, config: dict[str, Any], storage: Storage) -> int:
    from .cleanup import ArchiveCleanup

    report = ArchiveCleanup(
        storage,
        config.get("output", {}).get("path", "output"),
    ).prune(execute=bool(args.execute))
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_automation_install_launch_agent(
    args: argparse.Namespace,
    config: dict[str, Any],
    storage: Storage,
) -> int:
    from .launch_agent import LaunchAgentManager

    web_cfg = config.get("web", {})
    manager = LaunchAgentManager(Path(__file__).resolve().parent.parent)
    path = manager.install(
        host=args.host or web_cfg.get("host", "127.0.0.1"),
        port=int(args.port or web_cfg.get("port", 8765)),
        source_database=storage.db_path,
        config=config,
    )
    print(
        json.dumps(
            {
                "plist_path": str(path),
                "product_root": str(manager.product_root),
                "database_path": str(manager.database_path),
                "output_dir": str(manager.output_dir),
                "status_app_path": str(manager.status_app_path),
                "status_item_plist_path": str(manager.status_plist_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_automation_uninstall_launch_agent() -> int:
    from .launch_agent import LaunchAgentManager

    LaunchAgentManager(Path(__file__).resolve().parent.parent).uninstall()
    print("LaunchAgent removed")
    return 0


def cmd_automation_launch_agent_status() -> int:
    from .launch_agent import LaunchAgentManager

    status = LaunchAgentManager(Path(__file__).resolve().parent.parent).status()
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status["loaded"] else 2


def read_input_lines(path: str | Path) -> list[str]:
    input_path = Path(path)
    lines = []
    for line in input_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


def read_input_values(value: str | Path) -> list[str]:
    input_path = Path(value)
    if input_path.exists() and input_path.is_file():
        return read_input_lines(input_path)
    text = str(value).strip()
    return [text] if text else []


def _deep_update(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
