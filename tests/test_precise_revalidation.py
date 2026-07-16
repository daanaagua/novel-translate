import hashlib
import json
import re
from contextlib import closing, contextmanager
from dataclasses import replace

import pytest

from src.core.v4.context import ContextBuilder
from src.core.v4.database import KnowledgeSnapshotError, V4Database, render_fingerprint
from src.core.v4.pipeline import V4TranslationPipeline
import src.core.v4.models as v4_models
from src.core.v4.coreference import cache_key, semantic_cache_key
from src.core.v4.models import (
    CoreferenceCase,
    CoreferenceConceptAnchor,
    CoreferenceMention,
    CoreferenceTypeObservation,
    Island,
    RenderingMatchSnapshot,
    TranslationOutcome,
    V4BlockStatus,
)


def _database(tmp_path, texts):
    root = tmp_path / "book"
    root.mkdir(parents=True)
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


def test_claim_fingerprint_tracks_prompt_semantics_but_not_metadata():
    base = {
        "exists": True,
        "active": True,
        "accepted": True,
        "kind": "translation_constraint",
        "statement": "Keep the title neutral.",
        "subject_form": "Archon",
        "scope": "book",
        "reveal_global_index": 7,
        "locked": False,
        "high_impact_constraint": False,
        "confidence": 0.8,
        "evidence_count": 1,
        "created_at": "old",
    }
    metadata = {
        **base,
        "confidence": 0.1,
        "evidence_count": 999,
        "created_at": "new",
    }
    assert render_fingerprint("claim", "claim-1", base) == render_fingerprint(
        "claim", "claim-1", metadata
    )
    assert render_fingerprint("claim", "claim-1", base) != render_fingerprint(
        "claim", "claim-1", {**base, "statement": "Use a different title."}
    )
    assert render_fingerprint("claim", "claim-1", base) != render_fingerprint(
        "claim", "claim-1", {**base, "accepted": False}
    )


def test_claim_create_verify_and_human_resolution_record_semantic_changes(tmp_path):
    database = _database(tmp_path, ["Archon spoke."])
    locked_id = database.create_claim(
        kind="translation_constraint",
        statement="LOCKED",
        reveal_global_index=0,
        subject_form="Archon",
        status="verified",
        locked=True,
    )
    repeated_version = database.current_knowledge_version()
    assert database.create_claim(
        kind="translation_constraint",
        statement="LOCKED",
        reveal_global_index=0,
        subject_form="Archon",
        status="verified",
        locked=True,
    ) == locked_id
    assert database.current_knowledge_version() == repeated_version

    claim_id = database.create_claim(
        kind="translation_constraint",
        statement="PROPOSED",
        reveal_global_index=0,
        subject_form="Archon",
    )
    task = next(
        item
        for item in database.list_verification_tasks()
        if item["subject_id"] == claim_id
    )
    database.start_run("verify-claim", "verify", {})
    assert database.commit_verification_result(
        "verify-claim",
        task,
        [
            {"parsed": {"verdict": "support", "rationale": "yes"}},
            {"parsed": {"verdict": "support", "rationale": "yes"}},
        ],
    ) == "verified"
    with closing(database.connect()) as connection:
        locked_change = connection.execute(
            """SELECT impact_level FROM knowledge_changes
               WHERE subject_type='claim' AND subject_id=? ORDER BY id DESC LIMIT 1""",
            (locked_id,),
        ).fetchone()
        verified_changes = connection.execute(
            """SELECT impact_level FROM knowledge_changes
               WHERE subject_type='claim' AND subject_id=? ORDER BY id""",
            (claim_id,),
        ).fetchall()
    assert locked_change[0] == 3
    assert verified_changes[-1][0] == 2


