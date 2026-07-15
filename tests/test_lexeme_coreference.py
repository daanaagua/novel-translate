from contextlib import closing
from dataclasses import FrozenInstanceError, replace
import json

import pytest

from src.core.v4 import models as v4_models
from src.core.v4 import coreference as coreference_module
from src.core.v4.coreference import CoreferenceCoordinator, CoreferenceProtocolError
from src.core.v4.database import V4Database, normalize_english_form, stable_id


def _db(tmp_path, source_text="Briah met BRIAH.", source_hash="hash-1"):
    root = tmp_path / "book"
    root.mkdir()
    database = V4Database(root)
    edition = database.ensure_source_edition(
        "raw-1", "normalized-1", "test", "source.txt"
    )
    database.upsert_blocks(
        edition,
        [
            {
                "id": "block-1",
                "chapter_id": "ch01",
                "chapter_title": "I",
                "chapter_index": 0,
                "block_index": 0,
                "global_index": 0,
                "block_type": "prose",
                "source_text": source_text,
                "source_hash": source_hash,
                "token_count": len(source_text.split()),
                "status": "ready",
            }
        ],
    )
    return database


def _require_method(database, name):
    method = getattr(database, name, None)
    assert method is not None, f"V4Database.{name} public API is missing"
    return method


def _occurrence(**values):
    model = getattr(v4_models, "FormOccurrence", None)
    assert model is not None, "FormOccurrence public model is missing"
    return model(**values)


def test_lexeme_persistence_models_are_immutable_public_contracts():
    model_values = {
        "LexemeRef": {
            "id": "lexeme-1",
            "language": "en",
            "normalized_form": "briah",
            "canonical_form": "Briah",
        },
        "TypeObservation": {
            "lexeme_id": "lexeme-1",
            "kind": "place",
            "confidence": 0.8,
            "source": "scanner",
        },
        "FormOccurrence": {
            "lexeme_id": "lexeme-1",
            "block_id": "block-1",
            "start_offset": 0,
            "end_offset": 5,
            "source_form": "Briah",
            "source_hash": "hash-1",
        },
    }

    for name, values in model_values.items():
        model = getattr(v4_models, name, None)
        assert model is not None, f"{name} public model is missing"
        instance = model(**values)
        with pytest.raises(FrozenInstanceError):
            setattr(instance, next(iter(values)), "changed")


def test_ensure_lexeme_reuses_stable_id_and_preserves_exact_surface_forms(tmp_path):
    database = _db(tmp_path)
    ensure_lexeme = _require_method(database, "ensure_lexeme")

    first = ensure_lexeme("Briah", language="en")
    repeated = ensure_lexeme("Briah", language="en")
    recased = ensure_lexeme("BRIAH", language="en")
    recased_repeated = ensure_lexeme("BRIAH", language="en")

    expected = stable_id("lexeme", f"en:{normalize_english_form('Briah')}")
    assert first == repeated == recased == recased_repeated == expected
    with closing(database.connect()) as connection:
        lexemes = connection.execute(
            """SELECT id, canonical_form, default_target, working_target,
                      verified_target, status, locked
               FROM lexemes
               WHERE language='en' AND normalized_form='briah'
                     AND retired_version IS NULL"""
        ).fetchall()
        forms = connection.execute(
            """SELECT lexeme_id, form, normalized_form
               FROM source_forms WHERE lexeme_id=? ORDER BY form""",
            (first,),
        ).fetchall()
        concept_count = connection.execute("SELECT COUNT(*) FROM concepts").fetchone()[0]

    assert len(lexemes) == 1
    assert dict(lexemes[0]) == {
        "id": expected,
        "canonical_form": "Briah",
        "default_target": "",
        "working_target": "",
        "verified_target": "",
        "status": "provisional",
        "locked": 0,
    }
    assert [tuple(row) for row in forms] == [
        (expected, "BRIAH", "briah"),
        (expected, "Briah", "briah"),
    ]
    assert concept_count == 0


def test_ensure_lexeme_rejects_empty_language_and_normalized_form(tmp_path):
    database = _db(tmp_path)
    ensure_lexeme = _require_method(database, "ensure_lexeme")

    with pytest.raises(ValueError):
        ensure_lexeme("Briah", language="")
    with pytest.raises(ValueError):
        ensure_lexeme("   ", language="en")


