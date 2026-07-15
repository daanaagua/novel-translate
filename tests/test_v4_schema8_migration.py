import sqlite3
from contextlib import closing

import pytest

from src.core.v4.database import V4Database
from src.core.v4.schema_v8 import (
    SCHEMA_VERSION,
    SchemaUpgradeRequired,
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


def test_schema7_requires_explicit_preview_and_confirm(tmp_path):
    path = seed_schema7_database(tmp_path)
    with pytest.raises(SchemaUpgradeRequired, match="migrate-v4 --preview"):
        V4Database(tmp_path)
    assert read_schema_version(path) == 7


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