def test_claim_mutation_failure_rolls_back_state_and_version(tmp_path, monkeypatch):
    database = _database(tmp_path, ["Archon"])
    claim_id = database.create_claim(
        kind="translation_constraint",
        statement="PROPOSED",
        reveal_global_index=0,
    )
    task = next(
        item
        for item in database.list_verification_tasks()
        if item["subject_id"] == claim_id
    )
    database.start_run("verify-failure", "verify", {})
    before = database.current_knowledge_version()
    monkeypatch.setattr(
        database,
        "record_render_change",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("change failed")),
    )
    with pytest.raises(RuntimeError, match="change failed"):
        database.commit_verification_result(
            "verify-failure",
            task,
            [
                {"parsed": {"verdict": "support", "rationale": "yes"}},
                {"parsed": {"verdict": "support", "rationale": "yes"}},
            ],
        )
    assert database.current_knowledge_version() == before
    with closing(database.connect()) as connection:
        assert connection.execute(
            "SELECT status FROM claims WHERE id=?", (claim_id,)
        ).fetchone()[0] == "proposed"
        assert connection.execute(
            "SELECT COUNT(*) FROM verification_votes WHERE task_id=?", (task["id"],)
        ).fetchone()[0] == 0


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


def test_record_render_change_derives_kind_and_impact_instead_of_trusting_label(
    tmp_path
):
    database = _database(tmp_path, ["text"])
    version = database.current_knowledge_version()
    with database.transaction() as connection:
        target = database.record_render_change(
            connection,
            subject_type="lexeme",
            subject_id="lex-1",
            old_state={"working_target": "A"},
            new_state={"working_target": "B"},
            change_kind="human_lock",
            reason="forged label",
            knowledge_version=version,
        )
        rule = database.record_render_change(
            connection,
            subject_type="lexeme",
            subject_id="lex-2",
            old_state={"working_target": "A", "rules": []},
            new_state={
                "working_target": "A",
                "rules": [
                    {
                        "condition": {"speaker": "A"},
                        "target": "B",
                        "priority": 1,
                    }
                ],
            },
            change_kind="target",
            reason="downgraded label",
            knowledge_version=version,
        )
    assert (target["change_kind"], target["impact_level"]) == ("target", 1)
    assert (rule["change_kind"], rule["impact_level"]) == (
        "rendering_rule",
        2,
    )


