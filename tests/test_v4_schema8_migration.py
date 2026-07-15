import hashlib
import sqlite3
from contextlib import closing

import pytest

from src.core.v4.database import V4Database
from src.core.v4.models import ScanOutcome, ScanResponse
from src.core.v4.schema_v8 import (
    SCHEMA_VERSION,
    SchemaUpgradeRequired,
    create_schema8,
)


def seed_schema7_database(tmp_path):
    path = tmp_path / "artifacts" / "parallel_v4" / "book.db"
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES('schema_version', '7');
            """
        )
    return path


def read_schema_version(path):
    with closing(sqlite3.connect(path)) as connection:
        return int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
        )


def filesystem_snapshot(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def seed_incomplete_schema8(tmp_path, corruption):
    path = tmp_path / "artifacts" / "parallel_v4" / "book.db"
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as connection:
        if corruption == "sentinel":
            connection.executescript(
                """
                CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO schema_meta VALUES('schema_version', '8');
                CREATE TABLE sentinel(value TEXT);
                """
            )
        else:
            create_schema8(connection)
            if corruption == "table":
                connection.execute("DROP TABLE lexemes")
            elif corruption == "column":
                connection.executescript(
                    """
                    DROP TABLE candidate_resolutions;
                    CREATE TABLE candidate_resolutions(id TEXT PRIMARY KEY);
                    """
                )
            elif corruption == "index":
                connection.execute("DROP INDEX uq_active_lexeme")
            elif corruption == "rendering_check":
                connection.executescript(
                    """
                    ALTER TABLE rendering_rules RENAME TO old_rendering_rules;
                    CREATE TABLE rendering_rules (
                        id TEXT PRIMARY KEY,
                        lexeme_id TEXT,
                        concept_id TEXT,
                        condition_json TEXT NOT NULL,
                        target TEXT NOT NULL,
                        created_version INTEGER NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    DROP TABLE old_rendering_rules;
                    """
                )
            elif corruption == "translation_check":
                connection.executescript(
                    """
                    ALTER TABLE translation_versions RENAME TO old_translation_versions;
                    CREATE TABLE translation_versions (
                        id INTEGER PRIMARY KEY,
                        block_id TEXT NOT NULL,
                        pipeline TEXT NOT NULL,
                        status TEXT NOT NULL,
                        validation_status TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    DROP TABLE old_translation_versions;
                    """
                )
            elif corruption == "revalidation_check":
                connection.executescript(
                    """
                    DROP TABLE revalidation_tasks;
                    CREATE TABLE revalidation_tasks (
                        id TEXT PRIMARY KEY,
                        status TEXT NOT NULL
                    );
                    """
                )
            else:
                raise AssertionError(f"unknown corruption: {corruption}")
            connection.commit()
    return path


def test_schema7_requires_explicit_preview_and_confirm(tmp_path):
    path = seed_schema7_database(tmp_path)
    with pytest.raises(SchemaUpgradeRequired, match="migrate-v4 --preview"):
        V4Database(tmp_path)
    assert read_schema_version(path) == 7


@pytest.mark.parametrize(
    "corruption",
    (
        "sentinel",
        "table",
        "column",
        "index",
        "rendering_check",
        "translation_check",
        "revalidation_check",
    ),
)
def test_incomplete_schema8_is_rejected_without_writes(tmp_path, corruption):
    path = seed_incomplete_schema8(tmp_path, corruption)
    before = filesystem_snapshot(tmp_path)

    with pytest.raises(
        SchemaUpgradeRequired,
        match=r"(?:incomplete|corrupt).*migrate-v4 --preview",
    ):
        V4Database(tmp_path)

    assert filesystem_snapshot(tmp_path) == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before[
        str(path.relative_to(tmp_path))
    ]


def test_create_schema8_two_phase_state_can_finish_initialization(tmp_path):
    path = tmp_path / "artifacts" / "parallel_v4" / "book.db"
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as connection:
        create_schema8(connection)

    database = V4Database(tmp_path)

    with closing(database.connect()) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "8"
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='blocks'"
        ).fetchone() is not None


def test_empty_directory_creates_schema8_with_required_tables(tmp_path):
    database = V4Database(tmp_path)
    with closing(database.connect()) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        columns = {
            table: {
                row[1]
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for table in (
                "source_forms",
                "mentions",
                "rendering_rules",
                "candidate_resolutions",
                "translation_versions",
                "dependencies",
            )
        }
        column_info = {
            table: {
                row[1]: row
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for table in ("source_forms", "mentions")
        }

    assert version == SCHEMA_VERSION == 8
    assert {
        "lexemes",
        "concept_lexemes",
        "concept_type_observations",
        "coreference_decisions",
        "concept_redirects",
        "form_occurrences",
        "knowledge_changes",
        "revalidation_tasks",
    } <= tables
    assert "lexeme_id" in columns["source_forms"]
    assert "lexeme_id" in columns["mentions"]
    assert column_info["source_forms"]["lexeme_id"][3] == 1
    assert "concept_id" not in column_info["source_forms"]
    assert column_info["mentions"]["lexeme_id"][3] == 1
    assert {"lexeme_id", "concept_id"} <= columns["rendering_rules"]
    assert "lexeme_id" in columns["candidate_resolutions"]
    assert {
        "validation_status",
        "validated_knowledge_version",
        "validation_fingerprint",
    } <= columns["translation_versions"]
    assert {
        "dependency_fingerprint",
        "matched_form",
        "occurrence_count",
        "rendered_target",
        "applied_rule_ids_json",
        "source_spans_json",
    } <= columns["dependencies"]


def test_lexeme_owned_rows_reject_missing_lexeme(tmp_path):
    database = V4Database(tmp_path)
    with database.transaction() as connection:
        version = connection.execute(
            "SELECT MAX(id) FROM knowledge_versions"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO concepts(
                   id, kind, canonical_source, created_version, created_at)
               VALUES('concept-1', 'person', 'Drotte', ?, 'now')""",
            (version,),
        )
        connection.execute(
            """INSERT INTO source_editions(
                   raw_sha256, normalized_sha256, parser_version, source_path,
                   created_at)
               VALUES('raw', 'normalized', 'test', 'source.txt', 'now')"""
        )
        edition_id = connection.execute(
            "SELECT id FROM source_editions"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO blocks(
                   id, legacy_id, source_edition_id, chapter_id, chapter_title,
                   chapter_index, block_index, global_index, source_text,
                   source_hash, updated_at)
               VALUES('block-1', 'block-1', ?, 'chapter-1', 'One', 0, 0, 0,
                      'Drotte', 'hash', 'now')""",
            (edition_id,),
        )
        evidence_id = connection.execute(
            """INSERT INTO evidence(
                   block_id, paragraph_id, kind, evidence_quote, payload_json,
                   confidence, extractor, created_at)
               VALUES('block-1', 'P000', 'entity', 'Drotte', '{}', 1.0,
                      'test', 'now')"""
        ).lastrowid

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO source_forms(form, normalized_form)
                   VALUES('Drotte', 'drotte')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO mentions(
                       block_id, paragraph_id, source_form, normalized_form,
                       discourse_function, concept_id, evidence_id)
                   VALUES('block-1', 'P000', 'Drotte', 'drotte',
                          'referential', 'concept-1', ?)""",
                (evidence_id,),
            )


def test_legacy_concept_import_populates_lexeme_ownership(tmp_path):
    database = V4Database(tmp_path)
    concept_id = database.import_legacy_concept(
        "Drotte", "德罗特", "person", "a companion"
    )
    with closing(database.connect()) as connection:
        concept = connection.execute(
            "SELECT primary_lexeme_id FROM concepts WHERE id=?",
            (concept_id,),
        ).fetchone()
        lexeme = connection.execute(
            """SELECT language, normalized_form, canonical_form
               FROM lexemes WHERE id=?""",
            (concept["primary_lexeme_id"],),
        ).fetchone()
        association = connection.execute(
            """SELECT role FROM concept_lexemes
               WHERE concept_id=? AND lexeme_id=? AND retired_version IS NULL""",
            (concept_id, concept["primary_lexeme_id"]),
        ).fetchone()
        source_form = connection.execute(
            """SELECT lexeme_id, form, normalized_form FROM source_forms"""
        ).fetchone()

    assert tuple(lexeme) == ("en", "drotte", "Drotte")
    assert association["role"] == "primary"
    assert tuple(source_form) == (concept["primary_lexeme_id"], "Drotte", "drotte")


@pytest.mark.parametrize(
    "operations",
    (("import", "lock"), ("lock", "import")),
)
def test_ownership_helper_never_copies_concept_target_state(tmp_path, operations):
    database = V4Database(tmp_path)
    for operation in operations:
        if operation == "import":
            database.import_legacy_concept(
                "Drotte", "", "person", "a companion"
            )
        else:
            database.lock_concept_translation("Drotte", "德罗特", kind="person")

    with closing(database.connect()) as connection:
        lexeme = connection.execute(
            """SELECT default_target, working_target, verified_target,
                      status, locked
               FROM lexemes WHERE normalized_form='drotte'"""
        ).fetchone()

    assert tuple(lexeme) == ("", "", "", "provisional", 0)


def test_reconcile_reuses_canonical_mention_lexeme(tmp_path):
    database = V4Database(tmp_path)
    edition = database.ensure_source_edition(
        "raw", "normalized", "test", "source.txt"
    )
    database.upsert_blocks(
        edition,
        [
            {
                "id": "block-1",
                "chapter_id": "chapter-1",
                "chapter_title": "One",
                "chapter_index": 0,
                "block_index": 0,
                "global_index": 0,
                "block_type": "prose",
                "source_text": "Wolves gathered.",
                "source_hash": "hash",
            }
        ],
    )
    block = database.list_blocks()[0]
    response = ScanResponse.model_validate(
        {
            "mentions": [
                {
                    "paragraph_id": "P000",
                    "source_form": "Wolves",
                    "canonical_form": "Wolf",
                    "category": "species",
                    "evidence_quote": "Wolves",
                }
            ]
        }
    )
    database.start_run("scan-run", "scan", {})
    database.commit_scan_batch(
        "scan-run", [ScanOutcome(block=block, response=response)], "fake"
    )

    database.reconcile_exact_forms()

    with closing(database.connect()) as connection:
        mention = connection.execute(
            "SELECT lexeme_id, concept_id FROM mentions"
        ).fetchone()
        concept = connection.execute(
            "SELECT primary_lexeme_id FROM concepts WHERE id=?",
            (mention["concept_id"],),
        ).fetchone()
        lexemes = connection.execute(
            "SELECT id, normalized_form FROM lexemes ORDER BY normalized_form"
        ).fetchall()

    assert [row["normalized_form"] for row in lexemes] == ["wolf"]
    assert concept["primary_lexeme_id"] == mention["lexeme_id"] == lexemes[0]["id"]


def test_rendering_rule_requires_exactly_one_subject(tmp_path):
    database = V4Database(tmp_path)
    with database.transaction() as connection:
        version = connection.execute(
            "SELECT MAX(id) FROM knowledge_versions"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO lexemes(
                   id, language, normalized_form, canonical_form,
                   created_version, created_at)
               VALUES('lexeme-1', 'en', 'drotte', 'Drotte', ?, 'now')""",
            (version,),
        )
        connection.execute(
            """INSERT INTO concepts(
                   id, kind, canonical_source, created_version, created_at)
               VALUES('concept-1', 'person', 'Drotte', ?, 'now')""",
            (version,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """INSERT INTO rendering_rules(
                       id, lexeme_id, concept_id, condition_json, target,
                       created_version, created_at)
                   VALUES('rule-neither', NULL, NULL, '{}', '德罗特', ?, 'now')""",
                (version,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """INSERT INTO rendering_rules(
                       id, lexeme_id, concept_id, condition_json, target,
                       created_version, created_at)
                   VALUES('rule-both', 'lexeme-1', 'concept-1', '{}',
                          '德罗特', ?, 'now')""",
                (version,),
            )


def test_schema8_status_enums_are_checked(tmp_path):
    database = V4Database(tmp_path)
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO source_editions(
                   raw_sha256, normalized_sha256, parser_version, source_path,
                   created_at)
               VALUES('raw', 'normalized', 'test', 'source.txt', 'now')"""
        )
        edition_id = connection.execute(
            "SELECT id FROM source_editions"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO blocks(
                   id, legacy_id, source_edition_id, chapter_id, chapter_title,
                   chapter_index, block_index, global_index, source_text,
                   source_hash, updated_at)
               VALUES('block-1', 'block-1', ?, 'chapter-1', 'One', 0, 0, 0,
                      'Drotte', 'hash', 'now')""",
            (edition_id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """INSERT INTO translation_versions(
                       block_id, pipeline, status, validation_status, created_at)
                   VALUES('block-1', 'parallel_v4', 'completed', 'invalid', 'now')"""
            )
        translation_id = connection.execute(
            """INSERT INTO translation_versions(
                   block_id, pipeline, status, validation_status, created_at)
               VALUES('block-1', 'parallel_v4', 'completed', 'clean', 'now')"""
        ).lastrowid
        version = connection.execute(
            "SELECT MAX(id) FROM knowledge_versions"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """INSERT INTO revalidation_tasks(
                       id, translation_id, block_id, from_knowledge_version,
                       to_knowledge_version, change_set_hash, impact_level,
                       status, created_at)
                   VALUES('task-1', ?, 'block-1', ?, ?, 'changes', 1,
                          'invalid', 'now')""",
                (translation_id, version, version),
            )
