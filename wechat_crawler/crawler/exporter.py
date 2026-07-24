from __future__ import annotations

import json
from pathlib import Path

from markdownify import markdownify as html_to_markdown

from .models import now_iso
from .normalizer import safe_path_part
from .storage import Storage


class MarkdownExporter:
    def __init__(self, storage: Storage, output_dir: str | Path) -> None:
        self.storage = storage
        self.output_dir = Path(output_dir)

    def export_all(self, force: bool = False, account_id: int | None = None) -> int:
        exported = 0
        self.output_dir.mkdir(parents=True, exist_ok=True)
        accounts = [self.storage.account_by_id(account_id)] if account_id is not None else self.storage.iter_accounts()
        for account in accounts:
            if account is None:
                continue
            articles = self.storage.articles_for_account(int(account["id"]))
            account_dir = self.output_dir / safe_path_part(account["account_name"], f"account-{account['id']}")
            article_dir = account_dir / "articles"
            article_dir.mkdir(parents=True, exist_ok=True)
            metadata = {
                "account_name": account["account_name"],
                "account_id": account["biz"],
                "account_biz": account["biz"],
                "wechat_id": account["wechat_id"],
                "source": account["source"],
                "article_count": len(articles),
                "export_time": now_iso(),
            }
            rendered_metadata = json.dumps(metadata, ensure_ascii=False, indent=2)
            for metadata_name in ("metadata.json", "account_metadata.json"):
                (account_dir / metadata_name).write_text(rendered_metadata, encoding="utf-8")
            used_filenames: set[str] = set()
            for article in articles:
                target = article_dir / self._article_filename(article, used_filenames)
                if target.exists() and not force:
                    continue
                target.write_text(self._render_article(article), encoding="utf-8")
                exported += 1
        return exported

    def _article_filename(self, article, used_filenames: set[str]) -> str:
        publish_time = article["publish_time"] or "unknown-date"
        date_part = publish_time[:10] if len(publish_time) >= 10 else "unknown-date"
        title = safe_path_part(article["title"], f"article-{article['id']}")
        filename = f"{date_part}_{title}.md"
        if filename in used_filenames:
            filename = f"{date_part}_{title}_{article['id']}.md"
        used_filenames.add(filename)
        return filename

    def _render_article(self, article) -> str:
        image_urls = json.loads(article["image_urls"] or "[]")
        body = html_to_markdown(article["content_html"] or "", heading_style="ATX").strip()
        if not body:
            body = article["content_text"] or ""
        header = [
            f"title: {article['title']}",
            f"account_name: {article['account_name']}",
            f"account_id: {article['biz'] or ''}",
            f"account_biz: {article['biz'] or ''}",
            f"author: {article['author'] or ''}",
            f"publish_time: {article['publish_time'] or ''}",
            f"source_url: {article['source_url']}",
            f"discovered_from: {article['discovered_from'] or ''}",
            f"crawl_time: {article['crawl_time']}",
            f"content_hash: {article['content_hash']}",
            "",
        ]
        if article["summary"]:
            header.extend(["summary:", article["summary"], ""])
        if article["cover_image_url"]:
            header.extend([f"cover_image: {article['cover_image_url']}", ""])
        if image_urls:
            header.extend(["images:"])
            header.extend(f"- {url}" for url in image_urls)
            header.append("")
        return "\n".join(header) + body + "\n"