def test_record_render_change_rejects_old_version_and_reuses_exact_duplicate(
    tmp_path
):
    database = _database(tmp_path, ["text"])
    old_version = database.current_knowledge_version()
    with database.transaction() as connection:
        current_version = database.create_knowledge_version("new current", connection)
        with pytest.raises(ValueError, match="current knowledge version"):
            database.record_render_change(
                connection,
                subject_type="lexeme",
                subject_id="lex-old",
                old_state={"working_target": "A"},
                new_state={"working_target": "B"},
                change_kind="target",
                reason="stale",
                knowledge_version=old_version,
            )
        first = database.record_render_change(
            connection,
            subject_type="lexeme",
            subject_id="lex-current",
            old_state={"working_target": "A"},
            new_state={"working_target": "B"},
            change_kind="human_lock",
            reason="first",
            knowledge_version=current_version,
        )
        repeated = database.record_render_change(
            connection,
            subject_type="lexeme",
            subject_id="lex-current",
            old_state={"working_target": "A"},
            new_state={"working_target": "B"},
            change_kind="target",
            reason="repeat",
            knowledge_version=current_version,
        )
    assert repeated["change_id"] == first["change_id"]
    with closing(database.connect()) as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM knowledge_changes
               WHERE knowledge_version=? AND subject_id='lex-current'""",
            (current_version,),
        ).fetchone()[0] == 1


def test_working_target_uses_its_write_version_for_one_render_change(tmp_path):
    database = _database(tmp_path, ["Archon"])
    concept_id = database.import_legacy_concept("Archon", "", "title", "office")
    result = database.apply_working_target_decisions(
        [{"concept_id": concept_id, "target": "执政官", "rules": []}]
    )
    with closing(database.connect()) as connection:
        changes = connection.execute(
            "SELECT * FROM knowledge_changes WHERE knowledge_version=? ORDER BY id",
            (result["knowledge_version"],),
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
                claim_dependencies=[
                    v4_models.ClaimDependencySnapshot("claim-1", "a" * 64),
                    v4_models.ClaimDependencySnapshot("claim-1", "a" * 64),
                ],
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


def test_frozen_claim_dependency_keeps_semantic_fingerprint_after_same_id_mutates(
    tmp_path
):
    snapshot_type = getattr(v4_models, "ClaimDependencySnapshot", None)
    assert snapshot_type is not None, "ClaimDependencySnapshot must be public"
    database = _database(tmp_path, ["Archon spoke."])
    claim_id = database.create_claim(
        kind="translation_constraint",
        statement="OLD STATEMENT",
        reveal_global_index=0,
        subject_form="Archon",
        status="verified",
        locked=True,
    )
    block = database.list_blocks()[0]
    frozen = database.freeze_render_bundle([block.id])
    frozen_claim = frozen.claims_by_block[block.id][0]
    assert re.fullmatch(r"[0-9a-f]{64}", frozen_claim["semantic_fingerprint"])
    dependency = snapshot_type(
        claim_id=claim_id,
        semantic_fingerprint=frozen_claim["semantic_fingerprint"],
    )
    database.start_run(
        "run-claim-fingerprint",
        "translate",
        {
            "frozen_knowledge_version": frozen.knowledge_version,
            "target_snapshot_signature": frozen.signature,
        },
        knowledge_version=frozen.knowledge_version,
    )
    database.commit_translation_batch(
        "run-claim-fingerprint",
        [
            TranslationOutcome(
                block=block,
                knowledge_version=frozen.knowledge_version,
                status=V4BlockStatus.COMPLETED.value,
                claim_dependencies=(dependency,),
            )
        ],
    )
    with database.transaction() as connection:
        version = database.create_knowledge_version("update claim statement", connection)
        old_state = database._claim_state_for_subject(connection, claim_id)
        connection.execute(
            "UPDATE claims SET statement='NEW STATEMENT' WHERE id=?", (claim_id,)
        )
        database.record_render_change(
            connection,
            subject_type="claim",
            subject_id=claim_id,
            old_state=old_state,
            new_state=database._claim_state_for_subject(connection, claim_id),
            change_kind="metadata",
            reason="update claim statement",
            knowledge_version=version,
        )
    next_bundle = database.freeze_render_bundle([block.id])
    assert next_bundle.claims_by_block[block.id][0][
        "semantic_fingerprint"
    ] != frozen_claim["semantic_fingerprint"]
    with closing(database.connect()) as connection:
        stored = connection.execute(
            """SELECT dependency_fingerprint FROM dependencies
               WHERE dependency_type='claim' AND dependency_id=?""",
            (claim_id,),
        ).fetchone()[0]
    assert stored == frozen_claim["semantic_fingerprint"]


def test_translation_commit_rejects_unknown_and_run_mismatched_versions(tmp_path):
    database = _database(tmp_path, ["Alpha"])
    block = database.list_blocks()[0]
    database.start_run("run-version", "translate", {})
    unknown = TranslationOutcome(
        block=block,
        knowledge_version=999999,
        status=V4BlockStatus.COMPLETED.value,
        matched_renderings=(_match("lex-a", "Alpha", 0, 5, "A"),),
    )
    with pytest.raises(ValueError, match="knowledge version"):
        database.commit_translation_batch("run-version", [unknown])

    run_version = database.current_knowledge_version()
    database.create_claim(
        kind="interpretation_hypothesis",
        statement="newer",
        reveal_global_index=0,
    )
    mismatched = replace(unknown, knowledge_version=database.current_knowledge_version())
    assert mismatched.knowledge_version != run_version
    with pytest.raises(ValueError, match="run.*knowledge version|knowledge version.*run"):
        database.commit_translation_batch("run-version", [mismatched])
    with closing(database.connect()) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM translation_versions"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 0


def test_translation_commit_rejects_inconsistent_run_bundle_config(tmp_path):
    database = _database(tmp_path, ["Alpha"])
    block = database.list_blocks()[0]
    version = database.current_knowledge_version()
    database.start_run(
        "run-bad-bundle",
        "translate",
        {
            "frozen_knowledge_version": version + 1,
            "target_snapshot_signature": "0" * 64,
        },
        knowledge_version=version,
    )
    with pytest.raises(ValueError, match="bundle|frozen"):
        database.commit_translation_batch(
            "run-bad-bundle",
            [
                TranslationOutcome(
                    block=block,
                    knowledge_version=version,
                    status=V4BlockStatus.COMPLETED.value,
                    matched_renderings=(
                        _match("lex-a", "Alpha", 0, 5, "A"),
                    ),
                )
            ],
        )


def test_translation_commit_snapshots_inputs_before_transaction_and_rejects_duck_matches(
    tmp_path, monkeypatch
):
    database = _database(tmp_path, ["Alpha"])
    block = database.list_blocks()[0]
    database.start_run("run-snapshot", "translate", {})
    outcome = TranslationOutcome(
        block=block,
        knowledge_version=database.current_knowledge_version(),
        status=V4BlockStatus.COMPLETED.value,
        final_translation="SAFE",
    )
    original_transaction = database.transaction

    @contextmanager
    def mutate_after_entry_snapshot():
        outcome.final_translation = "EVIL"
        with original_transaction() as connection:
            yield connection

    monkeypatch.setattr(database, "transaction", mutate_after_entry_snapshot)
    database.commit_translation_batch("run-snapshot", [outcome])
    with closing(database.connect()) as connection:
        assert connection.execute(
            "SELECT final_translation FROM translation_versions"
        ).fetchone()[0] == "SAFE"

    class EvilDuck:
        @property
        def lexeme_id(self):
            raise AssertionError("getter must never run")

    second = _database(tmp_path / "duck", ["Alpha"])
    second.start_run("run-duck", "translate", {})
    evil = TranslationOutcome(
        block=second.list_blocks()[0],
        knowledge_version=second.current_knowledge_version(),
        status=V4BlockStatus.COMPLETED.value,
        matched_renderings=(EvilDuck(),),
    )
    with pytest.raises(ValueError, match="RenderingMatchSnapshot|mapping|snapshot"):
        second.commit_translation_batch("run-duck", [evil])


def test_translation_proposals_record_one_shared_semantic_change_and_repeat_noop(
    tmp_path
):
    database = _database(tmp_path, ["NovelTerm appeared."])
    database.start_run("proposal-run", "translate", {})
    block = database.list_blocks()[0]
    outcome = TranslationOutcome(
        block=block,
        knowledge_version=database.current_knowledge_version(),
        status=V4BlockStatus.COMPLETED.value,
        term_proposals=[
            {
                "src": "NovelTerm",
                "tgt": "NOVEL",
                "type": "concept",
                "context": "term",
            }
        ],
    )
    version = database.commit_translation_proposals("proposal-run", [outcome])
    assert version is not None
    with closing(database.connect()) as connection:
        changes = connection.execute(
            "SELECT impact_level FROM knowledge_changes WHERE knowledge_version=?",
            (version,),
        ).fetchall()
        before = (
            connection.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM knowledge_changes").fetchone()[0],
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM dependencies WHERE dependency_type='concept'"
        ).fetchone()[0] == 0
    assert [row[0] for row in changes] == [2]
    assert database.commit_translation_proposals("proposal-run", [outcome]) is None
    with closing(database.connect()) as connection:
        after = (
            connection.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM knowledge_changes").fetchone()[0],
        )
    assert after == before


def test_translation_proposal_target_only_change_is_impact_one(tmp_path):
    database = _database(tmp_path, ["Archon"])
    concept_id = database.import_legacy_concept("Archon", "", "title", "office")
    with database.transaction() as connection:
        connection.execute("UPDATE concepts SET status='verified' WHERE id=?", (concept_id,))
    database.start_run("proposal-target", "translate", {})
    block = database.list_blocks()[0]
    outcome = TranslationOutcome(
        block=block,
        knowledge_version=database.current_knowledge_version(),
        status=V4BlockStatus.COMPLETED.value,
        term_proposals=[{"src": "Archon", "tgt": "ARCHON", "type": "title"}],
    )
    version = database.commit_translation_proposals("proposal-target", [outcome])
    with closing(database.connect()) as connection:
        row = connection.execute(
            """SELECT impact_level FROM knowledge_changes
               WHERE knowledge_version=? AND subject_id=?""",
            (version, concept_id),
        ).fetchone()
    assert row[0] == 1


def test_k0_claim_and_prior_materialization_are_hard_bounded(tmp_path):
    claims_db = _database(tmp_path / "claims", ["Archon"])
    version = claims_db.current_knowledge_version()
    with claims_db.transaction() as connection:
        connection.executemany(
            """INSERT INTO claims(
                   id, kind, statement, subject_form, status, scope, confidence,
                   reveal_global_index, high_impact, locked, created_version, created_at)
               VALUES(?, 'translation_constraint', ?, '', 'verified', 'book',
                      1.0, 0, 0, 0, ?, 'now')""",
            [(f"claim-{index:04d}", f"statement {index}", version) for index in range(1000)],
        )
    with pytest.raises(KnowledgeSnapshotError, match="claim|limit|bound"):
        claims_db.freeze_render_bundle(["block-0"])

    prior_db = _database(
        tmp_path / "prior",
        ["scape " + ("x" * 10000), "The scape opened."],
    )
    prior_db.import_legacy_concept("scape", "SCAPE", "concept", "world")
    bundle = prior_db.freeze_render_bundle(["block-1"])
    evidence = bundle.prior_concept_evidence_by_block["block-1"]
    assert len(evidence) <= 2
    assert sum(len(item["source_text"]) for item in evidence) <= 3600


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
                        _match("lex-o", changed_text, 0, len(changed_text), "Omega"),
                    ),
                )
            ],
        )


@pytest.mark.parametrize(
    "forged_hash",
    [
        hashlib.sha256(b"Alpha").hexdigest().upper(),
        "not-a-sha256",
        "0" * 64,
    ],
)
def test_exact_dependencies_require_authentic_lowercase_source_hash(
    tmp_path, forged_hash
):
    database = _database(tmp_path, ["Alpha"])
    database.start_run("run-forged-hash", "translate", {})
    original = database.list_blocks()[0]
    forged_block = replace(original, source_hash=forged_hash)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE blocks SET source_hash=? WHERE id=?",
            (forged_hash, original.id),
        )

    with pytest.raises(ValueError, match="hash"):
        database.commit_translation_batch(
            "run-forged-hash",
            [
                TranslationOutcome(
                    block=forged_block,
                    knowledge_version=database.current_knowledge_version(),
                    status=V4BlockStatus.COMPLETED.value,
                    matched_renderings=(
                        _match("lex-a", "Alpha", 0, 5, "A"),
                    ),
                )
            ],
        )
    with closing(database.connect()) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM translation_versions"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM dependencies"
        ).fetchone()[0] == 0


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


def test_merge_only_rewrites_active_translation_dependency_rows(tmp_path):
    database = _database(tmp_path, ["scape"])
    canonical_id = database.import_legacy_concept(
        "scape", "SCAPE", "concept", "canonical"
    )
    old_id = database.import_legacy_concept(
        "scapes", "SCAPES", "concept", "plural"
    )
    version = database.current_knowledge_version()
    with database.transaction() as connection:
        inactive_translation = connection.execute(
            """INSERT INTO translation_versions(
                   block_id, pipeline, knowledge_version, status,
                   final_translation, active, created_at)
               VALUES('block-0', 'parallel_v4', ?, 'completed', 'old', 0, 'old')""",
            (version,),
        ).lastrowid
        active_translation = connection.execute(
            """INSERT INTO translation_versions(
                   block_id, pipeline, knowledge_version, status,
                   final_translation, active, created_at)
               VALUES('block-0', 'parallel_v4', ?, 'completed', 'new', 1, 'new')""",
            (version,),
        ).lastrowid
        connection.executemany(
            """INSERT INTO dependencies(
                   translation_id, dependency_type, dependency_id,
                   knowledge_version, dependency_fingerprint, matched_form,
                   occurrence_count, rendered_target, applied_rule_ids_json,
                   source_spans_json)
               VALUES(?, 'concept', ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    inactive_translation,
                    old_id,
                    version,
                    "historical-fingerprint",
                    "scapes",
                    7,
                    "HISTORICAL",
                    '["historical-rule"]',
                    "[[11,17]]",
                ),
                (
                    active_translation,
                    canonical_id,
                    version,
                    "active-canonical",
                    "scape",
                    1,
                    "SCAPE",
                    "[]",
                    "[[0,5]]",
                ),
                (
                    active_translation,
                    old_id,
                    version,
                    "active-old",
                    "scapes",
                    2,
                    "SCAPES",
                    "[]",
                    "[[6,12]]",
                ),
            ],
        )
        inactive_before = tuple(
            connection.execute(
                "SELECT * FROM dependencies WHERE translation_id=?",
                (inactive_translation,),
            ).fetchone()
        )

    database.merge_concept_forms("scape", ["scapes"])

    with closing(database.connect()) as connection:
        inactive_after = tuple(
            connection.execute(
                "SELECT * FROM dependencies WHERE translation_id=?",
                (inactive_translation,),
            ).fetchone()
        )
        active_rows = connection.execute(
            """SELECT dependency_id, occurrence_count FROM dependencies
               WHERE translation_id=? AND dependency_type='concept'""",
            (active_translation,),
        ).fetchall()
    assert inactive_after == inactive_before
    assert [tuple(row) for row in active_rows] == [(canonical_id, 3)]


