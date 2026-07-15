import json
from contextlib import closing

import pytest
from pydantic import ValidationError

from src.agents.glossary_manager import GlossaryManager
from src.core.schemas import Glossary, GlossaryItem, GlossaryRule, TermCategory, TermStatus
from src.core.translator import TranslationConfig, TranslationEngine
from src.core.v4.context import ContextBuilder
from src.core.v4.database import V4Database
from src.core.v4.models import (
    TranslationOutcome,
    V4BlockStatus,
    WorkingTargetDecision,
    WorkingTargetResponse,
    WorkingTargetRule,
)
from src.core.v4.pipeline import V4TranslationPipeline
from src.core.v4.pipeline import V4PipelineConfig
from src.core.v4.target_resolver import TargetResolver


class FakeTargetLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get_model(self, purpose):
        return "fake-target"

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def _database(tmp_path, sources):
    root = tmp_path / "book"
    root.mkdir()
    database = V4Database(root)
    edition = database.ensure_source_edition("raw", "normalized", "test", "source.txt")
    database.upsert_blocks(
        edition,
        [
            {
                "id": f"block_{index:03d}",
                "chapter_id": "ch01",
                "chapter_title": "I",
                "chapter_index": 0,
                "block_index": index,
                "global_index": index,
                "block_type": "prose",
                "source_text": source,
                "source_hash": f"hash-{index}",
                "token_count": len(source.split()),
                "status": "ready",
            }
            for index, source in enumerate(sources)
        ],
    )
    return database


def _seed_concept(database, source, *, kind="person", blocks=None):
    concept_id = database.import_legacy_concept(source, "", kind, f"{source} description")
    selected = blocks or database.list_blocks()
    with database.transaction() as connection:
        for block in selected:
            evidence_id = connection.execute(
                """INSERT INTO evidence(
                       block_id, paragraph_id, kind, source_form, evidence_quote,
                       payload_json, confidence, extractor, run_id, created_at
                   ) VALUES(?, 'P000', 'test', ?, ?, '{}', 1.0, 'test', NULL, 'now')""",
                (block.id, source, source),
            ).lastrowid
            connection.execute(
                """INSERT INTO mentions(
                       block_id, paragraph_id, source_form, normalized_form,
                       discourse_function, concept_id, evidence_id
                   ) VALUES(?, 'P000', ?, lower(?), 'referential', ?, ?)""",
                (block.id, source, source, concept_id, evidence_id),
            )
    return concept_id


def _response(*decisions):
    return json.dumps({"decisions": list(decisions)}, ensure_ascii=False)


def test_working_target_models_are_strict_and_bounded():
    rule = WorkingTargetRule(condition={"discourse_function": "vocative"}, target="阁下")
    decision = WorkingTargetDecision(
        concept_id="Q01",
        working_target="塞万里安",
        rules=[rule],
        confidence=0.9,
    )
    assert WorkingTargetResponse(decisions=[decision]).decisions[0].working_target == "塞万里安"

    with pytest.raises(ValidationError):
        WorkingTargetDecision(concept_id="concept-long-id", working_target="塞万里安", confidence=1)
    with pytest.raises(ValidationError):
        WorkingTargetDecision(concept_id="Q01", working_target=" ", confidence=1)
    with pytest.raises(ValidationError):
        WorkingTargetRule(condition={}, target="阁下")
    with pytest.raises(ValidationError):
        WorkingTargetRule(condition={"use": "vocative"}, target="")
    with pytest.raises(ValidationError):
        WorkingTargetDecision(
            concept_id="Q01",
            working_target="塞万里安",
            rules=[rule] * 7,
            confidence=1,
        )
    with pytest.raises(ValidationError):
        WorkingTargetResponse(decisions=[decision] * 25)
    with pytest.raises(ValidationError):
        WorkingTargetResponse.model_validate(
            {"decisions": [], "unexpected": True}
        )