def test_type_observations_coexist_without_creating_or_splitting_concepts(tmp_path):
    database = _db(tmp_path)
    ensure_lexeme = _require_method(database, "ensure_lexeme")
    record = _require_method(database, "record_type_observation")
    lexeme_id = ensure_lexeme("Briah")

    place_id = record(
        lexeme_id,
        "place",
        confidence=0.8,
        source="scanner",
    )
    repeated_place_id = record(
        lexeme_id,
        "place",
        confidence=0.8,
        source="scanner",
    )
    concept_observation_id = record(
        lexeme_id,
        "concept",
        confidence=0.6,
        source="heuristic",
    )

    with closing(database.connect()) as connection:
        observations = connection.execute(
            """SELECT id, lexeme_id, concept_id, kind, confidence, source
               FROM concept_type_observations
               WHERE lexeme_id=? ORDER BY id""",
            (lexeme_id,),
        ).fetchall()
        concept_count = connection.execute("SELECT COUNT(*) FROM concepts").fetchone()[0]

    assert place_id == repeated_place_id
    assert concept_observation_id != place_id
    assert [tuple(row) for row in observations] == [
        (place_id, lexeme_id, None, "place", 0.8, "scanner"),
        (concept_observation_id, lexeme_id, None, "concept", 0.6, "heuristic"),
    ]
    assert concept_count == 0


def test_retired_type_observation_does_not_satisfy_idempotent_replay(tmp_path):
    database = _db(tmp_path)
    lexeme_id = database.ensure_lexeme("Briah")
    first_id = database.record_type_observation(
        lexeme_id,
        "place",
        confidence=0.8,
        source="scanner",
    )
    with database.transaction() as connection:
        retired_version = database.create_knowledge_version(
            "retire type observation",
            connection,
        )
        connection.execute(
            """UPDATE concept_type_observations SET retired_version=?
               WHERE id=?""",
            (retired_version, first_id),
        )

    replay_id = database.record_type_observation(
        lexeme_id,
        "place",
        confidence=0.8,
        source="scanner",
    )

    assert replay_id != first_id
    with closing(database.connect()) as connection:
        observations = connection.execute(
            """SELECT id, retired_version FROM concept_type_observations
               WHERE lexeme_id=? ORDER BY id""",
            (lexeme_id,),
        ).fetchall()
    assert [tuple(row) for row in observations] == [
        (first_id, retired_version),
        (replay_id, None),
    ]


@pytest.mark.parametrize(
    ("kind", "confidence", "source"),
    [
        ("", 0.5, "scanner"),
        ("place", -0.01, "scanner"),
        ("place", 1.01, "scanner"),
        ("place", 0.5, ""),
    ],
)
def test_type_observation_validates_kind_confidence_and_source(
    tmp_path, kind, confidence, source
):
    database = _db(tmp_path)
    ensure_lexeme = _require_method(database, "ensure_lexeme")
    record = _require_method(database, "record_type_observation")
    lexeme_id = ensure_lexeme("Briah")

    with pytest.raises(ValueError):
        record(
            lexeme_id,
            kind,
            confidence=confidence,
            source=source,
        )


def test_occurrence_batch_prevalidation_rolls_back_all_rows_on_bad_offset(tmp_path):
    database = _db(tmp_path)
    lexeme_id = _require_method(database, "ensure_lexeme")("Briah")
    record = _require_method(database, "record_form_occurrences")
    valid = _occurrence(
        lexeme_id=lexeme_id,
        block_id="block-1",
        start_offset=0,
        end_offset=5,
        source_form="Briah",
        source_hash="hash-1",
    )
    bad_offset = _occurrence(
        lexeme_id=lexeme_id,
        block_id="block-1",
        start_offset=9,
        end_offset=14,
        source_form="BRIAH",
        source_hash="hash-1",
    )

    with pytest.raises(ValueError):
        record([valid, bad_offset])

    with closing(database.connect()) as connection:
        count = connection.execute("SELECT COUNT(*) FROM form_occurrences").fetchone()[0]
    assert count == 0


def test_occurrence_rejects_missing_inactive_or_source_hash_mismatched_block(tmp_path):
    database = _db(tmp_path)
    lexeme_id = _require_method(database, "ensure_lexeme")("Briah")
    record = _require_method(database, "record_form_occurrences")

    def row(block_id="block-1", source_hash="hash-1"):
        return _occurrence(
            lexeme_id=lexeme_id,
            block_id=block_id,
            start_offset=0,
            end_offset=5,
            source_form="Briah",
            source_hash=source_hash,
        )

    with pytest.raises((KeyError, ValueError)):
        record([row(block_id="missing")])
    with pytest.raises(ValueError):
        record([row(source_hash="stale-hash")])

    database.ensure_source_edition(
        "raw-2", "normalized-2", "test", "replacement.txt"
    )
    with pytest.raises(ValueError):
        record([row()])

    with closing(database.connect()) as connection:
        count = connection.execute("SELECT COUNT(*) FROM form_occurrences").fetchone()[0]
    assert count == 0