def test_rule_rebuild_metadata_does_not_raise_target_change_impact(tmp_path):
    database = _database(tmp_path, ["Archon"])
    concept_id = database.import_legacy_concept(
        "Archon", "", "title", "office"
    )
    rule = {"condition": {"speaker": "A"}, "target": "HONORIFIC"}
    database.apply_working_target_decisions(
        [{"concept_id": concept_id, "target": "A", "rules": [rule]}]
    )
    second = database.apply_working_target_decisions(
        [{"concept_id": concept_id, "target": "B", "rules": [rule]}]
    )
    with closing(database.connect()) as connection:
        change = connection.execute(
            """SELECT impact_level, change_kind FROM knowledge_changes
               WHERE knowledge_version=?""",
            (second["knowledge_version"],),
        ).fetchone()
    assert tuple(change) == (1, "target")


def test_synonymous_rule_id_and_version_rebuild_has_same_state_fingerprint(tmp_path):
    database = _database(tmp_path, ["Archon"])
    concept_id = database.import_legacy_concept("Archon", "", "title", "office")
    database.apply_working_target_decisions(
        [
            {
                "concept_id": concept_id,
                "target": "A",
                "rules": [
                    {"condition": {"speaker": "A"}, "target": "HONORIFIC"}
                ],
            }
        ]
    )
    with database.transaction() as connection:
        lexeme_id = connection.execute(
            "SELECT primary_lexeme_id FROM concepts WHERE id=?", (concept_id,)
        ).fetchone()[0]
        old_state = database._render_state_for_subject(
            connection, "lexeme", lexeme_id
        )
        row = connection.execute(
            """SELECT * FROM rendering_rules
               WHERE lexeme_id=? AND retired_version IS NULL""",
            (lexeme_id,),
        ).fetchone()
        connection.execute(
            "UPDATE rendering_rules SET retired_version=created_version WHERE id=?",
            (row["id"],),
        )
        connection.execute(
            """INSERT INTO rendering_rules(
                   id, lexeme_id, condition_json, target, priority, status,
                   scope, locked, created_version, created_at)
               VALUES('replacement-rule', ?, ?, ?, ?, 'legacy_provisional',
                      ?, ?, ?, 'replacement-time')""",
            (
                lexeme_id,
                row["condition_json"],
                row["target"],
                row["priority"],
                row["scope"],
                row["locked"],
                row["created_version"],
            ),
        )
        new_state = database._render_state_for_subject(
            connection, "lexeme", lexeme_id
        )

    assert old_state["rendering_rules_sha256"] == new_state[
        "rendering_rules_sha256"
    ]
    assert render_fingerprint("lexeme", lexeme_id, old_state) == render_fingerprint(
        "lexeme", lexeme_id, new_state
    )