def test_resolver_assigns_one_working_target_to_four_occurrences(tmp_path):
    database = _database(
        tmp_path,
        [
            "Severian waited at the gate.",
            "Severian entered the tower.",
            "Severian spoke to the guard.",
            "Severian left before dawn.",
        ],
    )
    concept_id = _seed_concept(database, "Severian")
    llm = FakeTargetLLM(
        [
            _response(
                {
                    "concept_id": "Q01",
                    "working_target": "塞万里安",
                    "rules": [],
                    "confidence": 0.98,
                }
            )
        ]
    )

    result = TargetResolver(database, llm, max_attempts=1).run()

    assert result["resolved"] == 1
    assert result["queued"] == 0
    with closing(database.connect()) as connection:
        row = connection.execute(
            """SELECT working_target, verified_target, locked
               FROM concepts WHERE id=?""",
            (concept_id,),
        ).fetchone()
    assert tuple(row) == ("塞万里安", "", 0)
    for block in database.list_blocks():
        concept = database.concepts_for_text(block.source_text)[0]
        assert concept["default_target"] == "塞万里安"
        assert concept["working_target"] == "塞万里安"
        assert concept["verified_target"] == ""
        assert concept["target_strength"] == "working"

    payload = json.loads(llm.calls[0]["messages"][-1]["content"])
    assert len(payload["concepts"]) == 1
    assert payload["concepts"][0]["concept_id"] == "Q01"
    assert concept_id not in llm.calls[0]["messages"][-1]["content"]
    assert len(payload["concepts"][0]["contexts"]) == 3
    assert len(payload["concepts"][0]["baseline_translations"]) <= 2


def test_verified_and_locked_targets_are_never_overwritten(tmp_path):
    database = _database(tmp_path, ["Severian waited.", "Severian spoke."])
    concept_id = _seed_concept(database, "Severian")
    with database.transaction() as connection:
        connection.execute(
            """UPDATE concepts SET default_target='终稿', working_target='工作稿',
                      verified_target='审定稿', locked=0 WHERE id=?""",
            (concept_id,),
        )
    llm = FakeTargetLLM([])
    assert TargetResolver(database, llm).run()["resolved"] == 0
    assert llm.calls == []
    concept = database.concepts_for_text("Severian")[0]
    assert concept["default_target"] == "审定稿"
    assert concept["target_strength"] == "verified"

    locked = database.lock_concept_translation("Drotte", "德罗特", kind="person")
    with database.transaction() as connection:
        before = connection.execute(
            "SELECT working_target, verified_target FROM concepts WHERE id=?",
            (locked["concept_id"],),
        ).fetchone()
    applied = database.apply_working_target_decisions(
        [{"concept_id": locked["concept_id"], "target": "错误", "rules": []}]
    )
    with database.transaction() as connection:
        after = connection.execute(
            "SELECT working_target, verified_target FROM concepts WHERE id=?",
            (locked["concept_id"],),
        ).fetchone()
    assert tuple(after) == tuple(before) == ("德罗特", "德罗特")
    assert applied["changed"] == 0


def test_resolver_retries_unknown_alias_and_accepts_reordered_decisions(tmp_path):
    database = _database(
        tmp_path,
        ["Drotte met Severian.", "Drotte followed Severian."],
    )
    drotte_id = _seed_concept(database, "Drotte")
    severian_id = _seed_concept(database, "Severian")
    invalid = _response(
        {"concept_id": "Q03", "working_target": "错误", "rules": [], "confidence": 1}
    )
    valid = _response(
        {"concept_id": "Q02", "working_target": "塞万里安", "rules": [], "confidence": 0.9},
        {"concept_id": "Q01", "working_target": "德罗特", "rules": [], "confidence": 0.9},
    )
    llm = FakeTargetLLM([invalid, valid])

    result = TargetResolver(database, llm, max_attempts=2).run()

    assert result["resolved"] == 2
    assert len(llm.calls) == 2
    with closing(database.connect()) as connection:
        targets = {
            row["id"]: row["working_target"]
            for row in connection.execute(
                "SELECT id, working_target FROM concepts WHERE id IN (?, ?)",
                (drotte_id, severian_id),
            )
        }
    assert targets[drotte_id] == "德罗特"
    assert targets[severian_id] == "塞万里安"