def test_identical_occurrence_replay_is_idempotent(tmp_path):
    database = _db(tmp_path)
    lexeme_id = _require_method(database, "ensure_lexeme")("Briah")
    record = _require_method(database, "record_form_occurrences")
    occurrence = _occurrence(
        lexeme_id=lexeme_id,
        block_id="block-1",
        start_offset=0,
        end_offset=5,
        source_form="Briah",
        source_hash="hash-1",
    )

    assert record([occurrence]) == 1
    assert record([occurrence]) == 0

    with closing(database.connect()) as connection:
        rows = connection.execute(
            """SELECT lexeme_id, block_id, start_offset, end_offset,
                      source_form, source_hash
               FROM form_occurrences"""
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (lexeme_id, "block-1", 0, 5, "Briah", "hash-1")
    ]


def test_occurrence_batch_failure_is_atomic_inside_caught_outer_transaction(tmp_path):
    database = _db(tmp_path)
    lexeme_id = database.ensure_lexeme("Briah")
    valid = _occurrence(
        lexeme_id=lexeme_id,
        block_id="block-1",
        start_offset=0,
        end_offset=5,
        source_form="Briah",
        source_hash="hash-1",
    )
    missing_lexeme = _occurrence(
        lexeme_id="lexeme_missing",
        block_id="block-1",
        start_offset=10,
        end_offset=15,
        source_form="BRIAH",
        source_hash="hash-1",
    )

    with database.transaction() as connection:
        preserved_before = database.ensure_lexeme(
            "Preserved Before",
            connection=connection,
        )
        with pytest.raises(KeyError):
            database.record_form_occurrences(
                [valid, missing_lexeme],
                connection=connection,
            )
        preserved_after = database.ensure_lexeme(
            "Preserved After",
            connection=connection,
        )

    with closing(database.connect()) as connection:
        count = connection.execute("SELECT COUNT(*) FROM form_occurrences").fetchone()[0]
        preserved_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM lexemes WHERE id IN (?, ?)",
                (preserved_before, preserved_after),
            )
        }
    assert count == 0
    assert preserved_ids == {preserved_before, preserved_after}


@pytest.mark.parametrize("invalid_lexeme_id", [None, ""])
def test_occurrence_batch_rejects_empty_lexeme_without_partial_writes(
    tmp_path, invalid_lexeme_id
):
    database = _db(tmp_path)
    lexeme_id = database.ensure_lexeme("Briah")
    valid = _occurrence(
        lexeme_id=lexeme_id,
        block_id="block-1",
        start_offset=0,
        end_offset=5,
        source_form="Briah",
        source_hash="hash-1",
    )
    invalid = _occurrence(
        lexeme_id=invalid_lexeme_id,
        block_id="block-1",
        start_offset=10,
        end_offset=15,
        source_form="BRIAH",
        source_hash="hash-1",
    )

    with database.transaction() as connection:
        with pytest.raises(ValueError):
            database.record_form_occurrences(
                [valid, invalid],
                connection=connection,
            )

    with closing(database.connect()) as connection:
        count = connection.execute("SELECT COUNT(*) FROM form_occurrences").fetchone()[0]
    assert count == 0


@pytest.mark.parametrize(
    "api_name",
    ["ensure_lexeme", "record_type_observation", "record_form_occurrences"],
)
def test_persistence_api_rejects_external_connection_without_active_transaction(
    tmp_path, api_name
):
    database = _db(tmp_path)
    lexeme_id = database.ensure_lexeme("Briah")

    def counts():
        with closing(database.connect()) as inspection:
            return tuple(
                inspection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "lexemes",
                    "source_forms",
                    "concept_type_observations",
                    "form_occurrences",
                )
            )

    before = counts()
    connection = database.connect()
    assert not connection.in_transaction
    try:
        with pytest.raises(ValueError, match=r"(?i)active transaction|transaction"):
            if api_name == "ensure_lexeme":
                database.ensure_lexeme("Raw Connection", connection=connection)
            elif api_name == "record_type_observation":
                database.record_type_observation(
                    lexeme_id,
                    "place",
                    confidence=0.8,
                    source="scanner",
                    connection=connection,
                )
            else:
                database.record_form_occurrences(
                    [
                        _occurrence(
                            lexeme_id=lexeme_id,
                            block_id="block-1",
                            start_offset=0,
                            end_offset=5,
                            source_form="Briah",
                            source_hash="hash-1",
                        )
                    ],
                    connection=connection,
                )
        assert not connection.in_transaction
        connection.rollback()
    finally:
        connection.close()

    assert counts() == before


