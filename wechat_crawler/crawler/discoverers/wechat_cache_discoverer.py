from __future__ import annotations

import hashlib
import html
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from wechat_crawler.crawler.normalizer import normalize_url

FULL_URL_RE = re.compile(rb"https?://mp\.weixin\.qq\.com/s\?[^ \x00\r\n\"'<>]+")
SHORT_URL_RE = re.compile(rb"https?://mp\.weixin\.qq\.com/s/([A-Za-z0-9_-]{22})")
MID_RE_TEMPLATE = r"{wechat_id},(\d{{6,}}),(\d+),"
SENSITIVE_PARAMS = {
    "key",
    "pass_ticket",
    "uin",
    "exportkey",
    "sessionid",
    "clicktime",
    "enterid",
    "devicetype",
    "version",
    "lang",
    "countrycode",
    "acctmode",
    "wx_header",
    "scene",
    "subscene",
    "ascene",
}
STABLE_PARAMS = ("__biz", "mid", "idx", "sn", "chksm")
# Share Data is refreshed by the visible article page. Favicons can retain old
# signed URLs for weeks, so it must remain a fallback when session scan stops early.
CACHE_FILE_NAMES = ("Share Data", "Favicons")
HTTP_CACHE_DIRECTORY = Path("Cache") / "Cache_Data"
MAX_HTTP_CACHE_FILES = 512
PROTECTED_IO_TIMEOUT_SECONDS = 0.8
INDEXED_DB_DIR_NAMES = (
    "https_mp.weixin.qq.com_0.indexeddb.leveldb",
    "weixin_resourceid_0.indexeddb.leveldb",
    "weixin_xworker_0.indexeddb.leveldb",
)


@dataclass(frozen=True)
class CacheArticleCandidate:
    public_url: str
    fetch_url: str | None
    source_file: str
    biz: str | None
    mid: str | None
    idx: str | None
    sn: str | None
    chksm: str | None
    wechat_id: str | None = None