@pytest.mark.parametrize(
    "responses",
    [
        [_response()],
        [RuntimeError("network unavailable")],
    ],
)
def test_missing_alias_or_model_failure_goes_to_human_queue(tmp_path, responses):
    database = _database(tmp_path, ["Severian waited.", "Severian spoke."])
    concept_id = _seed_concept(database, "Severian")

    result = TargetResolver(
        database, FakeTargetLLM(responses), max_attempts=1
    ).run()

    assert result["resolved"] == 0
    assert result["queued"] == 1
    with closing(database.connect()) as connection:
        assert connection.execute(
            "SELECT working_target FROM concepts WHERE id=?", (concept_id,)
        ).fetchone()[0] == ""
    queued = database.list_human_queue()
    assert len(queued) == 1
    assert queued[0]["kind"] == "working_target_required"


def test_success_closes_only_matching_working_target_queue_items(tmp_path):
    database = _database(tmp_path, ["Severian waited.", "Severian spoke."])
    concept_id = _seed_concept(database, "Severian")
    other_id = database.import_legacy_concept("Other", "", "person", "other")
    database.enqueue_working_target_review([concept_id], "first failure")
    database.enqueue_working_target_review([concept_id], "same failure again")
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO human_queue(
                   block_id, kind, severity, payload_json, created_at
               ) VALUES(NULL, 'working_target_required', 'blocking', ?, 'now')""",
            (json.dumps({"concept_id": other_id}),),
        )
        connection.execute(
            """INSERT INTO human_queue(
                   block_id, kind, severity, payload_json, created_at
               ) VALUES(NULL, 'unrelated', 'review', ?, 'now')""",
            (json.dumps({"concept_id": concept_id}),),
        )
    assert len(database.list_human_queue()) == 3

    result = TargetResolver(
        database,
        FakeTargetLLM(
            [
                _response(
                    {
                        "concept_id": "Q01",
                        "working_target": "塞万里安",
                        "rules": [],
                        "confidence": 0.9,
                    }
                )
            ]
        ),
        max_attempts=1,
    ).run()

    assert result["resolved"] == 1
    with closing(database.connect()) as connection:
        rows = connection.execute(
            "SELECT kind, status, payload_json, resolved_at FROM human_queue ORDER BY id"
        ).fetchall()
    matching = [
        row for row in rows
        if row["kind"] == "working_target_required"
        and json.loads(row["payload_json"])["concept_id"] == concept_id
    ]
    assert len(matching) == 1
    assert matching[0]["status"] == "resolved"
    assert matching[0]["resolved_at"] is not None
    assert rows[1]["status"] == "open"
    assert rows[2]["status"] == "open"


def test_single_low_impact_concept_is_not_required_by_verification_queue(tmp_path):
    database = _database(tmp_path, ["Scape shimmered once."])
    concept_id = _seed_concept(
        database, "Scape", kind="concept", blocks=database.list_blocks()[:1]
    )
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO verification_tasks(
                   id, subject_type, subject_id, payload_json, status,
                   required_votes, created_at
               ) VALUES('ordinary-verify', 'concept', ?, '{}', 'open', 2, 'now')""",
            (concept_id,),
        )

    assert database.working_target_candidates() == []
    llm = FakeTargetLLM([])
    assert TargetResolver(database, llm).run()["resolved"] == 0
    assert llm.calls == []


