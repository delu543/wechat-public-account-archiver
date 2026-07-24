from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from .crawler.storage import Storage


@dataclass(frozen=True)
class CleanupReport:
    raw_html_rows: int = 0
    raw_html_bytes: int = 0
    temporary_files: int = 0
    temporary_bytes: int = 0
    executed: bool = False

    def to_dict(self) -> dict[str, int | bool]:
        return asdict(self)


class ArchiveCleanup:
    """Remove reproducible raw pages and product-owned temporary files only."""

    def __init__(self, storage: Storage, output_dir: str | Path) -> None:
        self.storage = storage
        self.output_dir = Path(output_dir)

    def prune(
        self,
        *,
        article_ids: Sequence[int] | None = None,
        execute: bool = False,
        include_raw_html: bool = True,
    ) -> CleanupReport:
        where = "raw_html != '' AND content_text != ''" if include_raw_html else "0"
        params: list[int] = []
        if include_raw_html and article_ids is not None:
            unique_ids = sorted({int(value) for value in article_ids})
            if not unique_ids:
                where = "0"
            else:
                where += f" AND id IN ({','.join('?' for _ in unique_ids)})"
                params.extend(unique_ids)
        with self.storage.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS rows, COALESCE(SUM(LENGTH(raw_html)), 0) AS bytes FROM articles WHERE {where}",
                params,
            ).fetchone()
            if execute and int(row["rows"]):
                conn.execute(f"UPDATE articles SET raw_html = '' WHERE {where}", params)
        temp_files = self._temporary_files()
        temporary_bytes = sum(path.stat().st_size for path in temp_files if path.exists())
        if execute:
            for path in temp_files:
                path.unlink(missing_ok=True)
            temp_dir = self.output_dir / ".automation-tmp"
            if temp_dir.exists() and not any(temp_dir.iterdir()):
                temp_dir.rmdir()
            with self.storage.connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return CleanupReport(
            raw_html_rows=int(row["rows"]),
            raw_html_bytes=int(row["bytes"]),
            temporary_files=len(temp_files),
            temporary_bytes=temporary_bytes,
            executed=execute,
        )

    def _temporary_files(self) -> list[Path]:
        files: list[Path] = []
        temp_root = self.output_dir / ".automation-tmp"
        if temp_root.exists():
            files.extend(path for path in temp_root.rglob("*") if path.is_file())
        weekly_root = self.output_dir / "weekly"
        if weekly_root.exists():
            files.extend(path for path in weekly_root.rglob("*.tmp") if path.is_file())
        return sorted(set(files))
