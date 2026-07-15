import json
from contextlib import closing

import pytest

from src.core.v4.database import KnowledgeSnapshotError, V4Database
from src.core.v4.matcher import (
    AhoConceptMatcher,
    ConceptMatcherCache,
    FrozenConceptIndex,
)
from src.core.v4.models import TranslationOutcome, V4BlockStatus


def _db(tmp_path, texts):
    root = tmp_path / "book"
    root.mkdir()
    db = V4Database(root)
    edition = db.ensure_source_edition("raw", "normalized", "test", "source.txt")
    db.upsert_blocks(
        edition,
        [
            {
                "id": f"block_{index}",
                "chapter_id": "ch01",
                "chapter_title": "I",
                "chapter_index": 0,
                "block_index": index,
                "global_index": index,
                "block_type": "prose",
                "source_text": text,
                "source_hash": f"hash-{index}",
                "token_count": len(text.split()),
                "status": "ready",
            }
            for index, text in enumerate(texts)
        ],
    )
    return db


def test_aho_matcher_preserves_word_boundaries_case_and_nfkc():
    snapshot = [
        {"id": "archon", "forms": ["Archon"]},
        {"id": "fullwidth", "forms": ["Ａｅｔａ"]},
    ]
    matcher = AhoConceptMatcher.from_snapshot(snapshot)

    assert matcher.match("ARCHON met Aeta.") == ("archon", "fullwidth")
    assert matcher.match("Archoness XArchon Archon_2") == ()


def test_matcher_cache_builds_once_for_one_signature():
    ConceptMatcherCache.clear()
    snapshot = [
        {"id": f"c{index}", "forms": [f"Name{index}"]}
        for index in range(2000)
    ]

    first = ConceptMatcherCache.get("same-version", snapshot)
    second = ConceptMatcherCache.get("same-version", list(reversed(snapshot)))

    assert first is second
    assert ConceptMatcherCache.build_count() == 1
    assert first.match("Name1999 and Name1") == ("c1", "c1999")


def test_concepts_for_text_deepcopies_nested_rules_and_reuses_matcher(tmp_path):
    ConceptMatcherCache.clear()
    db = _db(tmp_path, ["Archon spoke."])
    concept_id = db.import_legacy_concept("Archon", "", "title", "office")
    db.apply_working_target_decisions(
        [
            {
                "concept_id": concept_id,
                "target": "执政官",
                "rules": [
                    {
                        "condition": {"mode": "vocative"},
                        "target": "阁下",
                    }
                ],
            }
        ]
    )
    version, snapshot, signature = db.freeze_translation_knowledge()

    first = db.concepts_for_text("Archon", concept_snapshot=snapshot)
    first[0]["rules"][0]["condition"]["mode"] = "polluted"
    second = db.concepts_for_text("ARCHON", concept_snapshot=snapshot)

    assert version > 0 and signature
    assert second[0]["rules"][0]["condition"]["mode"] == "vocative"
    assert snapshot[0]["rules"][0]["condition"]["mode"] == "vocative"
    assert ConceptMatcherCache.build_count() == 0


def test_frozen_index_compiles_once_and_hot_matching_never_walks_vocabulary(
    tmp_path, monkeypatch
):
    db = _db(tmp_path, ["Name1999 spoke.", "Name1 waited.", "Nothing happened."])
    version = db.current_knowledge_version()
    with db.transaction() as connection:
        for index in range(2000):
            concept_id = f"concept-{index:04d}"
            source = f"Name{index}"
            connection.execute(
                """INSERT INTO concepts(
                       id, kind, canonical_source, default_target, working_target,
                       description, status, scope, locked, created_version, created_at
                   ) VALUES(?, 'person', ?, ?, ?, '', 'provisional', 'book', 0, ?, 'now')""",
                (concept_id, source, f"译名{index}", f"译名{index}", version),
            )
            connection.execute(
                """INSERT INTO source_forms(
                       concept_id, form, normalized_form, grammar_json
                   ) VALUES(?, ?, lower(?), '{}')""",
                (concept_id, source, source),
            )

    counts = {"signature": 0, "snapshot": 0, "map": 0, "matcher": 0}
    original_signature = db.target_snapshot_signature
    original_snapshot = FrozenConceptIndex._deep_snapshot
    original_map = FrozenConceptIndex._build_map
    original_matcher = FrozenConceptIndex._build_matcher

    def counted_signature(snapshot):
        counts["signature"] += 1
        return original_signature(snapshot)

    def counted_snapshot(snapshot):
        counts["snapshot"] += 1
        return original_snapshot(snapshot)

    def counted_map(snapshot):
        counts["map"] += 1
        return original_map(snapshot)

    def counted_matcher(snapshot):
        counts["matcher"] += 1
        return original_matcher(snapshot)

    monkeypatch.setattr(db, "target_snapshot_signature", counted_signature)
    monkeypatch.setattr(FrozenConceptIndex, "_deep_snapshot", staticmethod(counted_snapshot))
    monkeypatch.setattr(FrozenConceptIndex, "_build_map", staticmethod(counted_map))
    monkeypatch.setattr(FrozenConceptIndex, "_build_matcher", staticmethod(counted_matcher))

    _, frozen, signature = db.freeze_translation_knowledge()
    assert isinstance(frozen, FrozenConceptIndex)
    assert frozen.signature == signature
    assert counts == {"signature": 1, "snapshot": 1, "map": 1, "matcher": 1}

    for block in db.list_blocks():
        db.concepts_for_text(block.source_text, concept_snapshot=frozen)
    assert counts == {"signature": 1, "snapshot": 1, "map": 1, "matcher": 1}