def test_single_low_impact_concept_ignores_candidate_high_impact_flag(tmp_path):
    database = _database(tmp_path, ["Scape shimmered once."])
    concept_id = _seed_concept(
        database, "Scape", kind="concept", blocks=database.list_blocks()[:1]
    )
    database.start_run("risk-run", "adjudicate", {})
    version = database.current_knowledge_version()
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO candidate_clusters(
                   id, run_id, risk_flags_json, affected_blocks_json,
                   affected_block_count, ordinal, state, created_at, updated_at
               ) VALUES('risk-cluster', 'risk-run', '["high_impact"]',
                        '["block_000"]', 1, 0, 'adjudicated', 'now', 'now')"""
        )
        connection.execute(
            """INSERT INTO candidate_adjudications(
                   id, run_id, cluster_id, verdict, payload_hash,
                   selected_candidate_ids_json, entity_kind, confidence, reason,
                   rounds, payload_json, knowledge_version, active, created_at,
                   updated_at
               ) VALUES('risk-adjudication', 'risk-run', 'risk-cluster', 'promote',
                        'hash', '[]', 'concept', 1.0, '', 1, '{}', ?, 1,
                        'now', 'now')""",
            (version,),
        )
        connection.execute(
            """INSERT INTO candidate_resolutions(
                   id, adjudication_id, run_id, cluster_id, candidate_id,
                   concept_id, evidence_id, decision, ordinal, payload_json,
                   created_at
               ) VALUES('risk-resolution', 'risk-adjudication', 'risk-run',
                        'risk-cluster', NULL, ?, NULL, 'promote', 0, '{}', 'now')""",
            (concept_id,),
        )

    assert database.working_target_candidates() == []


def test_resolver_batches_at_24_and_bounds_context_and_baseline_payloads(tmp_path):
    names = [f"Name{index:02d}" for index in range(25)]
    source = " ".join(names)
    database = _database(tmp_path, [source, source])
    for name in names:
        _seed_concept(database, name)
    responses = []
    for count in (24, 1):
        responses.append(
            _response(
                *[
                    {
                        "concept_id": f"Q{index:02d}",
                        "working_target": f"译名{index}",
                        "rules": [],
                        "confidence": 0.8,
                    }
                    for index in range(1, count + 1)
                ]
            )
        )
    llm = FakeTargetLLM(responses)

    result = TargetResolver(database, llm, max_attempts=1).run()

    assert result["resolved"] == 25
    payloads = [json.loads(call["messages"][-1]["content"]) for call in llm.calls]
    assert [len(payload["concepts"]) for payload in payloads] == [24, 1]
    assert all(
        len(item["contexts"]) <= 3 and len(item["baseline_translations"]) <= 2
        for payload in payloads
        for item in payload["concepts"]
    )


def test_rules_render_cleanly_and_working_apply_is_idempotent(tmp_path):
    database = _database(tmp_path, ["Archon spoke.", "Archon listened."])
    concept_id = _seed_concept(database, "Archon", kind="title")
    decision = {
        "concept_id": concept_id,
        "target": "执政官",
        "rules": [
            {
                "condition": {"discourse_function": "vocative"},
                "target": "阁下",
            }
        ],
    }
    first = database.apply_working_target_decisions([decision])
    version_count = database.current_knowledge_version()
    second = database.apply_working_target_decisions([decision])

    assert first["changed"] == 1
    assert second["changed"] == 0
    assert second["knowledge_version"] is None
    assert database.current_knowledge_version() == version_count
    concept = database.concepts_for_text("Archon")[0]
    assert concept["rules"][0]["target"] == "阁下"
    rendered = ContextBuilder._render_concepts([concept])
    assert "核心译名: 执政官" in rendered
    assert "工作译名" in rendered
    assert "→ 阁下" in rendered
    assert "->  " not in rendered
    assert "→  " not in rendered

    glossary = V4TranslationPipeline(database, lambda: None)._glossary_for(
        database.list_blocks()[:1]
    )
    assert glossary.glossary.items[0].default_target == "执政官"
    assert glossary.glossary.items[0].rules[0].target == "阁下"


def test_empty_effective_target_is_context_only_and_never_a_glossary_item(tmp_path):
    database = _database(tmp_path, ["Archon spoke."])
    _seed_concept(database, "Archon", kind="title")
    concept = database.concepts_for_text("Archon")[0]
    rendered = ContextBuilder._render_concepts([concept])
    assert "核心译名: （未定）" in rendered
    assert "→  " not in rendered

    glossary = V4TranslationPipeline(database, lambda: None)._glossary_for(
        database.list_blocks()
    )
    assert glossary.glossary.items == []


def test_target_change_invalidates_dependency_history_and_ready_matches(tmp_path):
    database = _database(
        tmp_path,
        ["Severian waited.", "Severian returned.", "No name here."],
    )
    blocks = database.list_blocks()
    concept_id = _seed_concept(database, "Severian", blocks=blocks[:2])
    with database.transaction() as connection:
        old_id = connection.execute(
            """INSERT INTO translation_versions(
                   block_id, pipeline, knowledge_version, status,
                   final_translation, active, created_at
               ) VALUES(?, 'parallel_v4', 1, 'completed', '旧稿', 0, 'now')""",
            (blocks[0].id,),
        ).lastrowid
        connection.execute(
            """INSERT INTO dependencies(
                   translation_id, dependency_type, dependency_id, knowledge_version
               ) VALUES(?, 'concept', ?, 1)""",
            (old_id, concept_id),
        )
        connection.execute(
            """INSERT INTO translation_versions(
                   block_id, pipeline, knowledge_version, status,
                   final_translation, active, created_at
               ) VALUES(?, 'parallel_v4', 1, 'completed', '现稿', 1, 'now')""",
            (blocks[0].id,),
        )
        connection.execute(
            "UPDATE blocks SET status='completed' WHERE id=?", (blocks[0].id,)
        )

    result = database.apply_working_target_decisions(
        [{"concept_id": concept_id, "target": "塞万里安", "rules": []}]
    )

    assert result["affected_blocks"] == 2
    active = database.active_translations()
    assert active[blocks[0].id]["status"] == "needs_revalidate"
    assert database.get_block_by_identifier(blocks[0].id).status == "needs_revalidate"
    assert database.get_block_by_identifier(blocks[1].id).status == "needs_revalidate"
    assert database.get_block_by_identifier(blocks[2].id).status == "ready"


def test_verification_pending_keeps_the_working_target_visible(tmp_path):
    database = _database(tmp_path, ["Severian waited.", "Severian spoke."])
    concept_id = _seed_concept(database, "Severian")
    database.apply_working_target_decisions(
        [{"concept_id": concept_id, "target": "塞万里安", "rules": []}]
    )
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO verification_tasks(
                   id, subject_type, subject_id, payload_json, status,
                   required_votes, created_at
               ) VALUES('verify-working', 'concept', ?, '{}', 'open', 2, 'now')""",
            (concept_id,),
        )

    concept = database.concepts_for_text("Severian")[0]

    assert concept["verification_pending"] is True
    assert concept["default_target"] == "塞万里安"
    assert "塞万里安（工作译名）" in ContextBuilder._render_concepts([concept])


