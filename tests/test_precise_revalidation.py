import hashlib
import json
from contextlib import closing
from dataclasses import replace

import pytest

from src.core.v4.database import V4Database, render_fingerprint
from src.core.v4.coreference import cache_key, semantic_cache_key
from src.core.v4.models import (
    CoreferenceCase,
    CoreferenceConceptAnchor,
    CoreferenceMention,
    CoreferenceTypeObservation,
    RenderingMatchSnapshot,
    TranslationOutcome,
    V4BlockStatus,
)


def _database(tmp_path, texts):
    root = tmp_path / "book"
    root.mkdir()
    database = V4Database(root)
    edition = database.ensure_source_edition("raw", "normalized", "test", "source.txt")
    database.upsert_blocks(
        edition,
        [
            {
                "id": f"block-{index}",
                "chapter_id": "chapter-1",
                "chapter_title": "One",
                "chapter_index": 0,
                "block_index": index,
                "global_index": index,
                "block_type": "prose",
                "source_text": text,
                "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "token_count": len(text.split()),
                "status": "ready",
            }
            for index, text in enumerate(texts)
        ],
    )
    return database


def _match(
    lexeme_id,
    matched_form,
    start,
    end,
    target,
    *,
    concept_id=None,
    rule_ids=(),
    fingerprint="winner-fingerprint",
):
    return RenderingMatchSnapshot(
        lexeme_id=lexeme_id,
        concept_id=concept_id,
        matched_form=matched_form,
        start_offset=start,
        end_offset=end,
        rendered_target=target,
        applied_rule_ids=tuple(rule_ids),
        dependency_fingerprint=fingerprint,
    )


def _coreference_semantic_case():
    observation = CoreferenceTypeObservation(
        observation_id=1,
        kind="person",
        confidence=0.9,
        source="human",
        evidence_id=7,
    )
    mention = CoreferenceMention(
        mention_id=3,
        request_id="M001",
        lexeme_id="lex-1",
        evidence_id=7,
        block_id="block-1",
        paragraph_id="P000",
        block_kind="prose",
        source_hash="source-hash",
        source_edition_hash="edition-hash",
        source_form="Archon",
        discourse_function="vocative",
        context="Archon spoke.",
        context_source="paragraph",
        start_offset=0,
        end_offset=6,
        type_observations=(observation,),
        concept_anchor_ids=("concept-1",),
    )
    anchor = CoreferenceConceptAnchor(
        concept_id="concept-1",
        kind="person",
        canonical_source="Archon",
        status="verified",
        role="primary",
        anchor_mention_id=3,
        evidence_id=7,
    )
    return CoreferenceCase(
        case_id="R001",
        mention_set_id="mention-set-1",
        knowledge_version=1,
        lexeme_id="lex-1",
        language="en",
        normalized_form="archon",
        canonical_form="Archon",
        mentions=(mention,),
        type_observations=(observation,),
        concept_anchors=(anchor,),
    )


def test_coreference_semantic_cache_ignores_global_version_but_not_case_evidence():
    case = _coreference_semantic_case()
    models = ("model-a", "model-b")
    original = semantic_cache_key(case, models)
    changed_version = replace(case, knowledge_version=99)
    assert semantic_cache_key(changed_version, models) == original
    assert cache_key(changed_version, models) != cache_key(case, models)

    mention = case.mentions[0]
    observation = case.type_observations[0]
    anchor = case.concept_anchors[0]
    variants = (
        replace(case, mentions=(replace(mention, evidence_id=8),)),
        replace(case, mentions=(replace(mention, source_hash="changed"),)),
        replace(case, mentions=(replace(mention, context="changed context"),)),
        replace(case, type_observations=(replace(observation, kind="place"),)),
        replace(case, concept_anchors=(replace(anchor, role="alias"),)),
    )
    assert all(semantic_cache_key(value, models) != original for value in variants)