def test_persistence_apis_join_the_callers_transaction(tmp_path):
    database = _db(tmp_path)
    ensure_lexeme = _require_method(database, "ensure_lexeme")
    record_type = _require_method(database, "record_type_observation")
    record_occurrences = _require_method(database, "record_form_occurrences")

    with pytest.raises(RuntimeError):
        with database.transaction() as connection:
            lexeme_id = ensure_lexeme("Briah", connection=connection)
            record_type(
                lexeme_id,
                "place",
                confidence=0.8,
                source="scanner",
                connection=connection,
            )
            record_occurrences(
                [
                    _occurrence(
                        lexeme_id=lexeme_id,
                        block_id="block-1",
                        start_offset=0,
                        end_offset=5,
                        source_form="Briah",
                        source_hash="hash-1",
                    )
                ],
                connection=connection,
            )
            raise RuntimeError("force caller rollback")

    with closing(database.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM lexemes").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM source_forms").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM concept_type_observations"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM form_occurrences").fetchone()[0] == 0


def _insert_coreference_mention(
    database,
    lexeme_id,
    *,
    paragraph_id,
    evidence_quote,
    kind=None,
    source_form="Briah",
):
    with database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO evidence(
                   block_id, paragraph_id, kind, source_form, evidence_quote,
                   payload_json, confidence, extractor, created_at)
               VALUES('block-1', ?, 'scan', ?, ?, '{}', 0.9,
                      'test', '2000-01-01T00:00:00+00:00')""",
            (paragraph_id, source_form, evidence_quote),
        )
        evidence_id = int(cursor.lastrowid)
        cursor = connection.execute(
            """INSERT INTO mentions(
                   block_id, paragraph_id, source_form, normalized_form,
                   discourse_function, lexeme_id, evidence_id)
               VALUES('block-1', ?, ?, ?, 'referential', ?, ?)""",
            (
                paragraph_id,
                source_form,
                normalize_english_form(source_form),
                lexeme_id,
                evidence_id,
            ),
        )
        mention_id = int(cursor.lastrowid)
        if kind is not None:
            database.record_type_observation(
                lexeme_id,
                kind,
                confidence=0.9,
                source="test",
                mention_id=mention_id,
                evidence_id=evidence_id,
                connection=connection,
            )
    return mention_id, evidence_id


def _coreference_case(tmp_path, *, count=2, long_context=False):
    database = _db(tmp_path, source_text="Briah.\n\nBriah again.")
    lexeme_id = database.ensure_lexeme("Briah")
    mention_ids = []
    evidence_ids = []
    for index in range(count):
        quote = (
            (f"context-{index}-" + ("x" * 2_000))
            if long_context
            else f"Briah context {index}"
        )
        mention_id, evidence_id = _insert_coreference_mention(
            database,
            lexeme_id,
            paragraph_id=f"P{index + 1:03d}",
            evidence_quote=quote,
            kind="person" if index % 2 == 0 else "place",
        )
        mention_ids.append(mention_id)
        evidence_ids.append(evidence_id)
    cases = CoreferenceCoordinator(database).freeze_cases()
    assert len(cases) == 1
    return database, lexeme_id, tuple(mention_ids), tuple(evidence_ids), cases[0]


@pytest.mark.parametrize(
    "relation", ["same", "different", "uncertain", "non_entity"]
)
def test_coreference_protocol_accepts_only_the_four_relations(tmp_path, relation):
    database, _, _, _, case = _coreference_case(tmp_path)
    coordinator = CoreferenceCoordinator(database)
    aliases = [mention.request_id for mention in case.mentions]

    response = coordinator.parse_response(
        {
            "votes": [
                {
                    "case_id": case.case_id,
                    "relation": relation,
                    "mention_ids": aliases,
                    "confidence": 0.8,
                    "rationale": "bounded rationale",
                }
            ]
        },
        [case],
    )

    assert response.votes[0].relation == relation


@pytest.mark.parametrize(
    "mutate",
    [
        lambda vote: vote.update(case_id="R999"),
        lambda vote: vote.pop("mention_ids"),
        lambda vote: vote.update(mention_ids=[]),
        lambda vote: vote.update(mention_ids=["M999"]),
        lambda vote: vote.update(mention_ids=["M001", "M001"]),
        lambda vote: vote.update(unexpected=True),
        lambda vote: vote.update(relation="maybe"),
    ],
)
def test_coreference_protocol_rejects_unknown_omitted_or_extra_values(
    tmp_path, mutate
):
    database, _, _, _, case = _coreference_case(tmp_path)
    coordinator = CoreferenceCoordinator(database)
    vote = {
        "case_id": case.case_id,
        "relation": "same",
        "mention_ids": [mention.request_id for mention in case.mentions],
        "confidence": 0.8,
        "rationale": "bounded rationale",
    }
    mutate(vote)

    with pytest.raises(CoreferenceProtocolError):
        coordinator.parse_response({"votes": [vote]}, [case])


def test_coreference_protocol_revalidates_an_existing_response_instance(tmp_path):
    database, _, _, _, case = _coreference_case(tmp_path)
    coordinator = CoreferenceCoordinator(database)
    response = v4_models.CoreferenceResponse.model_validate(
        {
            "votes": [
                {
                    "case_id": case.case_id,
                    "relation": "same",
                    "mention_ids": [
                        mention.request_id for mention in case.mentions
                    ],
                    "confidence": 0.8,
                    "rationale": "bounded rationale",
                }
            ]
        }
    )
    response.votes[0].relation = "maybe"

    with pytest.raises(CoreferenceProtocolError):
        coordinator.parse_response(response, [case])


def test_coreference_protocol_revalidates_nested_vote_instances(tmp_path):
    database, _, _, _, case = _coreference_case(tmp_path)
    coordinator = CoreferenceCoordinator(database)
    vote = v4_models.CoreferenceVote.model_validate(
        {
            "case_id": case.case_id,
            "relation": "same",
            "mention_ids": [mention.request_id for mention in case.mentions],
            "confidence": 0.8,
            "rationale": "bounded rationale",
        }
    )
    object.__setattr__(vote, "relation", "maybe")

    with pytest.raises(CoreferenceProtocolError):
        coordinator.parse_response({"votes": [vote]}, [case])


def test_frozen_coreference_payload_is_identical_for_both_models_and_runtime_free(
    tmp_path,
):
    database, _, _, _, case = _coreference_case(tmp_path)
    coordinator = CoreferenceCoordinator(database)

    left = coordinator.payload_bytes(case)
    right = coordinator.payload_bytes(case)
    payload = json.loads(left)

    assert left == right
    assert coordinator.model_payloads(case, ["model-b", "model-a"]) == (left, left)
    assert "run_id" not in left.decode("utf-8")
    assert "created_at" not in left.decode("utf-8")
    assert "timestamp" not in left.decode("utf-8")
    assert payload["protocol_version"] == coreference_module.COREFERENCE_PROTOCOL_VERSION


def test_coreference_high_frequency_selection_is_bounded_and_stable(tmp_path):
    database, _, real_ids, _, first_case = _coreference_case(tmp_path, count=12)
    coordinator = CoreferenceCoordinator(database)

    second_case = coordinator.freeze_cases()[0]
    selected_ids = [mention.mention_id for mention in first_case.mentions]

    assert first_case == second_case
    assert len(selected_ids) == 8
    assert real_ids[0] in selected_ids
    assert real_ids[-1] in selected_ids
    assert len(set(selected_ids)) == len(selected_ids)
    payload = json.loads(coordinator.payload_bytes(first_case))["case"]
    assert {
        item["mention_id"]
        for item in payload["type_observations"]
        if item["mention_id"] is not None
    } <= set(selected_ids)


def test_coreference_max_cases_only_materializes_selected_lexeme_details(
    tmp_path, monkeypatch
):
    database = _db(tmp_path)
    alpha_id = database.ensure_lexeme("Alpha")
    zulu_id = database.ensure_lexeme("Zulu")
    for lexeme_id, source_form in ((alpha_id, "Alpha"), (zulu_id, "Zulu")):
        for index in range(2):
            _insert_coreference_mention(
                database,
                lexeme_id,
                paragraph_id=f"P{index + 1:03d}",
                evidence_quote=f"{source_form} context {index}",
                kind="person",
                source_form=source_form,
            )

    statements = []
    real_connect = database.connect

    def traced_connect():
        connection = real_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "connect", traced_connect)
    cases = CoreferenceCoordinator(database).freeze_cases(max_cases=1)

    assert [case.lexeme_id for case in cases] == [alpha_id]
    detail_queries = [
        statement
        for statement in statements
        if "SELECT m.id AS mention_id" in statement
    ]
    assert len(detail_queries) == 1
    assert alpha_id in detail_queries[0]
    assert zulu_id not in detail_queries[0]
    assert all(
        zulu_id not in statement
        for statement in statements
        if "SELECT o.id AS observation_id" in statement
        or "SELECT cl.lexeme_id" in statement
        or "SELECT DISTINCT m.lexeme_id" in statement
    )


def test_coreference_zero_max_cases_skips_snapshot_detail_queries(
    tmp_path, monkeypatch
):
    database = _db(tmp_path)
    connect_calls = 0
    real_connect = database.connect

    def counted_connect():
        nonlocal connect_calls
        connect_calls += 1
        return real_connect()

    monkeypatch.setattr(database, "connect", counted_connect)

    assert CoreferenceCoordinator(database).freeze_cases(max_cases=0) == ()
    assert connect_calls == 0


def test_frozen_payload_keeps_real_ids_hashes_types_anchors_and_bounded_context(
    tmp_path,
):
    database, lexeme_id, mention_ids, evidence_ids, case = _coreference_case(
        tmp_path, long_context=True
    )
    coordinator = CoreferenceCoordinator(database)
    payload = json.loads(coordinator.payload_bytes(case))["case"]

    assert payload["knowledge_version"] == database.current_knowledge_version()
    assert payload["lexeme"]["id"] == lexeme_id
    assert payload["mention_set_id"] == stable_id(
        "mention-set", ":".join(sorted(str(value) for value in mention_ids))
    )
    assert [item["mention_id"] for item in payload["mentions"]] == list(mention_ids)
    assert [item["evidence_id"] for item in payload["mentions"]] == list(evidence_ids)
    assert all(item["block_id"] == "block-1" for item in payload["mentions"])
    assert all(item["source_hash"] == "hash-1" for item in payload["mentions"])
    assert all(item["request_id"].startswith("M") for item in payload["mentions"])
    all_kinds = {
        item["kind"]
        for item in payload["type_observations"]
    } | {
        item["kind"]
        for mention in payload["mentions"]
        for item in mention["type_observations"]
    }
    assert all_kinds == {
        "person",
        "place",
    }
    assert sum(len(item["context"]) for item in payload["mentions"]) <= 3_200
    assert all(len(item["context"]) <= 400 for item in payload["mentions"])


def test_coreference_payload_has_a_fixed_budget_for_large_metadata(tmp_path):
    database, lexeme_id, mention_ids, evidence_ids, _ = _coreference_case(
        tmp_path, count=8
    )
    with database.transaction() as connection:
        version = connection.execute(
            "SELECT MAX(id) FROM knowledge_versions"
        ).fetchone()[0]
        for index in range(20):
            suffix = f"{index:03d}"
            for mention_id, evidence_id in zip(mention_ids, evidence_ids):
                database.record_type_observation(
                    lexeme_id,
                    f"kind-{suffix}-" + ("🧪" * 5_000),
                    confidence=0.6,
                    source=f"mention-source-{suffix}-" + ("🧪" * 5_000),
                    mention_id=mention_id,
                    evidence_id=evidence_id,
                    connection=connection,
                )
            database.record_type_observation(
                lexeme_id,
                f"lexeme-kind-{suffix}-" + ("🧪" * 5_000),
                confidence=0.5,
                source=f"lexeme-source-{suffix}-" + ("🧪" * 5_000),
                connection=connection,
            )
            concept_id = f"concept-budget-{suffix}"
            connection.execute(
                """INSERT INTO concepts(
                       id, kind, canonical_source, primary_lexeme_id,
                       created_version, created_at)
                   VALUES(?, ?, ?, ?, ?, '2000-01-01T00:00:00+00:00')""",
                (
                    concept_id,
                    f"anchor-kind-{suffix}-" + ("🧪" * 5_000),
                    f"anchor-source-{suffix}-" + ("🧪" * 5_000),
                    lexeme_id,
                    version,
                ),
            )
            connection.execute(
                """INSERT INTO concept_lexemes(
                       concept_id, lexeme_id, role, confidence, status,
                       created_version, created_at)
                   VALUES(?, ?, 'primary', 1.0, 'provisional', ?,
                          '2000-01-01T00:00:00+00:00')""",
                (concept_id, lexeme_id, version),
            )

    coordinator = CoreferenceCoordinator(database)
    first_case = coordinator.freeze_cases()[0]
    second_case = coordinator.freeze_cases()[0]
    frozen = coordinator.payload_bytes(first_case)
    payload = json.loads(frozen)["case"]

    assert first_case == second_case
    assert len(frozen) <= coreference_module.MAX_CASE_PAYLOAD_BYTES
    assert len(payload["type_observations"]) <= (
        coreference_module.MAX_LEXEME_TYPE_OBSERVATIONS
    )
    assert all(
        len(mention["type_observations"])
        <= coreference_module.MAX_MENTION_TYPE_OBSERVATIONS
        for mention in payload["mentions"]
    )
    assert len(payload["concept_anchors"]) <= (
        coreference_module.MAX_CONCEPT_ANCHORS
    )
    assert all(
        len(item["source"])
        <= coreference_module.MAX_OBSERVATION_SOURCE_CHARS
        for item in payload["type_observations"]
    )
    assert all(
        len(item["source"])
        <= coreference_module.MAX_OBSERVATION_SOURCE_CHARS
        for mention in payload["mentions"]
        for item in mention["type_observations"]
    )
    assert all(
        len(item["canonical_source"])
        <= coreference_module.MAX_FREE_TEXT_CHARS
        for item in payload["concept_anchors"]
    )
    assert [item["mention_id"] for item in payload["mentions"]] == list(
        mention_ids
    )
    nested_kinds = {
        item["kind"]
        for mention in payload["mentions"]
        for item in mention["type_observations"]
    }
    assert {"person", "place"} <= nested_kinds
    assert any(kind.startswith("kind-019-") for kind in nested_kinds)


def test_coreference_payload_budget_accepts_worst_case_multibyte_text(tmp_path):
    _, _, _, _, case = _coreference_case(tmp_path, count=8)
    marker = "🧪" * 5_000
    observation_id = 10_000
    mentions = []
    for mention in case.mentions:
        observations = []
        for _ in range(coreference_module.MAX_MENTION_TYPE_OBSERVATIONS):
            observation_id += 1
            observations.append(
                v4_models.CoreferenceTypeObservation(
                    observation_id=observation_id,
                    kind=marker,
                    confidence=0.5,
                    source=marker,
                    mention_id=mention.mention_id,
                    evidence_id=mention.evidence_id,
                )
            )
        mentions.append(
            replace(
                mention,
                block_kind=marker,
                source_form=marker,
                discourse_function=marker,
                context=marker,
                context_source=marker,
                type_observations=tuple(observations),
            )
        )
    lexeme_observations = tuple(
        v4_models.CoreferenceTypeObservation(
            observation_id=20_000 + index,
            kind=marker,
            confidence=0.5,
            source=marker,
        )
        for index in range(coreference_module.MAX_LEXEME_TYPE_OBSERVATIONS)
    )
    anchors = tuple(
        v4_models.CoreferenceConceptAnchor(
            concept_id=f"concept-multibyte-{index}",
            kind=marker,
            canonical_source=marker,
            status=marker,
            role="primary",
        )
        for index in range(coreference_module.MAX_CONCEPT_ANCHORS)
    )
    worst_case = replace(
        case,
        language=marker,
        normalized_form=marker,
        canonical_form=marker,
        mentions=tuple(mentions),
        type_observations=lexeme_observations,
        concept_anchors=anchors,
    )

    frozen = CoreferenceCoordinator.payload_bytes(worst_case)

    assert len(frozen) <= coreference_module.MAX_CASE_PAYLOAD_BYTES


def test_coreference_payload_carries_each_existing_concept_anchor_once(tmp_path):
    database, lexeme_id, mention_ids, _, _ = _coreference_case(tmp_path)
    with database.transaction() as connection:
        version = connection.execute(
            "SELECT MAX(id) FROM knowledge_versions"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO concepts(
                   id, kind, canonical_source, primary_lexeme_id,
                   anchor_mention_id, created_version, created_at)
               VALUES('concept-briah', 'person', 'Briah', ?, ?, ?,
                      '2000-01-01T00:00:00+00:00')""",
            (lexeme_id, mention_ids[0], version),
        )
        connection.execute(
            """INSERT INTO concept_lexemes(
                   concept_id, lexeme_id, role, confidence, status,
                   created_version, created_at)
               VALUES('concept-briah', ?, 'primary', 1.0, 'provisional', ?,
                      '2000-01-01T00:00:00+00:00')""",
            (lexeme_id, version),
        )
        connection.execute(
            "UPDATE mentions SET concept_id='concept-briah' WHERE id=?",
            (mention_ids[0],),
        )

    case = CoreferenceCoordinator(database).freeze_cases()[0]
    payload = json.loads(CoreferenceCoordinator.payload_bytes(case))["case"]

    assert [item["concept_id"] for item in payload["concept_anchors"]] == [
        "concept-briah"
    ]
    assert payload["concept_anchors"][0]["role"] == "primary"
    assert payload["mentions"][0]["concept_anchor_ids"] == ["concept-briah"]