class WechatCacheDiscoverer:
    name = "wechat_cache"

    def __init__(
        self,
        root_dir: str | Path,
        target_biz: str,
        target_wechat_id: str | None = None,
        max_file_bytes: int = 20 * 1024 * 1024,
        runner=subprocess.run,
    ) -> None:
        self.root_dir = Path(root_dir).expanduser()
        self.target_biz = target_biz
        self.target_wechat_id = target_wechat_id
        self.max_file_bytes = max_file_bytes
        self._run = runner

    def discover(self, *, stop_when_session_fields: bool = False) -> list[CacheArticleCandidate]:
        candidates: dict[str, CacheArticleCandidate] = {}
        if not self.root_dir.exists():
            return []
        for path in self._iter_files():
            data = self._read_bytes(path)
            if not data:
                continue
            self._extract_full_urls(data, path, candidates)
            self._extract_encoded_full_urls(data, path, candidates)
            self._extract_mid_candidates(data, path, candidates)
            if stop_when_session_fields and self._has_session_candidate(candidates):
                break
        return sorted(candidates.values(), key=lambda item: (int(item.mid or 0), int(item.idx or 0)), reverse=True)

    def session_fingerprint(self) -> str | None:
        """Return an opaque fingerprint for the current exact-account session.

        The signed URL is used only in memory and is never returned or logged.
        A changed fingerprint means WeChat wrote a newly loaded article session.
        """
        signed_urls: list[str] = []
        for candidate in self.discover(stop_when_session_fields=True):
            if not candidate.fetch_url:
                continue
            params = dict(parse_qsl(urlparse(candidate.fetch_url).query, keep_blank_values=True))
            if params.get("uin") and params.get("key"):
                signed_urls.append(candidate.fetch_url)
        if not signed_urls:
            return None
        payload = "\n".join(sorted(set(signed_urls))).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _iter_files(self):
        roots = self._profile_roots()
        seen: set[Path] = set()
        for root in roots:
            for path in self._candidate_files(root):
                if path in seen:
                    continue
                seen.add(path)
                try:
                    if not path.is_file() or path.stat().st_size > self.max_file_bytes:
                        continue
                except OSError:
                    continue
                yield path

    def _profile_roots(self) -> list[Path]:
        if (self.root_dir / "Share Data").exists() or (self.root_dir / "IndexedDB").exists():
            return [self.root_dir]
        profiles_dir = self.root_dir / "radium" / "web" / "profiles"
        if profiles_dir.is_dir():
            # Listing this protected parent can block for minutes on recent
            # macOS releases. The renderer's open files identify the two
            # active profile directories without crawling historical profiles.
            active = self._active_profile_roots(profiles_dir)
            if active:
                return active
            if "com.tencent.xinWeChat" in profiles_dir.parts:
                return []
            return self._profile_roots_under(profiles_dir)
        if self.root_dir.name == "web" and (self.root_dir / "profiles").is_dir():
            return self._profile_roots_under(self.root_dir / "profiles")
        if self.root_dir.name == "radium" and (self.root_dir / "web" / "profiles").is_dir():
            return self._profile_roots_under(self.root_dir / "web" / "profiles")
        return self._profile_roots_under(self.root_dir)

    def _active_profile_roots(self, profiles_dir: Path) -> list[Path]:
        try:
            pgrep = self._run(
                ["pgrep", "-f", "/WeChatAppEx.app/Contents/MacOS/WeChatAppEx"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if pgrep.returncode != 0:
            return []
        roots: set[Path] = set()
        for pid in pgrep.stdout.split():
            if not pid.isdigit():
                continue
            try:
                opened = self._run(
                    ["lsof", "-nP", "-p", pid],
                    capture_output=True,
                    text=True,
                    timeout=4,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if opened.returncode != 0:
                continue
            for match in re.finditer(
                r"(/[^\r\n]+/radium/web/profiles/(?:multitab|webview)_[^/\s]+)",
                opened.stdout,
            ):
                path = Path(match.group(1))
                try:
                    path.relative_to(profiles_dir)
                except ValueError:
                    continue
                roots.add(path)
        ordered = sorted(
            roots,
            key=lambda path: (path.name.startswith("multitab_"), self._modified_time(path)),
            reverse=True,
        )
        multitab = [path for path in ordered if path.name.startswith("multitab_")]
        return multitab or ordered[:1]

    def _profile_roots_under(self, directory: Path) -> list[Path]:
        try:
            roots = [
                path
                for path in directory.iterdir()
                if path.is_dir() and (path.name.startswith("multitab") or path.name.startswith("webview"))
            ]
            roots.sort(key=self._modified_time, reverse=True)
            return roots or [directory]
        except OSError:
            return []

    @staticmethod
    def _modified_time(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _candidate_files(self, root: Path):
        yield from self._http_cache_candidate_files(root / HTTP_CACHE_DIRECTORY)
        for name in CACHE_FILE_NAMES:
            yield root / name
        # WeChat 4.x may keep the newest signed article response only in the
        # Chromium HTTP cache. Scan a bounded newest-first set so session
        # recovery remains fast even when the profile contains years of data.
        if not (root / "Share Data").exists() and not (root / "IndexedDB").exists():
            try:
                for path in root.iterdir():
                    if path.is_file():
                        yield path
            except OSError:
                pass

        indexed = root / "IndexedDB"
        for db_name in INDEXED_DB_DIR_NAMES:
            db_dir = indexed / db_name
            if not db_dir.is_dir():
                continue
            yield from self._leveldb_candidate_files(db_dir)

        yield from self._leveldb_candidate_files(root / "Local Storage" / "leveldb")
        yield from self._leveldb_candidate_files(root / "Session Storage")
        yield from self._leveldb_candidate_files(root / "net" / "kvcomm")

    def _http_cache_candidate_files(self, directory: Path):
        if self._is_protected_wechat_path(directory):
            names = self._protected_directory_names(directory, newest_first=True)
            for name in names[:MAX_HTTP_CACHE_FILES]:
                if name != "index-dir":
                    yield directory / name
            return
        try:
            files = [
                path
                for path in directory.iterdir()
                if path.is_file() and path.name != "the-real-index"
            ]
        except OSError:
            return
        files.sort(key=self._modified_time, reverse=True)
        yield from files[:MAX_HTTP_CACHE_FILES]

    def _protected_directory_names(self, directory: Path, *, newest_first: bool = False) -> list[str]:
        command = ["/bin/ls", "-1"]
        if newest_first:
            command.append("-t")
        command.append(str(directory))
        try:
            result = self._run(
                command,
                capture_output=True,
                text=True,
                timeout=PROTECTED_IO_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        return result.stdout.splitlines() if result.returncode == 0 else []

    @staticmethod
    def _is_protected_wechat_path(path: Path) -> bool:
        return "com.tencent.xinWeChat" in path.parts

    def _leveldb_candidate_files(self, directory: Path):
        if self._is_protected_wechat_path(directory):
            for name in self._protected_directory_names(directory):
                path = directory / name
                if path.suffix in {".log", ".ldb"}:
                    yield path
            return
        try:
            for path in directory.iterdir():
                if path.is_file() and path.suffix in {".log", ".ldb"}:
                    yield path
        except OSError:
            return

    @staticmethod
    def _has_session_candidate(candidates: dict[str, CacheArticleCandidate]) -> bool:
        for candidate in candidates.values():
            if not candidate.fetch_url:
                continue
            params = dict(parse_qsl(urlparse(candidate.fetch_url).query, keep_blank_values=True))
            if params.get("uin") and params.get("key"):
                return True
        return False

    def _read_bytes(self, path: Path) -> bytes | None:
        if self._is_protected_wechat_path(path):
            try:
                result = self._run(
                    ["/bin/cat", str(path)],
                    capture_output=True,
                    timeout=PROTECTED_IO_TIMEOUT_SECONDS,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            return result.stdout if result.returncode == 0 else None
        if threading.current_thread() is not threading.main_thread():
            try:
                return path.read_bytes()
            except OSError:
                return None

        def _timeout(_signum: int, _frame: FrameType | None) -> None:
            raise TimeoutError

        previous = signal.signal(signal.SIGALRM, _timeout)
        signal.setitimer(signal.ITIMER_REAL, 8.0)
        try:
            return path.read_bytes()
        except (OSError, TimeoutError):
            return None
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def _extract_full_urls(
        self,
        data: bytes,
        path: Path,
        candidates: dict[str, CacheArticleCandidate],
    ) -> None:
        self._extract_short_urls_near_target(data, path, candidates)
        for match in FULL_URL_RE.finditer(data):
            raw = self._decode_url(match.group(0))
            if not raw:
                continue
            self._add_full_url_candidate(raw, path, candidates)

    def _extract_encoded_full_urls(
        self,
        data: bytes,
        path: Path,
        candidates: dict[str, CacheArticleCandidate],
    ) -> None:
        for encoding in ("utf-8", "utf-16le", "utf-16be"):
            text = data.decode(encoding, errors="ignore")
            if self.target_biz not in text and "mp.weixin.qq.com/s" not in text:
                continue
            for match in re.finditer(r"https?://mp\.weixin\.qq\.com/s\?[^ \x00\r\n\"'<>]+", text):
                raw = self._decode_text_url(match.group(0))
                if raw:
                    self._add_full_url_candidate(raw, path, candidates)

    def _extract_short_urls_near_target(
        self,
        data: bytes,
        path: Path,
        candidates: dict[str, CacheArticleCandidate],
    ) -> None:
        for match in SHORT_URL_RE.finditer(data):
            short_url = f"https://mp.weixin.qq.com/s/{match.group(1).decode('ascii')}"
            window = data[match.start() : min(len(data), match.end() + 1800)]
            full_match = FULL_URL_RE.search(window)
            if not full_match:
                continue
            full_url = self._decode_url(full_match.group(0))
            if not full_url:
                continue
            params = self._target_full_url_params(full_url)
            if not params:
                continue
            key = normalize_url(short_url)
            candidates[key] = CacheArticleCandidate(
                public_url=short_url,
                fetch_url=short_url,
                source_file=str(path),
                biz=params.get("__biz"),
                mid=params.get("mid"),
                idx=params.get("idx"),
                sn=params.get("sn"),
                chksm=params.get("chksm"),
                wechat_id=self.target_wechat_id,
            )

    def _add_full_url_candidate(
        self,
        raw: str,
        path: Path,
        candidates: dict[str, CacheArticleCandidate],
    ) -> None:
        params = self._target_full_url_params(raw)
        if not params:
            return
        public_url = self._public_url(raw)
        key = normalize_url(public_url)
        candidates[key] = CacheArticleCandidate(
            public_url=public_url,
            fetch_url=raw,
            source_file=str(path),
            biz=params.get("__biz"),
            mid=params.get("mid"),
            idx=params.get("idx"),
            sn=params.get("sn"),
            chksm=params.get("chksm"),
            wechat_id=self.target_wechat_id,
        )

    def _target_full_url_params(self, raw: str) -> dict[str, str] | None:
        parsed = urlparse(html.unescape(raw))
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if params.get("__biz") != self.target_biz:
            return None
        if not params.get("mid") or not params.get("idx"):
            return None
        return params

    def _extract_mid_candidates(
        self,
        data: bytes,
        path: Path,
        candidates: dict[str, CacheArticleCandidate],
    ) -> None:
        if not self.target_wechat_id:
            return
        text = data.decode("utf-8", errors="ignore")
        pattern = re.compile(MID_RE_TEMPLATE.format(wechat_id=re.escape(self.target_wechat_id)))
        for match in pattern.finditer(text):
            mid = match.group(1)
            idx = match.group(2)
            public_url = self._mid_public_url(mid, idx)
            key = normalize_url(public_url)
            if key in candidates:
                continue
            candidates[key] = CacheArticleCandidate(
                public_url=public_url,
                fetch_url=None,
                source_file=str(path),
                biz=self.target_biz,
                mid=mid,
                idx=idx,
                sn=None,
                chksm=None,
                wechat_id=self.target_wechat_id,
            )

    def _decode_url(self, value: bytes) -> str | None:
        try:
            text = value.decode("utf-8", errors="ignore")
        except UnicodeDecodeError:
            return None
        return self._decode_text_url(text)

    def _decode_text_url(self, text: str) -> str | None:
        text = html.unescape(text)
        control = re.search(r"[\x00-\x1f\x7f]", text)
        if control:
            text = text[: control.start()]
        return text.rstrip(").,;")

    def _public_url(self, raw: str) -> str:
        parsed = urlparse(html.unescape(raw))
        values = dict(parse_qsl(parsed.query, keep_blank_values=True))
        params = [(key, values[key]) for key in STABLE_PARAMS if key in values]
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(params), ""))

    def _mid_public_url(self, mid: str, idx: str) -> str:
        query = urlencode({"__biz": self.target_biz, "mid": mid, "idx": idx})
        return f"https://mp.weixin.qq.com/s?{query}"
