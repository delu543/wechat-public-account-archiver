from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import re
import signal
import threading

from bs4 import BeautifulSoup
from v8serialize import V8SerializeError, loads

from wechat_crawler.crawler.dedup import normalized_text_hash
from wechat_crawler.crawler.models import ParsedArticle
from wechat_crawler.crawler.normalizer import normalize_url


SENSITIVE_RAW_KEYS = {
    "base_resp",
    "wxtoken",
    "sessionid",
    "cookie_count",
    "user_uin",
}
STABLE_PARAMS = ("__biz", "mid", "idx", "sn", "chksm")


@dataclass(frozen=True)
class ContentFlowStats:
    files_read: int
    decoded_batches: int
    decoded_items: int
    matched_articles: int


class WechatContentFlowCacheDiscoverer:
    name = "wechat_content_flow_cache"

    def __init__(
        self,
        root_dir: str | Path,
        target_biz: str,
        target_wechat_id: str | None = None,
        max_file_bytes: int = 120 * 1024 * 1024,
    ) -> None:
        self.root_dir = Path(root_dir).expanduser()
        self.target_biz = target_biz
        self.target_wechat_id = target_wechat_id
        self.max_file_bytes = max_file_bytes
        self.last_stats = ContentFlowStats(0, 0, 0, 0)

    def discover(self) -> list[ParsedArticle]:
        articles: dict[str, ParsedArticle] = {}
        files_read = 0
        decoded_batches = 0
        decoded_items = 0
        if not self.root_dir.exists():
            self.last_stats = ContentFlowStats(0, 0, 0, 0)
            return []

        for path in self._iter_files():
            data = self._read_bytes(path)
            if not data:
                continue
            files_read += 1
            for batch in self._decode_batches(data):
                decoded_batches += 1
                for public_url, payload in batch.items():
                    decoded_items += 1
                    article = self._article_from_payload(public_url, payload)
                    if article is None:
                        continue
                    key = article.normalized_url or normalize_url(article.source_url)
                    articles[key] = article
        result = sorted(
            articles.values(),
            key=lambda item: (
                item.publish_time or "",
                self._mid_idx_sort(item.source_url),
            ),
        )
        self.last_stats = ContentFlowStats(files_read, decoded_batches, decoded_items, len(result))
        return result

    def _iter_files(self):
        seen: set[Path] = set()
        for root in self._profile_roots():
            candidates = [
                root / "IndexedDB" / "weixin_xworker_0.indexeddb.leveldb",
                root / "IndexedDB" / "weixin_xworker_0.indexeddb.blob",
            ]
            for base in candidates:
                if not base.exists():
                    continue
                for path in base.rglob("*"):
                    if path in seen:
                        continue
                    seen.add(path)
                    try:
                        if path.is_file() and path.stat().st_size <= self.max_file_bytes:
                            yield path
                    except OSError:
                        continue
        if not seen:
            for path in self.root_dir.rglob("*"):
                try:
                    if path.is_file() and path.stat().st_size <= self.max_file_bytes:
                        yield path
                except OSError:
                    continue

    def _profile_roots(self) -> list[Path]:
        if (self.root_dir / "IndexedDB").exists():
            return [self.root_dir]
        profiles_dir = self.root_dir / "radium" / "web" / "profiles"
        if profiles_dir.is_dir():
            return self._profile_roots_under(profiles_dir)
        if self.root_dir.name == "web" and (self.root_dir / "profiles").is_dir():
            return self._profile_roots_under(self.root_dir / "profiles")
        if self.root_dir.name == "radium" and (self.root_dir / "web" / "profiles").is_dir():
            return self._profile_roots_under(self.root_dir / "web" / "profiles")
        return self._profile_roots_under(self.root_dir)

    def _profile_roots_under(self, directory: Path) -> list[Path]:
        try:
            roots = [
                path
                for path in directory.iterdir()
                if path.is_dir() and (path.name.startswith("multitab") or path.name.startswith("webview"))
            ]
            return roots or [directory]
        except OSError:
            return []

    def _read_bytes(self, path: Path) -> bytes | None:
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

    def _decode_batches(self, data: bytes):
        starts = set(range(min(16, len(data))))
        starts.update(match.start() for match in re.finditer(re.escape(b"\xff\x0f"), data))
        for start in sorted(starts):
            try:
                obj = loads(data[start:])
            except (V8SerializeError, ValueError, TypeError, EOFError):
                continue
            try:
                if obj["name"] != "__batch_get_appmsg_data":
                    continue
                raw_data = obj["data"]
            except (KeyError, TypeError):
                continue
            if not isinstance(raw_data, str):
                continue
            try:
                parsed = json.loads(raw_data)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                yield parsed

    def _article_from_payload(self, public_url_hint: str, payload: object) -> ParsedArticle | None:
        if not isinstance(payload, dict):
            return None
        content = payload.get("Content") or payload.get("content")
        if not isinstance(content, str):
            return None
        try:
            item = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(item, dict):
            return None
        if self.target_wechat_id and item.get("user_name") != self.target_wechat_id:
            return None

        source_url = self._stable_url(item.get("link") or payload.get("Url") or public_url_hint)
        if not source_url:
            return None
        params = dict(parse_qsl(urlparse(source_url).query, keep_blank_values=True))
        if params.get("__biz") != self.target_biz:
            return None

        content_text = self._content_text(item)
        if not content_text:
            return None
        title = self._short_title(content_text)
        if not title:
            return None
        image_urls = self._image_urls(item)
        raw_html = json.dumps(self._safe_raw_record(item), ensure_ascii=False, sort_keys=True)
        return ParsedArticle(
            source_url=source_url,
            normalized_url=normalize_url(source_url),
            title=title,
            account_name=str(item.get("nick_name") or "unknown-account").strip() or "unknown-account",
            account_id=self.target_biz,
            wechat_id=str(item.get("user_name") or "") or self.target_wechat_id,
            author=str(item.get("author") or "").strip() or None,
            publish_time=str(item.get("create_time") or "").strip() or None,
            summary=self._summary(content_text),
            content_html=self._text_to_html(content_text),
            content_text=content_text,
            image_urls=image_urls,
            cover_image_url=str(item.get("cdn_url") or "").strip() or None,
            raw_html=raw_html,
            content_hash=normalized_text_hash(content_text),
            status="parsed",
        )

    def _stable_url(self, value: object) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        parsed = urlparse(html.unescape(value.strip()))
        if parsed.netloc.lower() != "mp.weixin.qq.com":
            return None
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        stable = [(key, params[key]) for key in STABLE_PARAMS if key in params]
        if not params.get("__biz") or not params.get("mid") or not params.get("idx"):
            return None
        return urlunparse((parsed.scheme or "https", parsed.netloc, parsed.path or "/s", "", urlencode(stable), ""))

    def _content_text(self, item: dict[str, object]) -> str:
        text_page = item.get("text_page_info")
        if isinstance(text_page, dict):
            value = text_page.get("content_noencode") or text_page.get("content")
            if isinstance(value, str) and value.strip():
                return self._clean_text(value)
        for key in ("content_noencode", "content", "title"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return self._clean_text(value)
        return ""

    def _clean_text(self, text: str) -> str:
        text = html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
        if re.search(r"<(?:p|br|section|div|span|script|style|img)\b", text, flags=re.IGNORECASE):
            soup = BeautifulSoup(text, "html.parser")
            for node in soup(["script", "style", "noscript"]):
                node.decompose()
            for br in soup.find_all("br"):
                br.replace_with("\n")
            for block in soup.find_all(["p", "section", "div", "li"]):
                block.insert_after("\n\n")
            text = soup.get_text("")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _short_title(self, text: str) -> str:
        for line in text.splitlines():
            stripped = re.sub(r"\s+", " ", line).strip()
            if stripped:
                return stripped[:80]
        return ""

    def _summary(self, text: str) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        return compact[:220]

    def _text_to_html(self, text: str) -> str:
        paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
        return "\n".join(f"<p>{html.escape(part).replace(chr(10), '<br/>')}</p>" for part in paragraphs)

    def _image_urls(self, item: dict[str, object]) -> list[str]:
        urls: list[str] = []
        for key in ("cdn_url", "ori_head_img_url"):
            value = item.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                urls.append(value)
        for page in item.get("picture_page_info_list") or []:
            if not isinstance(page, dict):
                continue
            for key in ("cdn_url", "url"):
                value = page.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    urls.append(value)
        seen: set[str] = set()
        unique: list[str] = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                unique.append(url)
        return unique

    def _safe_raw_record(self, item: dict[str, object]) -> dict[str, object]:
        allowed = {
            "user_name",
            "nick_name",
            "alias",
            "mid",
            "idx",
            "create_time",
            "ori_create_time",
            "author",
            "signature",
            "item_show_type",
            "msg_daily_idx",
            "comment_id",
        }
        return {key: item.get(key) for key in sorted(allowed) if key in item and key not in SENSITIVE_RAW_KEYS}

    def _mid_idx_sort(self, url: str) -> tuple[int, int]:
        params = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
        try:
            mid = int(params.get("mid") or 0)
        except ValueError:
            mid = 0
        try:
            idx = int(params.get("idx") or 0)
        except ValueError:
            idx = 0
        return mid, idx