class CaptureLLM:
    def __init__(self):
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return iter(())


def test_serial_translator_filters_empty_glossary_arrows_and_rules(tmp_path):
    manager = GlossaryManager(str(tmp_path / "glossary"))
    manager.glossary = Glossary(
        items=[
            GlossaryItem(
                id="empty-src",
                src="",
                default_target="空源",
                category=TermCategory.CONCEPT,
                status=TermStatus.VERIFIED,
            ),
            GlossaryItem(
                id="empty-target",
                src="Archon",
                default_target="",
                category=TermCategory.CONCEPT,
                status=TermStatus.VERIFIED,
            ),
            GlossaryItem(
                id="valid",
                src="Archon",
                default_target="执政官",
                category=TermCategory.CONCEPT,
                status=TermStatus.PENDING,
                rules=[
                    GlossaryRule(condition="vocative", target=""),
                    GlossaryRule(condition="direct address", target="阁下"),
                ],
            ),
        ]
    )
    manager._build_patterns()
    llm = CaptureLLM()
    engine = TranslationEngine(
        llm,
        manager,
        config=TranslationConfig(enable_polish=True),
    )

    engine._draft_translate("Archon spoke.")
    engine._polish_translate("Archon spoke.", "执政官发言。")

    prompts = "\n".join(call["messages"][0]["content"] for call in llm.calls)
    assert "Archon -> 执政官" in prompts
    assert "direct address" in prompts and "阁下" in prompts
    assert "vocative" not in prompts
    assert "-> \n" not in prompts
    assert "-  ->" not in prompts