def test_snapshot_signature_covers_all_prompt_and_matching_semantics(tmp_path):
    db = _db(tmp_path, ["Archon spoke."])
    concept_id = db.import_legacy_concept("Archon", "", "title", "office")
    db.apply_working_target_decisions(
        [{"concept_id": concept_id, "target": "执政官", "rules": []}]
    )
    _, snapshot, original = db.freeze_translation_knowledge()
    variants = []
    for field, value in (
        ("source", "Other"),
        ("kind", "person"),
        ("description", "changed"),
        ("status", "verified"),
        ("locked", True),
        ("working_target", "别名"),
        ("verified_target", "审定"),
        ("default_target", "有效"),
        ("verification_pending", True),
    ):
        changed = json.loads(json.dumps(list(snapshot), ensure_ascii=False))
        changed[0][field] = value
        variants.append(db.target_snapshot_signature(changed))
    changed_form = json.loads(json.dumps(list(snapshot), ensure_ascii=False))
    changed_form[0]["forms"].append("Archons")
    variants.append(db.target_snapshot_signature(changed_form))

    assert all(signature != original for signature in variants)


def test_commit_uses_only_frozen_outcome_concept_dependencies(tmp_path, monkeypatch):
    db = _db(tmp_path, ["Archon spoke."])
    archon = db.import_legacy_concept("Archon", "", "title", "office")
    other = db.import_legacy_concept("Other", "", "person", "other")
    block = db.list_blocks()[0]
    db.start_run("translate-run", "translate", {})
    monkeypatch.setattr(
        db,
        "concepts_for_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("live concept matching is forbidden during commit")
        ),
    )

    db.commit_translation_batch(
        "translate-run",
        [
            TranslationOutcome(
                block=block,
                knowledge_version=db.current_knowledge_version(),
                status=V4BlockStatus.COMPLETED.value,
                final_translation="译文。",
                matched_concept_ids=[other],
            )
        ],
    )

    with closing(db.connect()) as connection:
        dependencies = connection.execute(
            "SELECT dependency_id FROM dependencies WHERE dependency_type='concept'"
        ).fetchall()
    assert [row[0] for row in dependencies] == [other]
    assert archon != other


def test_bad_rule_json_raises_typed_snapshot_error(tmp_path):
    db = _db(tmp_path, ["Archon spoke."])
    concept_id = db.import_legacy_concept("Archon", "", "title", "office")
    version = db.current_knowledge_version()
    with db.transaction() as connection:
        connection.execute(
            """INSERT INTO rendering_rules(
                   id, concept_id, condition_json, target, priority, status,
                   scope, locked, created_version, created_at
               ) VALUES('broken-rule', ?, '{broken', '阁下', 1, 'provisional',
                        'book', 0, ?, 'now')""",
            (concept_id, version),
        )

    with pytest.raises(KnowledgeSnapshotError, match="broken-rule") as error:
        db.freeze_translation_knowledge()

    assert error.value.rule_id == "broken-rule"


def test_atomic_finish_detects_changed_signature_and_rolls_back_on_failure(
    tmp_path, monkeypatch
):
    db = _db(tmp_path, ["Archon spoke."])
    concept_id = db.import_legacy_concept("Archon", "", "title", "office")
    db.apply_working_target_decisions(
        [{"concept_id": concept_id, "target": "旧译名", "rules": []}]
    )
    version, _, signature = db.freeze_translation_knowledge()
    block = db.list_blocks()[0]
    db.start_run("finish-run", "translate", {}, knowledge_version=version)
    db.commit_translation_batch(
        "finish-run",
        [
            TranslationOutcome(
                block=block,
                knowledge_version=version,
                status=V4BlockStatus.COMPLETED.value,
                final_translation="译文。",
                matched_concept_ids=[concept_id],
            )
        ],
    )
    original = db._concept_snapshot_from_connection

    def change_inside_finish(connection):
        connection.execute(
            "UPDATE concepts SET working_target='新译名' WHERE id=?", (concept_id,)
        )
        return original(connection)

    monkeypatch.setattr(db, "_concept_snapshot_from_connection", change_inside_finish)
    status, knowledge_stale = db.finish_translation_run_atomically(
        "finish-run", signature, "completed"
    )
    assert status == "completed_with_errors"
    assert knowledge_stale is True
    assert db.active_translations()[block.id]["status"] == "needs_revalidate"

    db.start_run("failure-run", "translate", {})
    monkeypatch.setattr(
        db,
        "_concept_snapshot_from_connection",
        lambda _connection: (_ for _ in ()).throw(RuntimeError("finish exploded")),
    )
    with pytest.raises(RuntimeError, match="finish exploded"):
        db.finish_translation_run_atomically("failure-run", signature, "completed")
    with closing(db.connect()) as connection:
        assert connection.execute(
            "SELECT status FROM runs WHERE id='failure-run'"
        ).fetchone()[0] == "running"