def test_render_fingerprint_ignores_metadata_but_tracks_render_semantics():
    base = {
        "working_target": "执政官",
        "status": "provisional",
        "description": "old prose",
        "kind": "title",
        "evidence_count": 1,
        "created_at": "yesterday",
        "rules": [
            {
                "id": "rule-1",
                "condition": {"speaker": "A", "discourse_function": "vocative"},
                "target": "阁下",
                "priority": 20,
                "locked": False,
                "status": "provisional",
            }
        ],
    }
    metadata_only = {
        **base,
        "description": "new prose",
        "kind": "person",
        "evidence_count": 99,
        "created_at": "today",
    }
    reordered = {
        **metadata_only,
        "rules": [
            {
                "target": "阁下",
                "locked": False,
                "priority": 20,
                "condition": {"discourse_function": "vocative", "speaker": "A"},
                "id": "rule-1",
                "status": "provisional",
            }
        ],
    }
    assert render_fingerprint("lexeme", "lex-1", base) == render_fingerprint(
        "lexeme", "lex-1", reordered
    )
    changed_rule_id = json.loads(json.dumps(base, ensure_ascii=False))
    changed_rule_id["rules"][0]["id"] = "replacement-rule-id"
    assert render_fingerprint("lexeme", "lex-1", base) == render_fingerprint(
        "lexeme", "lex-1", changed_rule_id
    )
    assert render_fingerprint("lexeme", "lex-1", base) != render_fingerprint(
        "lexeme", "lex-1", {**base, "working_target": "总督"}
    )
    changed_rule = json.loads(json.dumps(base, ensure_ascii=False))
    changed_rule["rules"][0]["condition"]["speaker"] = "B"
    assert render_fingerprint("lexeme", "lex-1", base) != render_fingerprint(
        "lexeme", "lex-1", changed_rule
    )


def test_render_fingerprint_has_clear_type_depth_and_size_limits():
    with pytest.raises(ValueError, match="subject_type"):
        render_fingerprint("", "lex-1", {})
    with pytest.raises(TypeError, match="mapping"):
        render_fingerprint("lexeme", "lex-1", [])
    nested = {}
    cursor = nested
    for _ in range(20):
        cursor["condition"] = {}
        cursor = cursor["condition"]
    with pytest.raises(ValueError, match="depth"):
        render_fingerprint("lexeme", "lex-1", nested)
    with pytest.raises(ValueError, match="string"):
        render_fingerprint("lexeme", "lex-1", {"working_target": "x" * 5000})


@pytest.mark.parametrize(
    ("old", "new", "kind", "expected"),
    [
        ({"description": "a"}, {"description": "b"}, "description", 0),
        ({"working_target": "A"}, {"working_target": "B"}, "description", 1),
        ({"working_target": "A"}, {"working_target": "B"}, "target", 1),
        (
            {"working_target": "A"},
            {"working_target": "A", "rules": [{"id": "r", "condition": {"speaker": "x"}, "target": "B"}]},
            "rendering_rule",
            2,
        ),
        (
            {"working_target": "A", "locked": False},
            {"working_target": "B", "locked": True},
            "human_lock",
            3,
        ),
    ],
)
def test_record_render_change_derives_impact_and_reuses_one_version(
    tmp_path, old, new, kind, expected
):
    database = _database(tmp_path, ["text"])
    before = database.current_knowledge_version()
    with database.transaction() as connection:
        result = database.record_render_change(
            connection,
            subject_type="lexeme",
            subject_id="lex-1",
            old_state=old,
            new_state=new,
            change_kind=kind,
            reason="test change",
            record_metadata=True,
        )
    if old == new or expected == 0 and render_fingerprint("lexeme", "lex-1", old) == render_fingerprint("lexeme", "lex-1", new):
        assert result["knowledge_version"] == before + 1
    else:
        assert result["knowledge_version"] == before + 1
    assert result["impact_level"] == expected
    with closing(database.connect()) as connection:
        change = connection.execute("SELECT * FROM knowledge_changes").fetchone()
        assert int(change["knowledge_version"]) == result["knowledge_version"]
        assert int(change["impact_level"]) == expected
        assert connection.execute("SELECT COUNT(*) FROM revalidation_tasks").fetchone()[0] == 0