def test_coreference_anchor_budget_prioritizes_selected_mention_binding(tmp_path):
    database, lexeme_id, mention_ids, _, _ = _coreference_case(tmp_path)
    with database.transaction() as connection:
        version = connection.execute(
            "SELECT MAX(id) FROM knowledge_versions"
        ).fetchone()[0]
        for index in range(coreference_module.MAX_CONCEPT_ANCHORS):
            concept_id = f"concept-a-{index:03d}"
            connection.execute(
                """INSERT INTO concepts(
                       id, kind, canonical_source, primary_lexeme_id,
                       created_version, created_at)
                   VALUES(?, 'person', ?, ?, ?,
                          '2000-01-01T00:00:00+00:00')""",
                (concept_id, concept_id, lexeme_id, version),
            )
            connection.execute(
                """INSERT INTO concept_lexemes(
                       concept_id, lexeme_id, role, confidence, status,
                       created_version, created_at)
                   VALUES(?, ?, 'primary', 1.0, 'provisional', ?,
                          '2000-01-01T00:00:00+00:00')""",
                (concept_id, lexeme_id, version),
            )
        connection.execute(
            """INSERT INTO concepts(
                   id, kind, canonical_source, primary_lexeme_id,
                   created_version, created_at)
               VALUES('concept-z-direct', 'person', 'direct', ?, ?,
                      '2000-01-01T00:00:00+00:00')""",
            (lexeme_id, version),
        )
        connection.execute(
            "UPDATE mentions SET concept_id='concept-z-direct' WHERE id=?",
            (mention_ids[0],),
        )

    case = CoreferenceCoordinator(database).freeze_cases()[0]
    payload = json.loads(CoreferenceCoordinator.payload_bytes(case))["case"]

    assert "concept-z-direct" in {
        item["concept_id"] for item in payload["concept_anchors"]
    }
    assert payload["mentions"][0]["concept_anchor_ids"] == [
        "concept-z-direct"
    ]


