import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from src.core.history import TranslationMemory
from src.core.schemas import Chapter, ChunkStatus, TextChunk
from src.core.v4.context import ContextBuilder, ContextOverflow
from src.core.v4.database import V4Database
from src.core.v4.exporter import ParallelV4BookExporter
from src.core.v4.migration import V4Migrator
from src.core.v4.models import ScanOutcome, ScanResponse
from src.core.v4.pipeline import V4PipelineConfig, V4TranslationPipeline
from src.core.v4.scanner import V4Scanner
from src.core.v4.validation import V4Validator


class FakeScanLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def get_model(self, purpose):
        return "fake-scan"

    def chat(self, **kwargs):
        self.calls += 1
        return next(self.responses)


class FakeTranslationLLM:
    def __init__(self, counter=None):
        self.counter = counter if counter is not None else {"calls": 0}

    def get_model(self, purpose):
        return f"fake-{purpose}"

    def chat(self, messages, purpose="draft", stream=False, **kwargs):
        self.counter["calls"] += 1
        source = messages[-1]["content"].split("<text_to_translate>\n", 1)[-1].split(
            "\n</text_to_translate>", 1
        )[0]
        paragraphs = [part.strip() for part in source.split("\n\n") if part.strip()]
        translated = "\n\n".join(f"译文：{part}" for part in paragraphs)
        payload = json.dumps(
            {
                "analysis": "保持信息",
                "translation": translated,
                "memory_summary": "本岛继续",
                "new_terms": [
                    {"src": "Archon", "tgt": "执政官", "type": "title", "context": "职衔"}
                ]
                if "Archon" in source
                else [],
                "relations": [],
            },
            ensure_ascii=False,
        )

        def generator():
            yield ("content", payload)

        return generator() if stream else payload