def test_legacy_import_creates_one_version_change_and_audit_then_replays_noop(
    tmp_path, monkeypatch
):
    database = _database(tmp_path, ["Archon"])
    before = database.current_knowledge_version()
    concept_id = database.import_legacy_concept(
        "Archon", "ARCHON", "title", "office"
    )
    created_version = database.current_knowledge_version()
    assert created_version == before + 1
    with closing(database.connect()) as connection:
        concept = connection.execute(
            "SELECT created_version FROM concepts WHERE id=?", (concept_id,)
        ).fetchone()
        lexeme = connection.execute(
            """SELECT created_version FROM lexemes
               WHERE id=(SELECT primary_lexeme_id FROM concepts WHERE id=?)""",
            (concept_id,),
        ).fetchone()
        rule = connection.execute(
            "SELECT created_version FROM rendering_rules WHERE concept_id=?",
            (concept_id,),
        ).fetchone()
        changes = connection.execute(
            "SELECT impact_level FROM knowledge_changes WHERE knowledge_version=?",
            (created_version,),
        ).fetchall()
        audits = connection.execute(
            "SELECT purpose FROM audit_calls WHERE knowledge_version=?",
            (created_version,),
        ).fetchall()
        counts_before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "knowledge_versions",
                "knowledge_changes",
                "audit_calls",
                "concepts",
                "lexemes",
                "rendering_rules",
            )
        }
    assert concept[0] == lexeme[0] == rule[0] == created_version
    assert [row[0] for row in changes] == [2]
    assert [row[0] for row in audits] == ["legacy_concept_import"]

    assert (
        database.import_legacy_concept("Archon", "ARCHON", "title", "office")
        == concept_id
    )
    with closing(database.connect()) as connection:
        counts_after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in counts_before
        }
    assert counts_after == counts_before

    failure_root = tmp_path / "failure"
    failure_root.mkdir()
    failing = _database(failure_root, ["Other"])
    failing_before = failing.current_knowledge_version()
    monkeypatch.setattr(
        failing,
        "record_audit_call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("audit failed")),
    )
    with pytest.raises(RuntimeError, match="audit failed"):
        failing.import_legacy_concept("Other", "OTHER", "title", "other")
    assert failing.current_knowledge_version() == failing_before
    with closing(failing.connect()) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM concepts WHERE canonical_source='Other'"
        ).fetchone()[0] == 0