def test_coreference_payload_does_not_emit_dangling_retired_anchor_ids(tmp_path):
    database, lexeme_id, mention_ids, _, _ = _coreference_case(tmp_path)
    with database.transaction() as connection:
        created_version = connection.execute(
            "SELECT MAX(id) FROM knowledge_versions"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO concepts(
                   id, kind, canonical_source, primary_lexeme_id,
                   anchor_mention_id, created_version, created_at)
               VALUES('retired-briah', 'person', 'Briah', ?, ?, ?,
                      '2000-01-01T00:00:00+00:00')""",
            (lexeme_id, mention_ids[0], created_version),
        )
        connection.execute(
            "UPDATE mentions SET concept_id='retired-briah' WHERE id=?",
            (mention_ids[0],),
        )
        retired_version = database.create_knowledge_version(
            "retire test concept", connection
        )
        connection.execute(
            "UPDATE concepts SET retired_version=? WHERE id='retired-briah'",
            (retired_version,),
        )

    case = CoreferenceCoordinator(database).freeze_cases()[0]
    payload = json.loads(CoreferenceCoordinator.payload_bytes(case))["case"]

    assert payload["concept_anchors"] == []
    assert payload["mentions"][0]["concept_anchor_ids"] == []


def test_coreference_type_observations_hide_retired_concept_ids(tmp_path):
    database, lexeme_id, mention_ids, evidence_ids, _ = _coreference_case(tmp_path)
    with database.transaction() as connection:
        created_version = connection.execute(
            "SELECT MAX(id) FROM knowledge_versions"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO concepts(
                   id, kind, canonical_source, primary_lexeme_id,
                   created_version, created_at)
               VALUES('retired-observation-concept', 'organization', 'Briah',
                      ?, ?, '2000-01-01T00:00:00+00:00')""",
            (lexeme_id, created_version),
        )
        database.record_type_observation(
            lexeme_id,
            "organization",
            confidence=0.7,
            source="retired-concept-test",
            mention_id=mention_ids[0],
            concept_id="retired-observation-concept",
            evidence_id=evidence_ids[0],
            connection=connection,
        )
        retired_version = database.create_knowledge_version(
            "retire observation concept", connection
        )
        connection.execute(
            """UPDATE concepts SET retired_version=?
               WHERE id='retired-observation-concept'""",
            (retired_version,),
        )

    case = CoreferenceCoordinator(database).freeze_cases()[0]
    payload = json.loads(CoreferenceCoordinator.payload_bytes(case))["case"]

    assert "retired-observation-concept" not in json.dumps(payload)
    assert all(
        item["concept_id"] is None for item in payload["type_observations"]
    )
    assert all(
        item["concept_id"] is None
        for mention in payload["mentions"]
        for item in mention["type_observations"]
    )