def test_repeat_equal_render_change_creates_no_version_or_change(tmp_path):
    database = _database(tmp_path, ["text"])
    before = database.current_knowledge_version()
    state = {"working_target": "A", "description": "same"}
    with database.transaction() as connection:
        result = database.record_render_change(
            connection,
            subject_type="lexeme",
            subject_id="lex-1",
            old_state=state,
            new_state=dict(state),
            change_kind="target",
            reason="repeat",
        )
    assert result["changed"] is False
    assert result["knowledge_version"] is None
    assert database.current_knowledge_version() == before
    with closing(database.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_changes").fetchone()[0] == 0


def test_working_target_uses_its_write_version_for_one_render_change(tmp_path):
    database = _database(tmp_path, ["Archon"])
    concept_id = database.import_legacy_concept("Archon", "", "title", "office")
    result = database.apply_working_target_decisions(
        [{"concept_id": concept_id, "target": "执政官", "rules": []}]
    )
    with closing(database.connect()) as connection:
        changes = connection.execute(
            "SELECT * FROM knowledge_changes ORDER BY id"
        ).fetchall()
        versions = connection.execute(
            "SELECT COUNT(*) FROM knowledge_versions WHERE id=?",
            (result["knowledge_version"],),
        ).fetchone()[0]
    assert versions == 1
    assert len(changes) == 1
    assert changes[0]["knowledge_version"] == result["knowledge_version"]
    assert changes[0]["subject_type"] == "lexeme"
    assert changes[0]["impact_level"] == 1


def test_working_rule_change_is_impact_two_and_failure_rolls_back(tmp_path, monkeypatch):
    database = _database(tmp_path, ["Archon"])
    concept_id = database.import_legacy_concept("Archon", "", "title", "office")
    first = database.apply_working_target_decisions(
        [
            {
                "concept_id": concept_id,
                "target": "执政官",
                "rules": [
                    {
                        "condition": {"discourse_function": "vocative"},
                        "target": "阁下",
                    }
                ],
            }
        ]
    )
    with closing(database.connect()) as connection:
        impact = connection.execute(
            "SELECT impact_level FROM knowledge_changes WHERE knowledge_version=?",
            (first["knowledge_version"],),
        ).fetchone()[0]
    assert impact == 2
    before_version = database.current_knowledge_version()
    with closing(database.connect()) as connection:
        before_target = connection.execute(
            "SELECT working_target FROM lexemes WHERE id=(SELECT primary_lexeme_id FROM concepts WHERE id=?)",
            (concept_id,),
        ).fetchone()[0]

    monkeypatch.setattr(
        database,
        "record_render_change",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("change failed")),
    )
    with pytest.raises(RuntimeError, match="change failed"):
        database.apply_working_target_decisions(
            [{"concept_id": concept_id, "target": "总督", "rules": []}]
        )
    assert database.current_knowledge_version() == before_version
    with closing(database.connect()) as connection:
        after_target = connection.execute(
            "SELECT working_target FROM lexemes WHERE id=(SELECT primary_lexeme_id FROM concepts WHERE id=?)",
            (concept_id,),
        ).fetchone()[0]
    assert after_target == before_target


def test_human_lock_is_impact_three_and_exact_repeat_is_noop(tmp_path):
    database = _database(tmp_path, ["Archon"])
    first = database.lock_concept_translation("Archon", "执政官")
    version = database.current_knowledge_version()
    repeated = database.lock_concept_translation("Archon", "执政官")
    assert repeated["knowledge_version"] is None
    assert database.current_knowledge_version() == version
    with closing(database.connect()) as connection:
        changes = connection.execute(
            "SELECT * FROM knowledge_changes WHERE subject_id=? ORDER BY id",
            (first["concept_id"],),
        ).fetchall()
    assert len(changes) == 1
    assert changes[0]["impact_level"] == 3


def test_coreference_fallback_target_records_one_change_and_repeat_is_noop(tmp_path):
    database = _database(tmp_path, ["Archon"])
    concept_id = database.import_legacy_concept(
        "Archon", "执政官", "title", "office"
    )
    with closing(database.connect()) as connection:
        lexeme_id = connection.execute(
            "SELECT primary_lexeme_id FROM concepts WHERE id=?", (concept_id,)
        ).fetchone()[0]
    before = database.current_knowledge_version()
    result = database.apply_coreference_fallback(lexeme_id, [])
    assert result["changed"] is True
    assert result["knowledge_version"] == before + 1
    repeated = database.apply_coreference_fallback(lexeme_id, [])
    assert repeated["changed"] is False
    assert repeated["knowledge_version"] is None
    with closing(database.connect()) as connection:
        changes = connection.execute(
            "SELECT * FROM knowledge_changes WHERE subject_type='lexeme' AND subject_id=?",
            (lexeme_id,),
        ).fetchall()
    assert len(changes) == 1
    assert changes[0]["knowledge_version"] == result["knowledge_version"]
    assert changes[0]["impact_level"] == 1