def test_full_translation_context_is_frozen_at_k0_and_mixed_bundle_is_rejected(
    tmp_path
):
    database = _database(
        tmp_path,
        ["Old scape evidence.", "Nothing here.", "The scape opened."],
    )
    database.import_legacy_concept("scape", "SCAPE", "concept", "world")
    current = database.list_blocks()[2]
    frozen = database.freeze_render_bundle([current.id])

    claim_id = database.create_claim(
        kind="translation_constraint",
        statement="NEW CLAIM",
        reveal_global_index=current.global_index,
        subject_form="scape",
        status="verified",
        locked=True,
    )
    changed_prior = "New scape evidence."
    with database.transaction() as connection:
        connection.execute(
            "UPDATE blocks SET source_text=?, source_hash=? WHERE id='block-0'",
            (
                changed_prior,
                hashlib.sha256(changed_prior.encode("utf-8")).hexdigest(),
            ),
        )
    next_bundle = database.freeze_render_bundle([current.id])

    assert frozen.knowledge_version < next_bundle.knowledge_version
    assert frozen.claims_by_block[current.id] == ()
    assert [item["id"] for item in next_bundle.claims_by_block[current.id]] == [
        claim_id
    ]
    assert frozen.prior_concept_evidence_by_block[current.id][0][
        "source_text"
    ] == "Old scape evidence."
    assert next_bundle.prior_concept_evidence_by_block[current.id][0][
        "source_text"
    ] == changed_prior
    assert frozen.signature != next_bundle.signature

    old_packet = ContextBuilder(database, 5000).build(
        current,
        knowledge_version=frozen.knowledge_version,
        concept_snapshot=frozen.index,
        frozen_claims=frozen.claims_by_block[current.id],
        frozen_prior_concept_evidence=(
            frozen.prior_concept_evidence_by_block[current.id]
        ),
    )
    assert old_packet.knowledge_version == frozen.knowledge_version
    assert old_packet.matched_claim_ids == []
    assert "NEW CLAIM" not in old_packet.rendered
    assert "Old scape evidence." in old_packet.rendered
    assert changed_prior not in old_packet.rendered
    with pytest.raises(RuntimeError, match="frozen claims"):
        ContextBuilder(database, 5000).build(
            current,
            knowledge_version=frozen.knowledge_version,
            concept_snapshot=frozen.index,
        )

    pipeline = V4TranslationPipeline(database, lambda: None)
    pipeline._active_render_bundle = frozen
    database.import_legacy_concept("other", "OTHER", "concept", "other")
    mixed_bundle = database.freeze_render_bundle([current.id])
    with pytest.raises(RuntimeError, match="exact frozen render bundle"):
        pipeline._translate_island(
            Island(id="mixed", blocks=[current]),
            frozen.knowledge_version,
            mixed_bundle.index,
        )