def test_coreference_cache_key_uses_payload_models_and_protocol_not_model_order(
    tmp_path, monkeypatch
):
    database, _, _, _, case = _coreference_case(tmp_path)
    coordinator = CoreferenceCoordinator(database)

    original = coordinator.cache_key(case, ["model-b", "model-a"])
    assert original == coordinator.cache_key(case, ["model-a", "model-b"])
    assert original != coordinator.cache_key(case, ["model-a", "model-c"])

    changed_case = replace(case, knowledge_version=case.knowledge_version + 1)
    assert original != coordinator.cache_key(changed_case, ["model-a", "model-b"])

    monkeypatch.setattr(coreference_module, "COREFERENCE_PROTOCOL_VERSION", "test-v2")
    assert original != coordinator.cache_key(case, ["model-a", "model-b"])


@pytest.mark.parametrize(
    "model_names",
    [
        ["", "model-b"],
        ["   ", "model-b"],
        ["model-a", "model-a"],
        [" model-a ", "model-a"],
    ],
)
def test_coreference_dual_model_names_must_be_nonempty_and_distinct(
    tmp_path, model_names
):
    database, _, _, _, case = _coreference_case(tmp_path)
    coordinator = CoreferenceCoordinator(database)

    with pytest.raises(ValueError):
        coordinator.cache_key(case, model_names)
    with pytest.raises(ValueError):
        coordinator.model_payloads(case, model_names)
