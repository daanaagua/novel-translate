import json
import io
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

from src.core.history import TranslationMemory
from src.core.schemas import Chapter, ChunkStatus, TextChunk
from src.core.v4.context import ContextBuilder, ContextOverflow
from src.core.v4.database import V4Database
from src.core.v4.exporter import ParallelV4BookExporter
from src.core.v4.migration import V4Migrator
from src.core.v4.models import ScanOutcome, ScanResponse
from src.core.v4.pipeline import V4PipelineConfig, V4TranslationPipeline
from src.core.v4.scanner import ScanProtocolError, V4Scanner
from src.core.v4.semantic_mapper import SemanticMapper
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


class FakePreparationLLM:
    def get_model(self, purpose):
        return f"fake-{purpose}"

    @staticmethod
    def _payload(messages):
        for message in reversed(messages):
            try:
                return json.loads(message["content"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
        raise AssertionError("preparation request did not contain JSON")

    def chat(self, *, messages, purpose, **_kwargs):
        payload = self._payload(messages)
        if purpose == "candidate_adjudication":
            return json.dumps(
                {
                    "decisions": [
                        {
                            "cluster_id": cluster["cluster_id"],
                            "verdict": "promote",
                            "selected_ids": [cluster["alternatives"][0]["id"]],
                            "entity_kind": "person",
                            "confidence": 0.99,
                            "reason": "",
                        }
                        for cluster in payload["clusters"]
                    ]
                }
            )
        if purpose == "working_target":
            return json.dumps(
                {
                    "decisions": [
                        {
                            "concept_id": concept["concept_id"],
                            "working_target": "固定译名",
                            "rules": [],
                            "confidence": 0.99,
                        }
                        for concept in payload["concepts"]
                    ]
                },
                ensure_ascii=False,
            )
        raise AssertionError(f"unexpected preparation purpose: {purpose}")


class FakeReferencePolishLLM:
    def __init__(self):
        self.calls = []

    def get_model(self, purpose):
        return f"fake-{purpose}"

    def chat(self, messages, purpose="draft", stream=False, **kwargs):
        self.calls.append({"purpose": purpose, "messages": messages})
        if purpose == "draft":
            payload = json.dumps(
                {
                    "analysis": "FIRST_LAYER_SELF_EVALUATION",
                    "semantic_obligations": (
                        "同一事件在不同感知层中呈现；保持暗示，不得直接宣布等同。"
                    ),
                    "translation": "这是完整的第一层译文。",
                    "memory_summary": "阿尔法开始。",
                    "new_terms": [],
                    "relations": [],
                },
                ensure_ascii=False,
            )
        else:
            payload = "「这是综合两稿后的完整译文。」"

        def generator():
            yield ("content", payload)

        return generator() if stream else payload


class FakeSemanticLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


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

    def test_local_scan_indexes_candidates_and_legacy_reconcile_is_conservative(self):
        blocks = self.add_blocks(["Archon spoke.", "archon waited.", "Archons gathered."])
        fake = FakeScanLLM([])
        scanner = V4Scanner(self.database, fake, max_attempts=1)
        candidate = next(
            item for item in scanner.extractor.extract(blocks[0])
            if item.normalized_text == "Archon"
        )
        outcome = scanner.scan_block(blocks[0])
        self.assertIsNotNone(outcome.response)
        self.assertEqual(outcome.response.mentions, [])
        self.assertEqual(fake.calls, 0)

        self.database.start_run("candidate-test", "scan", {})
        self.database.commit_candidate_index_batch("candidate-test", [outcome])
        self.database.finish_run("candidate-test", "completed")
        with closing(self.database.connect()) as connection:
            stored = connection.execute(
                "SELECT original_text, selected FROM lexical_candidates WHERE id=?",
                (candidate.id,),
            ).fetchone()
        self.assertEqual(stored["original_text"], "Archon")
        self.assertEqual(stored["selected"], 0)

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
                "SELECT source_form, concept_id FROM mentions ORDER BY id"
            ).fetchall()
            targets = {
                row["canonical_source"]: row["default_target"]
                for row in connection.execute(
                    "SELECT canonical_source, default_target FROM concepts"
                )
            }
        by_form = {}
        for row in concepts:
            by_form.setdefault(row["source_form"], row["concept_id"])
        self.assertEqual(by_form["Archon"], by_form["archon"])
        self.assertNotEqual(by_form["Archon"], by_form["Archons"])
        self.assertEqual(targets["Archon"], "")
        self.assertEqual(targets["Archons"], "")

    def test_scan_repairs_only_uniquely_mislocated_evidence(self):
        response = ScanResponse.model_validate(
            {
                "mentions": [
                    {
                        "paragraph_id": "P000",
                        "source_form": "Zak",
                        "evidence_quote": "Zak",
                    }
                ],
                "ambiguities": [],
            }
        )
        paragraphs = {"P000": "No name here.", "P001": "Zak answered."}
        V4Scanner._repair_unique_evidence_locations(response, paragraphs)
        self.assertEqual(response.mentions[0].paragraph_id, "P001")
        V4Scanner._validate_evidence(response, paragraphs)

        ambiguous = ScanResponse.model_validate(
            {
                "mentions": [
                    {
                        "paragraph_id": "P000",
                        "source_form": "Zak",
                        "evidence_quote": "Zak",
                    }
                ],
                "ambiguities": [],
            }
        )
        duplicate_paragraphs = {"P001": "Zak answered.", "P002": "Zak left."}
        V4Scanner._repair_unique_evidence_locations(ambiguous, duplicate_paragraphs)
        self.assertEqual(ambiguous.mentions[0].paragraph_id, "P000")
        with self.assertRaises(ScanProtocolError):
            V4Scanner._validate_evidence(ambiguous, duplicate_paragraphs)

    def test_local_candidate_index_preserves_exact_curly_punctuation(self):
        block = self.add_blocks(["The Witches’ Keep leaned behind the Old Yard."])[0]
        fake = FakeScanLLM([])
        scanner = V4Scanner(self.database, fake, max_attempts=1)
        candidate = next(
            item for item in scanner.extractor.extract(block)
            if item.normalized_text == "Witches Keep"
        )
        model_payload = candidate.model_payload()
        self.assertNotIn("’", model_payload["candidate"])
        self.assertNotIn("’", model_payload["context"])
        outcome = scanner.scan_block(block)
        self.assertIsNotNone(outcome.response)
        indexed = next(
            item
            for item in outcome.lexical_candidates
            if item["normalized_text"] == "Witches Keep"
        )
        self.assertEqual(indexed["original_text"], "Witches’ Keep")
        self.assertEqual(outcome.response.ambiguities, [])
        self.assertEqual(fake.calls, 0)

    def test_candidate_scan_canonicalizes_safe_title_and_inflection_forms(self):
        self.assertEqual(
            V4Scanner._deterministic_canonical_form(
                "Master Malrubius", "Master Malrubius", "person"
            ),
            "Malrubius",
        )
        self.assertEqual(
            V4Scanner._deterministic_canonical_form(
                "exultants", "exultants", "group"
            ),
            "exultant",
        )
        self.assertEqual(
            V4Scanner._deterministic_canonical_form(
                "Erebus’s", "Erebus", "place"
            ),
            "Erebus",
        )

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

    def test_context_retrieves_grounded_prior_concept_evidence(self):
        blocks = self.add_blocks(
            [
                "The scape was rendered differently for every observer.\n\nNothing else.",
                "They waited without speaking.",
                "She entered the scape.",
            ],
            status="ready",
        )
        self.database.import_legacy_concept(
            "scape",
            "拟景",
            "concept",
            "由计算系统生成、可按观察者分别呈现的感知环境",
        )
        packet = ContextBuilder(self.database, max_context_chars=5000).build(blocks[2])
        self.assertIn("<prior_concept_evidence>", packet.rendered)
        self.assertIn(
            "The scape was rendered differently for every observer.",
            packet.rendered,
        )
        self.assertNotIn("They waited without speaking.", packet.rendered)

    def test_semantic_mapper_extracts_grounded_cross_layer_relation(self):
        source = (
            "As Lahl walked away across the mesa, Rakesh peeked at her version of the scape. "
            "A long, translucent, segmented creature pushed its way briskly through a dense carpet."
        )
        response = json.dumps(
            {
                "relations": [
                    {
                        "relation_type": "same_event_different_rendering",
                        "inference_strength": "strongly_implied",
                        "source_spans": [
                            "Lahl walked away across the mesa",
                            "A long, translucent, segmented creature pushed its way briskly",
                        ],
                        "translation_constraint": (
                            "后句是在拉凯什所见的拟景版本中延续前句的离去动作；"
                            "中文必须让对应关系可推知，但不得直接宣布两者等同。"
                        ),
                    }
                ]
            },
            ensure_ascii=False,
        )
        fake = FakeSemanticLLM(response)
        rendered = SemanticMapper(fake).map(source, "The scape differs by observer.")
        self.assertIn("same_event_different_rendering", rendered)
        self.assertIn("后句是在拉凯什所见的拟景版本中延续前句的离去动作", rendered)
        self.assertEqual(fake.calls[0]["purpose"], "semantic")
        self.assertTrue(fake.calls[0]["json_mode"])

    def test_semantic_mapper_rejects_ungrounded_source_spans(self):
        response = json.dumps(
            {
                "relations": [
                    {
                        "relation_type": "referential_link",
                        "inference_strength": "explicit",
                        "source_spans": ["Missing quote", "Another missing quote"],
                        "translation_constraint": "保留指代。",
                    }
                ]
            }
        )
        self.assertEqual(SemanticMapper(FakeSemanticLLM(response)).map("Actual source."), "")

    def test_human_can_merge_inflected_concept_forms(self):
        self.database.import_legacy_concept(
            "scape", "拟景", "concept", "计算生成的感知环境"
        )
        self.database.import_legacy_concept(
            "scapes", "虚拟场景", "concept", "scape的复数形式"
        )
        result = self.database.merge_concept_forms("scape", ["scapes"])
        self.assertEqual(result["aliases"], ["scapes"])
        matched = self.database.concepts_for_text("Several scapes were available.")
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["source"], "scape")
        self.assertIn("scapes", matched[0]["forms"])

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
        first_block = self.database.list_blocks()[0]
        self.database.record_comparison_vote(
            first_block.id,
            "A",
            "parallel_v4",
            "serial_v3",
            "old-v4-hash",
            "old-baseline-hash",
        )
        forced = V4TranslationPipeline(
            self.database,
            llm_factory=FakeTranslationLLM,
            config=V4PipelineConfig(enable_polish=False, force=True, max_blocks=1),
        ).run()
        self.assertEqual(forced["completed"], 1)
        self.assertIsNone(self.database.comparison_vote_for_block(first_block.id))
        self.assertEqual(len(self.database.list_comparison_vote_history()), 1)

    def test_polish_can_compare_exact_baseline_and_normalizes_quote_style(self):
        block = self.add_blocks(["Alpha begins."], status="ready")[0]
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO baseline_documents(
                       id, source_edition_id, name, kind, file_path, file_sha256,
                       paragraph_count, metadata_json, active, created_at
                   ) VALUES('baseline_test', ?, 'serial_v3', 'docx', 'baseline.docx',
                            'hash', 1, '{}', 1, 'now')""",
                (self.edition,),
            )
            connection.execute(
                """INSERT INTO baseline_paragraphs(
                       baseline_document_id, paragraph_index, target_text, target_hash
                   ) VALUES('baseline_test', 0, '旧译文供逐句查漏。', 'target-hash')"""
            )
            connection.execute(
                """INSERT INTO block_baseline_links(
                       block_id, baseline_document_id, paragraph_index, ordinal,
                       overlap_start, overlap_end, partial_start, partial_end,
                       alignment_status
                   ) VALUES(?, 'baseline_test', 0, 0, 0, 13, 0, 0, 'exact')""",
                (block.id,),
            )
        fake = FakeReferencePolishLLM()
        result = V4TranslationPipeline(
            self.database,
            llm_factory=lambda: fake,
            config=V4PipelineConfig(
                island_size=1,
                initial_workers=1,
                max_workers=1,
                use_baseline_reference=True,
            ),
        ).run()
        self.assertEqual(result["completed"], 1)
        polish = next(call for call in fake.calls if call["purpose"] == "polish")
        self.assertIn("旧译文供逐句查漏。", polish["messages"][-1]["content"])
        self.assertIn("逐句查漏和比较措辞", polish["messages"][-1]["content"])
        self.assertIn("同一事件在不同感知层中呈现", polish["messages"][-1]["content"])
        self.assertNotIn("FIRST_LAYER_SELF_EVALUATION", polish["messages"][-1]["content"])
        final = self.database.active_translations()[block.id]["final_translation"]
        self.assertEqual(final, "“这是综合两稿后的完整译文。”")
        with closing(self.database.connect()) as connection:
            accepted = connection.execute(
                """SELECT accepted FROM audit_calls
                   WHERE run_id=? AND purpose='polish'
                   ORDER BY id DESC LIMIT 1""",
                (result["run_id"],),
            ).fetchone()["accepted"]
        self.assertEqual(accepted, 1)

    def test_polish_falls_back_to_aligned_serial_translation(self):
        block = self.add_blocks(["Alpha begins."], status="ready")[0]
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO translation_versions(
                       block_id, pipeline, knowledge_version, status,
                       draft_translation, final_translation, active, created_at
                   ) VALUES(?, 'serial_v3', 1, 'completed', ?, ?, 1, 'now')""",
                (block.id, "串行旧稿供对照。", "串行旧稿供对照。"),
            )
        fake = FakeReferencePolishLLM()
        result = V4TranslationPipeline(
            self.database,
            llm_factory=lambda: fake,
            config=V4PipelineConfig(
                island_size=1,
                initial_workers=1,
                max_workers=1,
                use_baseline_reference=True,
            ),
        ).run()
        self.assertEqual(result["completed"], 1)
        polish = next(call for call in fake.calls if call["purpose"] == "polish")
        self.assertIn("串行旧稿供对照。", polish["messages"][-1]["content"])

    def test_force_run_can_target_specific_block_ids(self):
        blocks = self.add_blocks(["First block.", "Second block."], status="ready")
        initial = V4TranslationPipeline(
            self.database,
            llm_factory=FakeTranslationLLM,
            config=V4PipelineConfig(enable_polish=False),
        ).run()
        self.assertEqual(initial["completed"], 2)
        targeted = V4TranslationPipeline(
            self.database,
            llm_factory=FakeTranslationLLM,
            config=V4PipelineConfig(
                enable_polish=False,
                force=True,
                include_block_ids=(blocks[1].id,),
            ),
        ).run()
        self.assertEqual(targeted["completed"], 1)
        with closing(self.database.connect()) as connection:
            counts = {
                row["block_id"]: row["version_count"]
                for row in connection.execute(
                    """SELECT block_id, COUNT(*) version_count
                       FROM translation_versions GROUP BY block_id"""
                )
            }
        self.assertEqual(counts[blocks[0].id], 1)
        self.assertEqual(counts[blocks[1].id], 2)

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

    def test_migration_orders_chapters_by_book_index_not_filename(self):
        root = Path(self.temp.name) / "chapter-order"
        root.mkdir()
        (root / "source.txt").write_text("First.\n\nSecond.", encoding="utf-8")
        (root / "glossary").mkdir()
        memory = TranslationMemory(root)
        memory.initialize_chapter(
            Chapter(
                id="z_first",
                title="First",
                index=0,
                source_text="First.",
                chunks=[TextChunk(id="z_first_000", chapter_id="z_first", index=0, source_text="First.")],
            )
        )
        memory.initialize_chapter(
            Chapter(
                id="a_second",
                title="Second",
                index=1,
                source_text="Second.",
                chunks=[TextChunk(id="a_second_000", chapter_id="a_second", index=0, source_text="Second.")],
            )
        )
        project = SimpleNamespace(
            root_dir=root,
            source_file=root / "source.txt",
            glossary_dir=root / "glossary",
            memory=memory,
        )
        V4Migrator(project).migrate()
        blocks = V4Database(root).list_blocks()
        self.assertEqual([block.source_text for block in blocks], ["First.", "Second."])


