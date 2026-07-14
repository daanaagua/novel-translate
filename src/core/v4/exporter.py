"""TXT/EPUB shadow exporter backed by the parallel_v4 SQLite database."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from ..exporter import BookExporter, ExportResult
from ..schemas import Chapter, ChunkStatus, TextChunk
from .database import V4Database
from .models import V4BlockStatus
from .validation import V4Validator


@dataclass
class V4ExportResult:
    txt_path: Path
    epub_path: Path
    quality_report_path: Path
    chapter_count: int
    chunk_count: int


class ParallelV4BookExporter(BookExporter):
    def __init__(self, project, database: V4Database | None = None):
        super().__init__(project)
        self.database = database or V4Database(project.root_dir)

    def _chapters(self) -> List[Chapter]:
        grouped: Dict[str, List[dict]] = {}
        for row in self.database.export_rows("parallel_v4"):
            grouped.setdefault(row["chapter_id"], []).append(row)
        chapters: List[Chapter] = []
        for rows in grouped.values():
            first = rows[0]
            chunks = []
            for row in rows:
                status = row.get("translation_status")
                if status == V4BlockStatus.COMPLETED.value:
                    chunk_status = ChunkStatus.COMPLETED
                elif status == V4BlockStatus.COMPLETED_WITH_WARNINGS.value:
                    chunk_status = ChunkStatus.HUMAN_REVIEW
                else:
                    chunk_status = ChunkStatus.PENDING
                chunks.append(
                    TextChunk(
                        id=row["id"],
                        chapter_id=row["chapter_id"],
                        index=row["block_index"],
                        source_text=row["source_text"],
                        status=chunk_status,
                        draft_translation=row.get("draft_translation") or "",
                        final_translation=row.get("final_translation") or "",
                    )
                )
            chapters.append(
                Chapter(
                    id=first["chapter_id"],
                    title=first["chapter_title"],
                    index=first["chapter_index"],
                    source_text="\n\n".join(row["source_text"] for row in rows),
                    chunks=chunks,
                )
            )
        return sorted(chapters, key=lambda chapter: chapter.index)

    def export_v4(
        self,
        output_dir: str | Path | None = None,
        allow_warnings: bool = False,
    ) -> V4ExportResult:
        report = V4Validator(self.database).validate()
        if report.high_count:
            raise ValueError(f"严格导出被拒绝：存在 {report.high_count} 个高严重度问题")
        if report.warning_count and not allow_warnings:
            raise ValueError(
                f"严格导出被拒绝：存在 {report.warning_count} 个警告；"
                "确认后可使用 allow_warnings"
            )
        target_dir = (
            Path(output_dir)
            if output_dir
            else self.project.root_dir / "exports" / "parallel_v4"
        )
        result: ExportResult = super().export(output_dir=target_dir, require_complete=True)
        report_path = target_dir / "quality_report.md"
        report_path.write_text(report.to_markdown(), encoding="utf-8")
        return V4ExportResult(
            txt_path=result.txt_path,
            epub_path=result.epub_path,
            quality_report_path=report_path,
            chapter_count=result.chapter_count,
            chunk_count=result.chunk_count,
        )
