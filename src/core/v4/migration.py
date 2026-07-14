"""Non-destructive import from serial_v3 project artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, List

from ...agents.glossary_manager import GlossaryManager
from ..schemas import ChunkStatus
from .database import V4Database, stable_id


PARSER_VERSION = "serial-v3-artifacts-1"


class V4Migrator:
    def __init__(self, project):
        self.project = project
        self.database = V4Database(project.root_dir)

    @staticmethod
    def _sha256_bytes(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _block_type(source_text: str) -> str:
        normalized = source_text.strip().casefold()
        if normalized.startswith("afterword") and normalized.endswith("the end"):
            return "bibliography"
        return "prose"

    def migrate(self) -> Dict[str, int | str]:
        source_bytes = self.project.source_file.read_bytes()
        source_text = self.project.source_file.read_text(encoding="utf-8")
        edition_id = self.database.ensure_source_edition(
            raw_sha256=self._sha256_bytes(source_bytes),
            normalized_sha256=self._sha256_bytes(source_text.encode("utf-8")),
            parser_version=PARSER_VERSION,
            source_path=str(self.project.source_file),
        )

        block_rows: List[dict] = []
        legacy_chunks = []
        global_index = 0
        chapters = []
        for chapter_file in sorted(self.project.memory.chapters_dir.glob("*.json")):
            chapter = self.project.memory.load_chapter(chapter_file.stem)
            if chapter:
                chapters.append(chapter)
        for chapter in sorted(chapters, key=lambda item: (item.index, item.id)):
            for chunk in sorted(chapter.chunks, key=lambda item: (item.index, item.id)):
                internal_id = stable_id("block", f"{edition_id}:{chunk.id}")
                block_rows.append(
                    {
                        "id": internal_id,
                        "legacy_id": chunk.id,
                        "chapter_id": chapter.id,
                        "chapter_title": chapter.title or chapter.id,
                        "chapter_index": chapter.index,
                        "block_index": chunk.index,
                        "global_index": global_index,
                        "block_type": self._block_type(chunk.source_text),
                        "source_text": chunk.source_text,
                        "source_hash": self._sha256_bytes(chunk.source_text.encode("utf-8")),
                        "token_count": chunk.token_count or 0,
                        "status": "pending",
                    }
                )
                legacy_chunks.append((internal_id, chunk))
                global_index += 1
        self.database.upsert_blocks(edition_id, block_rows)

        glossary = GlossaryManager(str(self.project.glossary_dir))
        glossary.load()
        imported_terms = 0
        for item in glossary.glossary.items:
            self.database.import_legacy_concept(
                source=item.src,
                target=item.default_target,
                kind=item.category.value if item.category else "concept",
                description=item.description or "",
            )
            imported_terms += 1

        imported_translations = 0
        for internal_id, chunk in legacy_chunks:
            if chunk.final_translation:
                self.database.import_legacy_translation(
                    block_id=internal_id,
                    status=chunk.status.value,
                    draft=chunk.draft_translation or "",
                    final=chunk.final_translation,
                    analysis=chunk.analysis or "",
                    warnings=chunk.quality_warnings,
                )
                imported_translations += 1

        return {
            "source_edition_id": edition_id,
            "blocks": len(block_rows),
            "legacy_terms": imported_terms,
            "legacy_translations": imported_translations,
            "database": str(self.database.path),
        }
