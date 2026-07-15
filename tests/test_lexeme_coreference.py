from contextlib import closing
from dataclasses import FrozenInstanceError

import pytest

from src.core.v4 import models as v4_models
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
