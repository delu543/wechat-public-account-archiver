"""Default auto uses bound A only. B requires explicit --source cache."""
from __future__ import annotations

import sys

from .passive_weekly_sources import child_json, HELPER_PYTHON


class WeeklySourceRouter:
    def __init__(self, settings, mode="auto", *, child=None):
        if mode not in ("auto", "cache", "biz"):
            raise ValueError("invalid passive source mode")
        self.settings, self.mode = settings, mode
        self.child = child or child_json

    def read(self, accounts):
        request = {"accounts": [{"biz": a["biz"], "wechat_id": a["wechat_id"]} for a in accounts]}
        if self.mode == "cache":
            # Never import/call the key helper or inspect its credential root.
            return self.child("wechat_crawler.manual_cache_source", request,
                              argument="--manual-cache-only", timeout=30)
        source = self.child("wechat_crawler.biz_push_reader", request,
            argument="--authorized-biz-only", timeout=60, python=self.settings.get("helper_python")
            or (str(HELPER_PYTHON) if HELPER_PYTHON.is_file() else sys.executable))
        source["source_mode"] = "biz"
        if source.get("reason") == "biz_salt_changed":
            source.update(key_available=None, key_health="reinitialization_required")
        # An A failure is reported once; never silently switch to B, recapture,
        # change aliases/account, restart WeChat or conceal the key failure.
        return source


def public_source_health(result, mode):
    """CLI presentation only; does not alter completion/checkpoint evidence."""
    result["source_mode"] = mode
    result["effective_source"] = "cache" if mode == "cache" else "biz"
    if result.get("source_reason") == "biz_salt_changed":
        result.update(key_health="reinitialization_required", required_actions=["需重新初始化"])
    elif result.get("source_reason") == "biz_setup_required":
        result["required_actions"] = ["需要一次性初始化；请本人显式调用 $wechat-local-export-setup 并另行确认，本命令不会自动执行"]
    elif mode == "cache" and result.get("source_status"):
        result["key_health"] = "not_used"
        result["required_actions"] = ["B无密钥：请本人刷新各公众号主页并加载至旧节点；缓存未证完整时不推进节点"]
    return result