class ParallelV4CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "book"
        self.root.mkdir(parents=True)
        self.project = SimpleNamespace(root_dir=self.root)

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *arguments):
        import main as cli_main

        output = io.StringIO()
        with (
            patch.object(cli_main.project_manager, "load_project", return_value=self.project),
            redirect_stdout(output),
        ):
            exit_code = cli_main.main(list(arguments))
        return exit_code, output.getvalue()

    def test_scan_v4_is_local_and_does_not_construct_an_llm(self):
        import main as cli_main

        calls = []

        class FakeScanner:
            def __init__(self, database, **kwargs):
                calls.append((database.project_root, kwargs))

            def scan_project(self, **kwargs):
                calls.append(kwargs)
                return {"indexed": 3, "clusters": 2, "failed": 0}

        with (
            patch.object(cli_main, "V4Migrator") as migrator,
            patch.object(cli_main, "V4Scanner", FakeScanner),
            patch.object(cli_main, "LLMManager", side_effect=AssertionError("local scan must not create an LLM")),
        ):
            exit_code, output = self.invoke(
                "scan-v4", "book", "--max-blocks", "3", "--max-attempts", "7",
                "--block", "block-2", "--block", "v01_ch01_003",
            )

        self.assertEqual(exit_code, 0)
        migrator.assert_called_once_with(self.project)
        self.assertEqual(calls[0], (self.root, {"max_attempts": 7}))
        self.assertEqual(calls[1]["max_blocks"], 3)
        self.assertEqual(calls[1]["block_ids"], ["block-2", "v01_ch01_003"])
        self.assertEqual(json.loads(output)["indexed"], 3)

    def test_prepare_v4_unknown_explicit_block_fails_before_constructing_an_llm(self):
        import main as cli_main

        with (
            patch.object(cli_main, "V4Migrator"),
            patch.object(
                cli_main,
                "LLMManager",
                side_effect=AssertionError("unknown block must stop before model stages"),
            ) as llm_manager,
        ):
            exit_code, output = self.invoke(
                "prepare-v4", "book", "--block", "does-not-exist"
            )

        self.assertNotEqual(exit_code, 0)
        llm_manager.assert_not_called()
        result = json.loads(output)
        self.assertEqual(result["failed_stage"], "scan")
        self.assertIn("does-not-exist", result["stages"]["scan"]["error"])

    def test_prepare_cli_makes_scanned_prose_eligible_for_translation_pipeline(self):
        database = V4Database(self.root)
        edition = database.ensure_source_edition(
            "raw", "normalized", "test", "source.txt"
        )
        database.upsert_blocks(
            edition,
            [
                {
                    "id": "block-1",
                    "legacy_id": "v01_ch01_000",
                    "chapter_id": "ch01",
                    "chapter_title": "I",
                    "chapter_index": 0,
                    "block_index": 0,
                    "global_index": 0,
                    "block_type": "prose",
                    "source_text": "Severian waited.",
                    "source_hash": "hash-1",
                    "token_count": 2,
                    "status": "pending",
                }
            ],
        )
        preparation_llm = FakePreparationLLM()
        import main as cli_main

        with (
            patch.object(cli_main, "V4Migrator") as migrator,
            patch.object(cli_main, "LLMManager", return_value=preparation_llm),
            patch.object(
                cli_main.config_loader,
                "load_config",
                return_value={"llm": {}, "parallel_v4": {}},
            ),
        ):
            exit_code, output = self.invoke(
                "prepare-v4", "book", "--max-blocks", "1", "--max-attempts", "1"
            )

        self.assertEqual(exit_code, 0, output)
        migrator.assert_called_once_with(self.project)
        self.assertEqual(
            database.get_block_by_identifier("block-1").status, "ready", output
        )
        counter = {"calls": 0}
        translated = V4TranslationPipeline(
            database,
            llm_factory=lambda: FakeTranslationLLM(counter),
            config=V4PipelineConfig(
                island_size=1,
                initial_workers=1,
                max_workers=1,
                max_blocks=1,
                enable_polish=False,
            ),
        ).run()

        self.assertEqual(translated["completed"], 1)
        self.assertGreater(counter["calls"], 0)

    def test_prepare_v4_runs_stages_in_order_and_forwards_limits(self):
        import main as cli_main

        calls = []

        def stage(name, result):
            def invoke(project, args):
                calls.append(
                    (
                        name,
                        project.root_dir,
                        args.max_blocks,
                        args.max_clusters,
                        args.max_attempts,
                        args.audit_mode,
                    )
                )
                return result

            return invoke

        with (
            patch.object(cli_main, "V4Migrator") as migrator,
            patch.object(cli_main, "_run_scan_v4", stage("scan", {"failed": 0})),
            patch.object(cli_main, "_run_adjudicate_v4", stage("adjudicate", {"failed": 0})),
            patch.object(cli_main, "_run_resolve_targets_v4", stage("resolve", {"queued": 0})),
        ):
            exit_code, output = self.invoke(
                "prepare-v4",
                "book",
                "--max-blocks",
                "8",
                "--max-clusters",
                "13",
                "--max-attempts",
                "2",
                "--audit-mode",
                "full",
            )

        self.assertEqual(exit_code, 0)
        migrator.assert_called_once_with(self.project)
        self.assertEqual([item[0] for item in calls], ["scan", "adjudicate", "resolve"])
        self.assertTrue(all(item[2:] == (8, 13, 2, "full") for item in calls))
        self.assertEqual(json.loads(output)["status"], "completed")

    def test_prepare_v4_stops_after_first_failed_stage(self):
        import main as cli_main

        calls = []

        def failed_scan(project, args):
            calls.append("scan")
            return {"failed": 1}

        def must_not_run(project, args):
            calls.append("unexpected")
            return {"failed": 0}

        with (
            patch.object(cli_main, "V4Migrator"),
            patch.object(cli_main, "_run_scan_v4", failed_scan),
            patch.object(cli_main, "_run_adjudicate_v4", must_not_run),
            patch.object(cli_main, "_run_resolve_targets_v4", must_not_run),
        ):
            exit_code, output = self.invoke("prepare-v4", "book")

        self.assertEqual(exit_code, 1)
        self.assertEqual(calls, ["scan"])
        result = json.loads(output)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_stage"], "scan")

    def test_model_preparation_commands_forward_audit_and_attempt_limits(self):
        import main as cli_main

        constructed = []

        class FakeAdjudicator:
            def __init__(self, llm, **kwargs):
                constructed.append(("adjudicate", kwargs))

            def run(self, *, max_clusters=None):
                constructed.append(("adjudicate-run", max_clusters))
                return {"adjudicated": 4, "failed": 0, "deferred": 1}

        class FakeResolver:
            def __init__(self, database, llm, **kwargs):
                constructed.append(("resolve", kwargs))

            def run(self, max_concepts=None):
                constructed.append(("resolve-run", max_concepts))
                return {"resolved": 3, "queued": 0}

        with (
            patch.object(cli_main, "V4Migrator"),
            patch.object(cli_main, "V4Adjudicator", FakeAdjudicator),
            patch.object(cli_main, "TargetResolver", FakeResolver),
            patch.object(cli_main, "LLMManager", return_value=object()),
            patch.object(cli_main.config_loader, "load_config", return_value={"llm": {}}),
        ):
            adjudicate_code, _ = self.invoke(
                "adjudicate-v4",
                "book",
                "--max-clusters",
                "4",
                "--max-attempts",
                "5",
                "--audit-mode",
                "response",
            )
            resolve_code, _ = self.invoke(
                "resolve-targets-v4",
                "book",
                "--max-concepts",
                "3",
                "--max-attempts",
                "6",
                "--audit-mode",
                "minimal",
            )

        self.assertEqual((adjudicate_code, resolve_code), (0, 0))
        self.assertIn(
            ("adjudicate", {"database": ANY, "max_attempts": 5, "audit_mode": "response"}),
            constructed,
        )
        self.assertIn(("adjudicate-run", 4), constructed)
        self.assertIn(
            ("resolve", {"max_attempts": 6, "audit_mode": "minimal"}),
            constructed,
        )
        self.assertIn(("resolve-run", 3), constructed)

    def test_preparation_cli_rejects_unsafe_negative_limits_before_loading_project(self):
        import main as cli_main

        invalid_commands = (
            ("scan-v4", "book", "--max-blocks", "-1"),
            ("prepare-v4", "book", "--max-blocks", "-1"),
            ("adjudicate-v4", "book", "--max-clusters", "-1"),
            ("prepare-v4", "book", "--max-clusters", "-1"),
            ("scan-v4", "book", "--max-attempts", "0"),
            ("adjudicate-v4", "book", "--max-attempts", "0"),
            ("resolve-targets-v4", "book", "--max-attempts", "0"),
            ("prepare-v4", "book", "--max-attempts", "0"),
        )
        for arguments in invalid_commands:
            with self.subTest(arguments=arguments):
                stderr = io.StringIO()
                with (
                    patch.object(cli_main.project_manager, "load_project") as load,
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    cli_main.main(list(arguments))
                self.assertNotEqual(raised.exception.code, 0)
                load.assert_not_called()
                self.assertIn("must be", stderr.getvalue())

    def test_reset_scan_v4_requires_current_preview_token_and_prints_clean_json(self):
        database = V4Database(self.root)
        edition = database.ensure_source_edition("raw", "normalized", "test", "source.txt")
        database.upsert_blocks(
            edition,
            [
                {
                    "id": "block-1",
                    "legacy_id": "ch01_000",
                    "chapter_id": "ch01",
                    "chapter_title": "One",
                    "chapter_index": 0,
                    "block_index": 0,
                    "global_index": 0,
                    "block_type": "prose",
                    "source_text": "Severian waited.",
                    "source_hash": "hash",
                    "token_count": 2,
                    "status": "pending",
                }
            ]
        )

        preview_code, preview_output = self.invoke("reset-scan-v4", "book", "--preview")
        preview = json.loads(preview_output)
        self.assertEqual(preview_code, 0)
        self.assertTrue(preview["token"])

        wrong_code, wrong_output = self.invoke(
            "reset-scan-v4", "book", "--confirm", "wrong"
        )
        self.assertEqual(wrong_code, 1)
        self.assertIn("token", wrong_output.casefold())

        confirm_code, confirm_output = self.invoke(
            "reset-scan-v4", "book", "--confirm", preview["token"]
        )
        self.assertEqual(confirm_code, 0)
        self.assertIn("blocks_reset", json.loads(confirm_output))

    def test_reset_scan_v4_clears_only_preparation_audit_runs(self):
        database = V4Database(self.root)
        for run_id, stage in (
            ("scan-run", "scan"),
            ("judge-run", "adjudicate"),
            ("target-run", "working_target"),
            ("translate-run", "translate"),
        ):
            database.start_run(run_id, stage, {})
            database.record_audit_call(
                run_id=run_id,
                block_id=None,
                purpose=stage,
                model="fake",
                knowledge_version=database.current_knowledge_version(),
                request={"run": run_id},
                raw_response="failure",
                parsed=None,
                accepted=False,
                attempts=1,
                elapsed_ms=1,
                error="test",
            )
            database.finish_run(run_id, "completed_with_errors")

        human_id = database.record_audit_call(
            run_id="scan-run",
            block_id=None,
            purpose="scan_review",
            model="human-reviewer",
            knowledge_version=database.current_knowledge_version(),
            request={"actor_type": "human", "note": "人工复核"},
            raw_response="人工全文",
            parsed={"actor_type": "human", "decision": "保留"},
            accepted=True,
            attempts=1,
            elapsed_ms=1,
            error=None,
        )
        locator = database.audit_archive.append(
            "scan-run",
            {
                "request": {"actor_type": "human"},
                "raw_response": "人工全文",
                "parsed": {"decision": "保留"},
            },
            stage="manual_review",
        )
        with database.transaction() as connection:
            connection.execute(
                """UPDATE audit_calls
                   SET archive_relative_path=?, archive_offset=?,
                       archive_compressed_length=?, archive_sha256=?
                   WHERE id=?""",
                (
                    locator.relative_path,
                    locator.offset,
                    locator.compressed_length,
                    locator.sha256,
                    human_id,
                ),
            )

        preview = database.preview_scan_reset()
        self.assertEqual(preview["audit_calls"], 3)
        self.assertEqual(preview["runs"], 2)
        with database.transaction() as connection:
            connection.execute(
                "UPDATE audit_calls SET raw_response='人工全文更新' WHERE id=?",
                (human_id,),
            )
        database.reset_scan_derivatives(preview["token"])
        with closing(database.connect()) as connection:
            remaining = connection.execute(
                """SELECT r.stage, a.purpose, a.raw_response,
                          a.archive_relative_path, a.archive_sha256
                   FROM runs r JOIN audit_calls a ON a.run_id=r.id
                   ORDER BY r.stage, a.id"""
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in remaining],
            [
                (
                    "scan",
                    "scan_review",
                    "人工全文更新",
                    locator.relative_path,
                    locator.sha256,
                ),
                ("translate", "translate", "failure", None, None),
            ],
        )
        self.assertEqual(database.read_audit_payload(human_id)["raw_response"], "人工全文")


if __name__ == "__main__":
    unittest.main()