class RecordingPipeline(V4TranslationPipeline):
    def __init__(self, *args, change_target=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen = []
        self.change_target = change_target

    def _translate_island(self, island, knowledge_version, concept_snapshot):
        glossary = self._glossary_for(
            island.blocks, concept_snapshot=concept_snapshot
        )
        targets = [item.default_target for item in glossary.glossary.items]
        self.seen.append((knowledge_version, targets))
        if self.change_target and len(self.seen) == 1:
            self.database.apply_working_target_decisions([self.change_target])
        return [
            TranslationOutcome(
                block=block,
                knowledge_version=knowledge_version,
                status=V4BlockStatus.COMPLETED.value,
                draft_translation="译稿完整。",
                final_translation="译稿完整。",
            )
            for block in island.blocks
        ]


def test_all_islands_keep_one_frozen_target_snapshot_and_version(tmp_path, monkeypatch):
    database = _database(
        tmp_path, ["Severian waited long enough.", "Severian returned home."]
    )
    concept_id = _seed_concept(database, "Severian")
    database.apply_working_target_decisions(
        [{"concept_id": concept_id, "target": "塞万里安", "rules": []}]
    )
    bumps = {"count": 0}
    freezes = {"count": 0}
    original_freeze = database.freeze_translation_knowledge

    def counted_freeze():
        freezes["count"] += 1
        return original_freeze()

    def unrelated_version(*_args, **_kwargs):
        bumps["count"] += 1
        with database.transaction() as connection:
            database.create_knowledge_version("unrelated", connection)
        return None

    monkeypatch.setattr(database, "commit_translation_proposals", unrelated_version)
    monkeypatch.setattr(database, "freeze_translation_knowledge", counted_freeze)
    pipeline = RecordingPipeline(
        database,
        lambda: None,
        config=V4PipelineConfig(
            island_size=1,
            initial_workers=1,
            max_workers=1,
            enable_polish=False,
        ),
    )

    result = pipeline.run()

    assert result["status"] == "completed"
    assert len(pipeline.seen) == 2
    assert pipeline.seen[0][0] == pipeline.seen[1][0]
    assert pipeline.seen[0][1] == pipeline.seen[1][1] == ["塞万里安"]
    assert bumps["count"] == 2
    assert freezes["count"] == 1
    with closing(database.connect()) as connection:
        run = connection.execute(
            "SELECT knowledge_version, config_json FROM runs WHERE id=?",
            (result["run_id"],),
        ).fetchone()
    config = json.loads(run["config_json"])
    assert run["knowledge_version"] == result["frozen_knowledge_version"]
    assert config["frozen_knowledge_version"] == result["frozen_knowledge_version"]
    assert config["target_snapshot_signature"]


def test_freeze_reads_version_and_targets_from_one_sqlite_snapshot(tmp_path, monkeypatch):
    database = _database(tmp_path, ["Severian waited.", "Severian spoke."])
    concept_id = _seed_concept(database, "Severian")
    database.apply_working_target_decisions(
        [{"concept_id": concept_id, "target": "旧译名", "rules": []}]
    )
    old_version = database.current_knowledge_version()
    original_snapshot = database._concept_snapshot_from_connection
    changed = {"done": False}

    def change_between_reads(connection):
        if not changed["done"]:
            changed["done"] = True
            database.apply_working_target_decisions(
                [{"concept_id": concept_id, "target": "新译名", "rules": []}]
            )
        return original_snapshot(connection)

    monkeypatch.setattr(
        database, "_concept_snapshot_from_connection", change_between_reads
    )

    version, snapshot, signature = database.freeze_translation_knowledge()

    frozen = next(item for item in snapshot if item["id"] == concept_id)
    assert version == old_version
    assert frozen["default_target"] == "旧译名"
    assert signature == database.target_snapshot_signature(snapshot)
    assert database.current_knowledge_version() > version
    assert database.concepts_for_text("Severian")[0]["default_target"] == "新译名"


def test_midrun_target_change_stops_before_mixing_and_revalidates_run(tmp_path):
    database = _database(
        tmp_path, ["Severian waited long enough.", "Severian returned home."]
    )
    concept_id = _seed_concept(database, "Severian")
    database.apply_working_target_decisions(
        [{"concept_id": concept_id, "target": "塞万里安", "rules": []}]
    )
    pipeline = RecordingPipeline(
        database,
        lambda: None,
        change_target={"concept_id": concept_id, "target": "塞维利安", "rules": []},
        config=V4PipelineConfig(
            island_size=1,
            initial_workers=1,
            max_workers=1,
            enable_polish=False,
        ),
    )

    result = pipeline.run()

    assert result["status"] == "stale_knowledge"
    assert result["knowledge_stale"] is True
    assert len(pipeline.seen) == 1
    assert pipeline.seen[0][1] == ["塞万里安"]
    assert all(
        block.status == V4BlockStatus.NEEDS_REVALIDATE.value
        for block in database.list_blocks()
    )
    assert set(database.active_translations()) == {"block_000"}
    assert database.active_translations()["block_000"]["status"] == "needs_revalidate"
