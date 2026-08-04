from pathlib import Path
import subprocess

from concurrent.futures import ThreadPoolExecutor

from wechat_crawler.crawler.discoverers.wechat_cache_discoverer import WechatCacheDiscoverer


def test_wechat_cache_discoverer_extracts_full_urls_and_redacts_sensitive_params(tmp_path):
    cache = tmp_path / "cache.bin"
    cache.write_bytes(
        (
            "prefix https://mp.weixin.qq.com/s?__biz=MzExampleAccount=="
            "&mid=2647929883&idx=1&sn=abc&chksm=def&key=secret"
            "&pass_ticket=ticket&uin=123&scene=91 suffix"
        ).encode("utf-8")
    )

    items = WechatCacheDiscoverer(tmp_path, target_biz="MzExampleAccount==").discover()

    assert len(items) == 1
    assert items[0].public_url == (
        "https://mp.weixin.qq.com/s?__biz=MzExampleAccount%3D%3D"
        "&mid=2647929883&idx=1&sn=abc&chksm=def"
    )
    assert "key=secret" in items[0].fetch_url
    assert "key=secret" not in items[0].public_url
    assert items[0].mid == "2647929883"


def test_wechat_cache_discoverer_extracts_mid_candidates_without_fetch_url(tmp_path):
    stat = tmp_path / "stat.bin"
    stat.write_bytes(b"gh_example_account,2647929878,1,0,0,0,2,2,0,2,1")

    items = WechatCacheDiscoverer(
        tmp_path,
        target_biz="MzExampleAccount==",
        target_wechat_id="gh_example_account",
    ).discover()

    assert len(items) == 1
    assert items[0].fetch_url is None
    assert items[0].public_url == "https://mp.weixin.qq.com/s?__biz=MzExampleAccount%3D%3D&mid=2647929878&idx=1"
    assert items[0].wechat_id == "gh_example_account"


def test_wechat_cache_discoverer_extracts_utf16be_http_full_url(tmp_path):
    cache = tmp_path / "resourceid.ldb"
    cache.write_bytes(
        (
            "noise http://mp.weixin.qq.com/s?__biz=MzExampleAccount=="
            "&mid=2647929884&idx=1&sn=abc2&chksm=def2&key=secret suffix"
        ).encode("utf-16be")
    )

    items = WechatCacheDiscoverer(tmp_path, target_biz="MzExampleAccount==").discover()

    assert len(items) == 1
    assert items[0].public_url == (
        "http://mp.weixin.qq.com/s?__biz=MzExampleAccount%3D%3D"
        "&mid=2647929884&idx=1&sn=abc2&chksm=def2"
    )
    assert "key=secret" in items[0].fetch_url


def test_wechat_cache_discoverer_pairs_short_url_with_nearby_target_full_url(tmp_path):
    cache = tmp_path / "share-data.bin"
    cache.write_bytes(
        (
            "prefix https://mp.weixin.qq.com/s/4KpNkL-JliiFvAjH04Whlwhttps://mp.weixin.qq.com/s"
            "?__biz=MzExampleAccount==&mid=2647929885&idx=1&sn=abc3&key=secret"
        ).encode("utf-8")
    )

    items = WechatCacheDiscoverer(tmp_path, target_biz="MzExampleAccount==").discover()

    assert any(item.public_url == "https://mp.weixin.qq.com/s/4KpNkL-JliiFvAjH04Whlw" for item in items)
    short = next(item for item in items if item.public_url.endswith("4KpNkL-JliiFvAjH04Whlw"))
    assert short.fetch_url == short.public_url
    assert short.mid == "2647929885"


def test_wechat_cache_discoverer_finds_profiles_from_app_data_root(tmp_path):
    profile = tmp_path / "radium" / "web" / "profiles" / "multitab_abc"
    profile.mkdir(parents=True)
    (profile / "Share Data").write_bytes(
        (
            "https://mp.weixin.qq.com/s?__biz=MzExampleAccount=="
            "&mid=2647929886&idx=1&sn=abc4&chksm=def4&key=secret"
        ).encode("utf-8")
    )

    items = WechatCacheDiscoverer(tmp_path, target_biz="MzExampleAccount==").discover()

    assert len(items) == 1
    assert items[0].mid == "2647929886"


