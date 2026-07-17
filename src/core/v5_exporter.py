"""Read-only exporter for the translator-v5 full-book SQLite store."""

from __future__ import annotations

import hashlib
import sqlite3
from collections import OrderedDict
from pathlib import Path
from typing import List

from .exporter import BookExporter, ExportResult
from .schemas import Chapter, ChunkStatus, TextChunk


class V5BookExporter(BookExporter):
    """Project V5's immutable active translations into the legacy readers."""

    def __init__(self, project, database_path: str | Path | None = None):
        super().__init__(project)
        self.database_path = Path(database_path) if database_path else (
            Path(project.root_dir) / "artifacts" / "translator_v5" / "book.db"
        )
        self._cached_chapters: List[Chapter] | None = None
        self._missing_count = 0
        self._stale_count = 0

    @staticmethod
    def _fingerprint(rows) -> str:
        digest = hashlib.sha256()
        for row in rows:
            digest.update(row["block_id"].encode())
            digest.update(b"\0")
            digest.update(row["source_hash"].encode())
            digest.update(b"\0")
            digest.update(str(row["global_index"]).encode())
            digest.update(b"\n")
        return digest.hexdigest()

    def _chapters(self) -> List[Chapter]:
        if self._cached_chapters is not None:
            return self._cached_chapters
        if not self.database_path.exists():
            raise FileNotFoundError(f"V5 数据库不存在：{self.database_path}")

        uri = f"{self.database_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as database:
            database.row_factory = sqlite3.Row
            rows = database.execute(
                """
                SELECT b.block_id, b.global_index, b.chapter_id, b.chapter_title,
                       b.block_index, b.source_text, b.source_hash,
                       t.source_hash AS translation_source_hash,
                       t.text AS translation_text, t.status AS translation_status
                FROM book_blocks AS b
                LEFT JOIN translations AS t
                  ON t.block_id=b.block_id AND t.active=1
                ORDER BY b.global_index
                """
            ).fetchall()
            meta = database.execute(
                "SELECT value FROM book_meta WHERE key='source_fingerprint'"
            ).fetchone()

        if not rows:
            raise ValueError("V5 数据库中没有文本块")
        expected_fingerprint = meta["value"] if meta else None
        actual_fingerprint = self._fingerprint(rows)
        if expected_fingerprint != actual_fingerprint:
            raise ValueError("V5 数据库的原文指纹不匹配")

        grouped = OrderedDict()
        missing = 0
        stale = 0
        for row in rows:
            chapter_id = row["chapter_id"]
            grouped.setdefault(
                chapter_id,
                {
                    "title": row["chapter_title"],
                    "source": [],
                    "chunks": [],
                },
            )
            translation = row["translation_text"]
            if translation is None or not translation.strip():
                missing += 1
                status = ChunkStatus.PENDING
                final_translation = None
            elif row["translation_source_hash"] != row["source_hash"]:
                stale += 1
                status = ChunkStatus.PENDING
                final_translation = None
            else:
                status = (
                    ChunkStatus.HUMAN_REVIEW
                    if row["translation_status"] == "completed_with_warnings"
                    else ChunkStatus.COMPLETED
                )
                final_translation = translation.strip()
            grouped[chapter_id]["source"].append(row["source_text"])
            grouped[chapter_id]["chunks"].append(
                TextChunk(
                    id=row["block_id"],
                    chapter_id=chapter_id,
                    index=row["block_index"],
                    source_text=row["source_text"],
                    status=status,
                    final_translation=final_translation,
                )
            )

        self._missing_count = missing
        self._stale_count = stale
        self._cached_chapters = [
            Chapter(
                id=chapter_id,
                title=data["title"],
                index=index,
                source_text="\n\n".join(data["source"]),
                chunks=data["chunks"],
            )
            for index, (chapter_id, data) in enumerate(grouped.items())
        ]
        return self._cached_chapters

    def export_v5(
        self,
        output_dir: str | Path | None = None,
        *,
        allow_incomplete: bool = False,
    ) -> ExportResult:
        self._chapters()
        if not allow_incomplete:
            if self._missing_count:
                raise ValueError(f"{self._missing_count} 个文本块没有活动译文")
            if self._stale_count:
                raise ValueError(f"{self._stale_count} 个活动译文的原文哈希不匹配")
        target_dir = output_dir or (
            Path(self.project.root_dir) / "exports" / "translator_v5"
        )
        return self.export(
            output_dir=target_dir,
            require_complete=not allow_incomplete,
        )