def test_commit_aggregates_exact_lexeme_concept_rule_and_claim_dependencies(tmp_path):
    text = "Archon met ARCHON."
    database = _database(tmp_path, [text])
    database.start_run("run-1", "translate", {})
    block = database.list_blocks()[0]
    first_start = text.index("Archon")
    second_start = text.index("ARCHON")
    matches = (
        _match("lex-1", "Archon", first_start, first_start + 6, "执政官", concept_id="concept-1", rule_ids=("rule-1",)),
        _match("lex-1", "ARCHON", second_start, second_start + 6, "阁下", concept_id="concept-1", rule_ids=("rule-2",), fingerprint="winner-2"),
    )
    database.commit_translation_batch(
        "run-1",
        [
            TranslationOutcome(
                block=block,
                knowledge_version=database.current_knowledge_version(),
                status=V4BlockStatus.COMPLETED.value,
                final_translation="译文",
                matched_renderings=matches,
                claim_dependencies=["claim-1", "claim-1"],
            )
        ],
    )
    with closing(database.connect()) as connection:
        rows = connection.execute(
            "SELECT * FROM dependencies ORDER BY dependency_type, dependency_id"
        ).fetchall()
    assert [(row["dependency_type"], row["dependency_id"]) for row in rows] == [
        ("claim", "claim-1"),
        ("concept", "concept-1"),
        ("lexeme", "lex-1"),
        ("rule", "rule-1"),
        ("rule", "rule-2"),
    ]
    lexeme = next(row for row in rows if row["dependency_type"] == "lexeme")
    assert lexeme["occurrence_count"] == 2
    assert json.loads(lexeme["source_spans_json"]) == [[0, 6], [11, 17]]
    assert json.loads(lexeme["applied_rule_ids_json"]) == ["rule-1", "rule-2"]
    assert lexeme["matched_form"] == "ARCHON"
    assert "target_count" in lexeme["rendered_target"]
    assert len(lexeme["dependency_fingerprint"]) == 64


def test_spans_are_bounded_but_occurrence_count_is_complete_and_commit_is_idempotent(tmp_path):
    text = " ".join(["Term"] * 1000)
    database = _database(tmp_path, [text])
    database.start_run("run-scale", "translate", {})
    block = database.list_blocks()[0]
    matches = tuple(
        _match("lex-term", "Term", start, start + 4, "术语")
        for start in range(0, len(text), 5)
    )
    outcome = TranslationOutcome(
        block=block,
        knowledge_version=database.current_knowledge_version(),
        status=V4BlockStatus.COMPLETED.value,
        matched_renderings=matches,
    )
    database.commit_translation_batch("run-scale", [outcome])
    database.commit_translation_batch("run-scale", [outcome])
    with closing(database.connect()) as connection:
        translations = connection.execute("SELECT COUNT(*) FROM translation_versions").fetchone()[0]
        row = connection.execute("SELECT * FROM dependencies").fetchone()
    assert translations == 1
    assert row["occurrence_count"] == 1000
    assert len(json.loads(row["source_spans_json"])) == 128


def test_unicode_combination_span_is_valid_and_defaults_are_immutable(tmp_path):
    text = "Cafe\u0301"
    database = _database(tmp_path, [text])
    database.start_run("run-unicode", "translate", {})
    block = database.list_blocks()[0]
    empty = TranslationOutcome(
        block=block,
        knowledge_version=database.current_knowledge_version(),
        status=V4BlockStatus.INCOMPLETE_REQUIRES_HUMAN.value,
    )
    assert empty.matched_renderings == ()
    outcome = TranslationOutcome(
        block=block,
        knowledge_version=database.current_knowledge_version(),
        status=V4BlockStatus.COMPLETED.value,
        matched_renderings=(
            _match("lex-cafe", text, 0, len(text), "咖啡馆"),
        ),
    )
    database.commit_translation_batch("run-unicode", [outcome])
    with closing(database.connect()) as connection:
        row = connection.execute("SELECT source_spans_json FROM dependencies").fetchone()
    assert json.loads(row[0]) == [[0, 5]]


