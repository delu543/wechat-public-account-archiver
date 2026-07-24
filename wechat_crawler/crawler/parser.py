from __future__ import annotations

import ast
import html as html_module
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .dedup import normalized_text_hash
from .models import ParsedArticle
from .normalizer import extract_mp_params, normalize_url


class ParseError(RuntimeError):
    pass


class WechatArticleParser:
    def parse(self, html: str, source_url: str) -> ParsedArticle:
        soup = BeautifulSoup(html, "html.parser")
        script_body_text = self._script_article_text(html)
        title = self._first_text(
            soup,
            [
                "#activity-name",
                "h1.rich_media_title",
                "meta[property='og:title']",
                "title",
            ],
        )
        if script_body_text and not soup.select_one("#js_content") and not soup.select_one("article"):
            title = self._short_title(script_body_text)
        account_name = self._first_text(
            soup,
            [
                "#js_name",
                "a#js_name",
                ".rich_media_meta_nickname",
                ".profile_nickname",
                "strong.profile_nickname",
            ],
        )
        if not account_name:
            account_name = self._first_js_string(html, [r"nick_name\s*:"])
        if not title:
            raise ParseError("missing article title")
        if not account_name:
            account_name = "unknown-account"

        content = soup.select_one("#js_content") or soup.select_one("article")
        script_content_mode = content is None and bool(script_body_text)
        if content is None and not script_content_mode:
            content = soup.body
        if content is None and not script_content_mode:
            raise ParseError("missing article body")

        if script_content_mode:
            content_text = script_body_text or ""
            content_html = self._text_to_html(content_text)
        else:
            content = BeautifulSoup(str(content), "html.parser")
            self._clean_content(content, source_url)
            content_text = content.get_text("\n", strip=True)
            content_html = str(content).strip()
        if not content_text:
            raise ParseError("empty article body")

        params = extract_mp_params(source_url)
        account_id = params.get("__biz") or self._regex_first(
            html,
            [
                r"bizuin\s*:\s*['\"]([^'\"]+)['\"]",
                r"var\s+biz\s*=\s*['\"]([^'\"]+)['\"]",
                r"__biz=([A-Za-z0-9_\-=]+)",
            ],
        )
        wechat_id = self._regex_first(
            html,
            [
                r"var\s+user_name\s*=\s*['\"]([^'\"]+)['\"]",
                r"user_name\s*:\s*['\"]([^'\"]+)['\"]",
                r'"user_name"\s*:\s*"([^"]+)"',
                r"微信号[:：]\s*([A-Za-z0-9_\-]+)",
            ],
        )
        publish_time = self._first_text(soup, ["#publish_time", "em#publish_time"])
        if not publish_time:
            publish_time = self._publish_time_from_script(html)

        image_urls = [] if script_content_mode else self._extract_images(content, source_url)
        summary = self._meta_content(
            soup,
            [
                ("name", "description"),
                ("property", "og:description"),
            ],
        )
        cover_image = self._meta_content(soup, [("property", "og:image")])
        if not cover_image:
            cover_image = self._regex_first(
                html,
                [r"var\s+msg_cdn_url\s*=\s*['\"]([^'\"]+)['\"]"],
            )

        return ParsedArticle(
            source_url=source_url,
            normalized_url=normalize_url(source_url),
            title=title,
            account_name=account_name,
            account_id=account_id,
            wechat_id=wechat_id,
            author=self._first_text(soup, ["#js_author", "em#js_author"]),
            publish_time=publish_time,
            summary=summary,
            content_html=content_html,
            content_text=content_text,
            image_urls=image_urls,
            cover_image_url=urljoin(source_url, cover_image) if cover_image else None,
            raw_html=html,
            content_hash=normalized_text_hash(content_text or content_html),
            status="parsed",
            error_message=None,
        )

    def _clean_content(self, soup: BeautifulSoup, source_url: str) -> None:
        for node in soup(["script", "style", "iframe", "noscript", "svg", "link", "meta"]):
            node.decompose()
        for tag in soup.find_all(True):
            if tag.name == "img":
                image_url = tag.get("data-src") or tag.get("data-original") or tag.get("src")
                tag.attrs = {}
                if image_url:
                    tag["src"] = urljoin(source_url, image_url)
            else:
                tag.attrs = {}

    def _extract_images(self, soup: BeautifulSoup, source_url: str) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for image in soup.find_all("img"):
            value = image.get("data-src") or image.get("data-original") or image.get("src")
            if not value:
                continue
            absolute = urljoin(source_url, value)
            if absolute not in seen:
                seen.add(absolute)
                urls.append(absolute)
        return urls

    def _first_text(self, soup: BeautifulSoup, selectors: list[str]) -> str | None:
        for selector in selectors:
            node = soup.select_one(selector)
            if not node:
                continue
            if node.name == "meta":
                value = node.get("content", "")
            else:
                value = node.get_text(" ", strip=True)
            value = re.sub(r"\s+", " ", value or "").strip()
            if value:
                return value
        return None

    def _meta_content(self, soup: BeautifulSoup, attrs: list[tuple[str, str]]) -> str | None:
        for key, value in attrs:
            node = soup.find("meta", attrs={key: value})
            if node and node.get("content"):
                return node["content"].strip()
        return None

    def _regex_first(self, text: str, patterns: list[str]) -> str | None:
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()
        return None

    def _publish_time_from_script(self, html: str) -> str | None:
        direct = self._regex_first(
            html,
            [
                r"var\s+publish_time\s*=\s*['\"]([^'\"]+)['\"]",
                r"publish_time\s*:\s*['\"]([^'\"]+)['\"]",
            ],
        )
        if direct:
            return direct
        ct = self._regex_first(
            html,
            [
                r"var\s+ct\s*=\s*['\"]?(\d{10})['\"]?",
                r"window\.ct\s*=\s*['\"]?(\d{10})['\"]?",
                r"ct\s*:\s*['\"]?(\d{10})['\"]?",
            ],
        )
        if ct:
            return datetime.fromtimestamp(int(ct)).astimezone().isoformat(timespec="seconds")
        return None

    def _script_article_text(self, html: str) -> str | None:
        value = self._first_js_string(
            html,
            [
                r"window\.msg_title\s*=\s*window\.title\s*=",
                r"msg_title\s*:",
                r"title\s*:",
            ],
        )
        if not value:
            return None
        cleaned = re.sub(r"\r\n?", "\n", value).strip()
        return cleaned or None

    def _first_js_string(self, html: str, prefixes: list[str]) -> str | None:
        for prefix in prefixes:
            pattern = re.compile(prefix + r"\s*(['\"])((?:\\.|(?!\1).)*?)\1", re.S)
            match = pattern.search(html)
            if not match:
                continue
            decoded = self._decode_js_string(match.group(1), match.group(2))
            if decoded:
                return decoded.strip()
        return None

    def _decode_js_string(self, quote: str, value: str) -> str:
        try:
            return ast.literal_eval(f"{quote}{value}{quote}")
        except (SyntaxError, ValueError):
            return (
                value.replace("\\n", "\n")
                .replace("\\r", "\r")
                .replace("\\t", "\t")
                .replace("\\'", "'")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )

    def _short_title(self, text: str) -> str:
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "").strip()
        if not first_line:
            return ""
        return first_line if len(first_line) <= 90 else first_line[:87].rstrip() + "..."

    def _text_to_html(self, text: str) -> str:
        paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
        if not paragraphs:
            paragraphs = [text.strip()]
        body = []
        for paragraph in paragraphs:
            escaped = html_module.escape(paragraph).replace("\n", "<br/>")
            body.append(f"<p>{escaped}</p>")
        return "<div>" + "".join(body) + "</div>"