class ParallelV4Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "book"
        self.root.mkdir(parents=True)
        self.database = V4Database(self.root)
        self.edition = self.database.ensure_source_edition("raw", "normalized", "test", "source.txt")

    def tearDown(self):
        self.temp.cleanup()

    def add_blocks(self, texts, status="pending"):
        rows = []
        for index, source in enumerate(texts):
            rows.append(
                {
                    "id": f"ch01_{index:03d}",
                    "chapter_id": "ch01",
                    "chapter_title": "第一章",
                    "chapter_index": 0,
                    "block_index": index,
                    "global_index": index,
                    "block_type": "prose",
                    "source_text": source,
                    "source_hash": f"hash-{index}",
                    "token_count": len(source.split()),
                    "status": status,
                }
            )
        self.database.upsert_blocks(self.edition, rows)
        return self.database.list_blocks()

    def test_scan_is_strict_and_reconcile_is_conservative(self):
        blocks = self.add_blocks(["Archon spoke.", "archon waited.", "Archons gathered."])
        valid = json.dumps(
            {
                "mentions": [
                    {
                        "paragraph_id": "P000",
                        "source_form": "Archon",
                        "category": "title",
                        "suggested_target": "执政官",
                        "description": "职衔",
                        "discourse_function": "referential",
                        "evidence_quote": "Archon",
                        "confidence": 0.9,
                    }
                ],
                "ambiguities": [],
            }
        )
        scanner = V4Scanner(self.database, FakeScanLLM([valid]), max_attempts=1)
        outcome = scanner.scan_block(blocks[0])
        self.assertIsNotNone(outcome.response)

        invalid_scanner = V4Scanner(
            self.database,
            FakeScanLLM([valid.replace('"Archon"', '"Missing"')]),
            max_attempts=1,
        )
        invalid = invalid_scanner.scan_block(blocks[0])
        self.assertIsNone(invalid.response)

        responses = []
        for form in ["Archon", "archon", "Archons"]:
            responses.append(
                ScanOutcome(
                    block=blocks[len(responses)],
                    response=ScanResponse.model_validate(
                        {
                            "mentions": [
                                {
                                    "paragraph_id": "P000",
                                    "source_form": form,
                                    "category": "title",
                                    "suggested_target": "执政官",
                                    "description": "职衔",
                                    "discourse_function": "referential",
                                    "evidence_quote": form,
                                    "confidence": 0.9,
                                }
                            ],
                            "ambiguities": [],
                        }
                    ),
                )
            )
        self.database.start_run("scan-test", "scan", {})
        self.database.commit_scan_batch("scan-test", list(reversed(responses)), "fake")
        self.database.finish_run("scan-test", "completed")
        self.database.reconcile_exact_forms()
        with closing(self.database.connect()) as connection:
            concepts = connection.execute(
                "SELECT normalized_form, concept_id FROM mentions ORDER BY id"
            ).fetchall()
        self.assertEqual(concepts[0]["concept_id"], concepts[1]["concept_id"])
        self.assertNotEqual(concepts[0]["concept_id"], concepts[2]["concept_id"])

    def test_context_only_injects_matched_terms_and_overflow_is_terminal_for_worker(self):
        blocks = self.add_blocks(["The Archon spoke.", "Nothing happened."], status="ready")
        self.database.import_legacy_concept("Archon", "执政官", "concept", "职衔")
        packet = ContextBuilder(self.database, max_context_chars=1000).build(blocks[0])
        self.assertIn("Archon", packet.rendered)
        other = ContextBuilder(self.database, max_context_chars=1000).build(blocks[1])
        self.assertNotIn("Archon", other.rendered)
        with self.assertRaises(ContextOverflow):
            ContextBuilder(self.database, max_context_chars=10).build(blocks[0])

        counter = {"calls": 0}
        result = V4TranslationPipeline(
            self.database,
            llm_factory=lambda: FakeTranslationLLM(counter),
            config=V4PipelineConfig(
                island_size=1,
                initial_workers=1,
                max_workers=1,
                max_context_chars=10,
                enable_polish=False,
                max_blocks=1,
            ),
        ).run()
        self.assertEqual(counter["calls"], 0)
        self.assertEqual(result["incomplete_requires_human"], 1)
        queued = self.database.list_human_queue()
        self.assertEqual(queued[0]["kind"], "context_overflow")
        retried = self.database.resolve_human_item(queued[0]["id"], "retry")
        self.assertEqual(retried["status"], "retried")

    def test_parallel_pipeline_commits_at_barrier_and_can_export(self):
        self.add_blocks(["The Archon spoke.", "The hall answered."], status="ready")
        result = V4TranslationPipeline(
            self.database,
            llm_factory=FakeTranslationLLM,
            config=V4PipelineConfig(
                island_size=1,
                initial_workers=2,
                max_workers=2,
                enable_polish=False,
            ),
        ).run()
        self.assertEqual(result["completed"], 2)
        active = self.database.active_translations()
        self.assertEqual(len(active), 2)
        with closing(self.database.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_calls").fetchone()[0], 2)
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0)

        project = SimpleNamespace(
            root_dir=self.root,
            book_id="test-book",
            config_file=self.root / "config.yaml",
        )
        project.config_file.write_text("title: 测试书\nauthor: 测试者\n", encoding="utf-8")
        report = V4Validator(self.database).validate()
        self.assertEqual(report.high_count, 0)
        exported = ParallelV4BookExporter(project, self.database).export_v4()
        self.assertTrue(exported.txt_path.exists())
        self.assertTrue(exported.epub_path.exists())
        self.assertTrue(exported.quality_report_path.exists())

        second = V4TranslationPipeline(
            self.database,
            llm_factory=FakeTranslationLLM,
            config=V4PipelineConfig(enable_polish=False),
        ).run()
        self.assertEqual(second["completed"], 0)

    def test_interactive_mode_pauses_only_for_a_new_decision(self):
        self.add_blocks(["The Archon spoke.", "The Archon waited."], status="ready")
        first = V4TranslationPipeline(
            self.database,
            llm_factory=FakeTranslationLLM,
            config=V4PipelineConfig(
                island_size=1,
                initial_workers=1,
                max_workers=1,
                enable_polish=False,
                decision_mode="interactive",
            ),
        ).run()
        self.assertEqual(first["status"], "paused_for_review")
        self.assertEqual(first["remaining_islands"], 1)
        with closing(self.database.connect()) as connection:
            queue_count = connection.execute(
                "SELECT COUNT(*) FROM human_queue WHERE kind='translation_proposal'"
            ).fetchone()[0]
        self.assertEqual(queue_count, 1)
        decision = self.database.resolve_human_item(1, "accept")
        self.assertEqual(decision["affected_translations"], 1)
        with closing(self.database.connect()) as connection:
            concept = connection.execute(
                "SELECT status, locked FROM concepts WHERE canonical_source='Archon'"
            ).fetchone()
        self.assertEqual(concept["status"], "verified")
        self.assertEqual(concept["locked"], 1)

        second = V4TranslationPipeline(
            self.database,
            llm_factory=FakeTranslationLLM,
            config=V4PipelineConfig(
                island_size=1,
                initial_workers=1,
                max_workers=1,
                enable_polish=False,
                decision_mode="interactive",
            ),
        ).run()
        self.assertEqual(second["status"], "completed")

    def test_migration_is_idempotent_and_does_not_edit_legacy_chapter(self):
        root = Path(self.temp.name) / "legacy"
        root.mkdir()
        (root / "source.txt").write_text("The Archon spoke.", encoding="utf-8")
        (root / "glossary").mkdir()
        (root / "glossary" / "terms.json").write_text(
            json.dumps([{"src": "Archon", "default_target": "执政官"}]),
            encoding="utf-8",
        )
        memory = TranslationMemory(root)
        chapter = Chapter(
            id="ch01",
            title="Chapter One",
            index=0,
            source_text="The Archon spoke.",
            chunks=[
                TextChunk(
                    id="ch01_000",
                    chapter_id="ch01",
                    index=0,
                    source_text="The Archon spoke.",
                    status=ChunkStatus.COMPLETED,
                    draft_translation="执政官开口。",
                    final_translation="执政官开口。",
                )
            ],
        )
        memory.initialize_chapter(chapter)
        project = SimpleNamespace(
            root_dir=root,
            source_file=root / "source.txt",
            glossary_dir=root / "glossary",
            memory=memory,
        )
        chapter_file = next(memory.chapters_dir.glob("*.json"))
        before = chapter_file.read_bytes()
        first = V4Migrator(project).migrate()
        second = V4Migrator(project).migrate()
        self.assertEqual(first["blocks"], second["blocks"])
        self.assertEqual(before, chapter_file.read_bytes())
        with closing(V4Database(root).connect()) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM translation_versions WHERE pipeline='serial_v3'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

        (root / "source.txt").write_text("The Archon changed.", encoding="utf-8")
        changed = chapter.model_copy(deep=True)
        changed.source_text = "The Archon changed."
        changed.chunks[0].source_text = "The Archon changed."
        changed.chunks[0].status = ChunkStatus.PENDING
        changed.chunks[0].final_translation = None
        memory.initialize_chapter(changed)
        V4Migrator(project).migrate()
        changed_database = V4Database(root)
        self.assertEqual(len(changed_database.list_blocks()), 1)
        with closing(changed_database.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_editions").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM blocks").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
