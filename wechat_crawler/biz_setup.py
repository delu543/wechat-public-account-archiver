"""Explicit, isolated setup entry. No weekly/import path invokes this module.

Authorization comes from the human's current explicit setup Skill invocation
and exact fresh consent, not from flags or a missing-key diagnostic. Native
operations are intentionally injectable so development stays entirely offline.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from dataclasses import dataclass, field
import io
import json
import secrets
import sys
import time

from . import biz_push_helper as gate
from . import biz_setup_store as store
from .biz_setup_targets import inventory, scope_digest, pages_for


@dataclass
class Scope:
    db_base: object = field(repr=False)
    targets: dict = field(repr=False)
    app: object = field(repr=False)
    cdhash: str = field(repr=False)
    digest: str = field(repr=False)
    dependencies_ready: bool = False
    created_at: float = field(default_factory=time.time, repr=False)
    consumed: bool = False


def scan(backend):
    db_base = backend.bind()
    app, cdhash = backend.app()
    targets = inventory(db_base)
    ready = backend.dependencies_ready()
    if backend.bind() != db_base:
        raise gate.GateError("current_account_unstable")
    return Scope(db_base, targets, app, cdhash, scope_digest(db_base, targets, app, cdhash), ready)


def public_scan(scope):
    return {"status": "dry_scan", "backend": "development-biz-only-setup", "signed_companion": False,
        "current_account_uniquely_bound": True, "targets": sorted(scope.targets),
        "app_version": scope.app.version, "dependencies_ready": scope.dependencies_ready,
        "writes": 0, "chat_database_reads": 0, "key_reads": 0, "capture_attempted": False,
        "risk": "临时本地签名副本和精确salt Frida观察；可能重登、手机验证或风控；4.1.15未实测",
        "credential_storage": "独立0700目录/0600文件，非Keychain；不修改旧聊天配置，不保留明文库"}


def initialize(scope, backend, *, explicit_skill=False, exact_consent=False, duration=60, root=None):
    report = {"status": "blocked", "backend": "development-biz-only-setup", "signed_companion": False,
              "capture_attempted": False, "credentials_written": False, "chat_database_reads": 0,
              "retained_plaintext": False, "master_pause_changed": False}
    if not explicit_skill or not exact_consent:
        return dict(report, reason="explicit_setup_skill_and_fresh_exact_consent_required")
    if scope.consumed or not 0 <= time.time() - scope.created_at <= 1800:
        return dict(report, reason="setup_scope_expired_or_consumed")
    runtime = None
    try:
        if not scope.dependencies_ready or not 5 <= duration <= 180:
            raise gate.GateError("setup_prerequisites_required")
        backend.quiescent(scope)  # user already quit; never quit original here
        current = inventory(scope.db_base)
        digest = scope_digest(scope.db_base, current, scope.app, scope.cdhash)
        if not secrets.compare_digest(scope.digest, digest):
            raise gate.GateError("biz_scope_changed_rescan_and_new_consent_required")
        scope.targets = current  # same files; carry post-quit sizes forward
        scope.consumed = True  # no automatic retries, even on an interrupted run
        runtime = backend.prepare(scope)
        report["capture_attempted"] = True
        with redirect_stdout(io.StringIO()):
            matched = backend.capture(scope, runtime, duration)
        report["observation_detached"] = True  # capture must unload+detach before returning
        backend.verify_account(scope, runtime)
        pages = pages_for(scope.targets)
        store.publish(scope.db_base, scope.targets, matched, pages, root=root)
        report.update(credentials_written=True, first_page_hmac_verified=True,
                      all_database_pages_authenticated=False, status="initialized")
    except gate.GateError as exc:
        report.update(status="blocked", reason=str(exc))
    except Exception:
        report.update(status="blocked", reason="biz_setup_local_check_failed")
    finally:
        if runtime is not None:
            try:
                report.update(backend.finish(scope, runtime))
            except Exception:
                report.update(status="blocked", restore_reason="private_quit_or_original_restore_unverified",
                              required_actions=["请正常退出临时副本并检查原版微信；不自动重试或强退"])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="公众号库专用开发初始化；仅本人显式setup Skill与单独同意后执行。")
    parser.add_argument("command", choices=["dry-scan", "initialize"])
    parser.add_argument("--explicit-setup-skill", action="store_true", help="仅流程标记，不构成用户授权。")
    parser.add_argument("--duration", type=int, default=60, help="一次观察上限5–180秒，无自动重试。")
    args = parser.parse_args(argv)
    if not args.explicit_setup_skill:
        print(json.dumps({"status": "blocked", "reason": "explicit_setup_skill_required"}))
        return 2
    # No account/DB/dependency access for an unattended initialize invocation.
    if args.command == "initialize" and not sys.stdin.isatty():
        print(json.dumps({"status": "blocked", "reason": "human_interactive_consent_required"}))
        return 2
    try:
        from .biz_setup_backend import NativeBackend
        backend = NativeBackend()
        scope = scan(backend)
        report = public_scan(scope)
        if args.command == "initialize":
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if not scope.dependencies_ready:
                raise gate.GateError("setup_dependencies_require_separate_install_consent")
            phrase = "仅为当前账号初始化 " + ",".join(sorted(scope.targets))
            print("将正常退出临时副本并请求打开已验证原版；这不保证登录成功。可Ctrl-C取消。")
            print("确认本次同一账号、精确目标、临时签名副本、精确salt观察和私有文件保存后，请输入：" + phrase)
            if input().strip() != phrase:
                raise gate.GateError("fresh_exact_consent_not_given")
            input("请本人正常退出官方微信，再按回车。不要强制退出，也不要换账号：")
            report = initialize(scope, backend, explicit_skill=True, exact_consent=True, duration=args.duration)
    except (KeyboardInterrupt, EOFError):
        report = {"status": "blocked", "reason": "setup_cancelled_no_automatic_resume"}
    except gate.GateError as exc:
        report = {"status": "blocked", "reason": str(exc)}
    except Exception:
        report = {"status": "blocked", "reason": "biz_setup_local_check_failed"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in ("dry_scan", "initialized") else 2


if __name__ == "__main__":
    raise SystemExit(main())