def test_wechat_cache_discoverer_uses_renderer_open_profile_without_parent_scan(tmp_path):
    profile = tmp_path / "radium" / "web" / "profiles" / "multitab_active"
    profile.mkdir(parents=True)
    (profile / "Share Data").write_text(
        "https://mp.weixin.qq.com/s?__biz=MzExampleAccount=="
        "&mid=2647929887&idx=1&sn=active&uin=123&key=current",
        encoding="utf-8",
    )

    def runner(command, **_kwargs):
        if command[0] == "pgrep":
            return subprocess.CompletedProcess(command, 0, stdout="321\n", stderr="")
        if command[0] == "lsof":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"WeChat 321 user 1r REG 1,1 0 0 {profile}/Share Data\n",
                stderr="",
            )
        raise AssertionError(command)

    items = WechatCacheDiscoverer(
        tmp_path,
        target_biz="MzExampleAccount==",
        runner=runner,
    ).discover(stop_when_session_fields=True)

    assert len(items) == 1
    assert items[0].mid == "2647929887"


def test_wechat_cache_read_works_from_automation_worker_thread(tmp_path):
    cache = tmp_path / "Share Data"
    cache.write_bytes(b"worker-safe")
    discoverer = WechatCacheDiscoverer(tmp_path, target_biz="MzExampleAccount==")

    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(discoverer._read_bytes, cache).result(timeout=2) == b"worker-safe"


def test_wechat_cache_session_scan_stops_after_signed_candidate(tmp_path):
    signed = tmp_path / "Share Data"
    signed.write_text(
        "https://mp.weixin.qq.com/s?__biz=MzExampleAccount==&mid=2647929999&idx=1"
        "&sn=abc&uin=123&key=current-session",
        encoding="utf-8",
    )
    later = tmp_path / "Local Storage" / "leveldb"
    later.mkdir(parents=True)
    (later / "000001.log").write_bytes(b"later")
    discoverer = WechatCacheDiscoverer(tmp_path, target_biz="MzExampleAccount==")

    items = discoverer.discover(stop_when_session_fields=True)

    assert len(items) == 1
    assert "key=current-session" in items[0].fetch_url


def test_wechat_cache_session_scan_prefers_share_data_over_stale_favicons(tmp_path):
    (tmp_path / "Favicons").write_text(
        "https://mp.weixin.qq.com/s?__biz=MzExampleAccount==&mid=2647929000&idx=1"
        "&sn=old&uin=123&key=stale-session",
        encoding="utf-8",
    )
    (tmp_path / "Share Data").write_text(
        "https://mp.weixin.qq.com/s?__biz=MzExampleAccount==&mid=2647930000&idx=1"
        "&sn=new&uin=123&key=current-session",
        encoding="utf-8",
    )
    discoverer = WechatCacheDiscoverer(tmp_path, target_biz="MzExampleAccount==")

    items = discoverer.discover(stop_when_session_fields=True)

    assert len(items) == 1
    assert items[0].source_file == str(tmp_path / "Share Data")
    assert "key=current-session" in items[0].fetch_url


def test_wechat_cache_session_scan_reads_newest_chromium_http_cache(tmp_path):
    cache_dir = tmp_path / "Cache" / "Cache_Data"
    cache_dir.mkdir(parents=True)
    signed = cache_dir / "current_0"
    signed.write_text(
        "https://mp.weixin.qq.com/s?__biz=MzExampleAccount==&mid=2647931000&idx=1"
        "&sn=current&uin=123&key=http-cache-session",
        encoding="utf-8",
    )
    discoverer = WechatCacheDiscoverer(tmp_path, target_biz="MzExampleAccount==")

    items = discoverer.discover(stop_when_session_fields=True)

    assert len(items) == 1
    assert items[0].source_file == str(signed)
    assert "key=http-cache-session" in items[0].fetch_url


def test_session_fingerprint_is_opaque_and_changes_with_exact_account_session(tmp_path):
    cache = tmp_path / "Share Data"
    cache.write_text(
        "https://mp.weixin.qq.com/s?__biz=MzExampleAccount==&mid=2647931001&idx=1"
        "&sn=first&uin=123&key=first-session",
        encoding="utf-8",
    )
    discoverer = WechatCacheDiscoverer(tmp_path, target_biz="MzExampleAccount==")

    first = discoverer.session_fingerprint()
    assert first
    assert "first-session" not in first

    cache.write_text(
        "https://mp.weixin.qq.com/s?__biz=MzExampleAccount==&mid=2647931002&idx=1"
        "&sn=second&uin=123&key=second-session",
        encoding="utf-8",
    )
    second = discoverer.session_fingerprint()

    assert second
    assert second != first
    assert "second-session" not in second