def test_source_or_span_drift_rolls_back_entire_batch(tmp_path):
    database = _database(tmp_path, ["Alpha", "Beta"])
    database.start_run("run-drift", "translate", {})
    first, second = database.list_blocks()
    outcomes = [
        TranslationOutcome(
            block=first,
            knowledge_version=database.current_knowledge_version(),
            status=V4BlockStatus.COMPLETED.value,
            matched_renderings=(_match("lex-a", "Alpha", 0, 5, "甲"),),
        ),
        TranslationOutcome(
            block=second,
            knowledge_version=database.current_knowledge_version(),
            status=V4BlockStatus.COMPLETED.value,
            matched_renderings=(_match("lex-b", "WRONG", 0, 4, "乙"),),
        ),
    ]
    with pytest.raises(ValueError, match="span|source"):
        database.commit_translation_batch("run-drift", outcomes)
    with closing(database.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM translation_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM audit_calls").fetchone()[0] == 0


def test_source_text_must_still_match_its_real_sha256(tmp_path):
    database = _database(tmp_path, ["Alpha"])
    database.start_run("run-hash-drift", "translate", {})
    original = database.list_blocks()[0]
    changed_text = "Omega"
    changed_block = replace(original, source_text=changed_text)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE blocks SET source_text=? WHERE id=?",
            (changed_text, original.id),
        )
    with pytest.raises(ValueError, match="hash"):
        database.commit_translation_batch(
            "run-hash-drift",
            [
                TranslationOutcome(
                    block=changed_block,
                    knowledge_version=database.current_knowledge_version(),
                    status=V4BlockStatus.COMPLETED.value,
                    matched_renderings=(
                        _match("lex-o", changed_text, 0, len(changed_text), "欧米伽"),
                    ),
                )
            ],
        )


def test_translation_proposals_do_not_backfill_false_exact_dependencies(tmp_path):
    database = _database(tmp_path, ["NovelTerm appeared."])
    database.start_run("run-proposal", "translate", {})
    block = database.list_blocks()[0]
    outcome = TranslationOutcome(
        block=block,
        knowledge_version=database.current_knowledge_version(),
        status=V4BlockStatus.COMPLETED.value,
        term_proposals=[
            {"src": "NovelTerm", "tgt": "新术语", "type": "concept", "context": "term"}
        ],
    )
    database.commit_translation_batch("run-proposal", [outcome])
    database.commit_translation_proposals("run-proposal", [outcome])
    with closing(database.connect()) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM dependencies WHERE dependency_type='concept'"
        ).fetchone()[0] == 0


def test_replacement_keeps_historical_dependencies_on_old_translation(tmp_path):
    database = _database(tmp_path, ["Alpha"])
    block = database.list_blocks()[0]
    outcomes_by_run = {}
    for ordinal, target in enumerate(("甲", "阿尔法"), start=1):
        run_id = f"run-{ordinal}"
        database.start_run(run_id, "translate", {})
        outcome = TranslationOutcome(
                    block=block,
                    knowledge_version=database.current_knowledge_version(),
                    status=V4BlockStatus.COMPLETED.value,
                    matched_renderings=(
                        _match("lex-a", "Alpha", 0, 5, target, fingerprint=f"winner-{ordinal}"),
                    ),
                )
        outcomes_by_run[run_id] = outcome
        database.commit_translation_batch(run_id, [outcome])
    database.commit_translation_batch("run-1", [outcomes_by_run["run-1"]])
    with closing(database.connect()) as connection:
        translations = connection.execute(
            "SELECT id, active FROM translation_versions ORDER BY id"
        ).fetchall()
        dependencies = connection.execute(
            "SELECT translation_id, rendered_target FROM dependencies ORDER BY translation_id"
        ).fetchall()
    assert [row["active"] for row in translations] == [0, 1]
    assert [row["translation_id"] for row in dependencies] == [
        translations[0]["id"],
        translations[1]["id"],
    ]
    assert [row["rendered_target"] for row in dependencies] == ["甲", "阿尔法"]
