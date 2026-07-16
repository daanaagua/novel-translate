from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from src.core.v4.database import V4Database
from src.core.v4.schema_v8 import create_schema8


def _schema8_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE knowledge_versions (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE blocks (
                id TEXT PRIMARY KEY,
                global_index INTEGER NOT NULL,
                source_hash TEXT NOT NULL
            );
            CREATE TABLE runs (id TEXT PRIMARY KEY);
            CREATE TABLE audit_calls (id INTEGER PRIMARY KEY);
            CREATE TABLE evidence (id INTEGER PRIMARY KEY);
            CREATE TABLE candidate_clusters (id TEXT PRIMARY KEY);
            CREATE TABLE candidate_adjudications (id TEXT PRIMARY KEY);
            INSERT INTO knowledge_versions(id, reason, created_at)
            VALUES(1, 'schema 8 fixture', '2026-07-16T00:00:00+00:00');
            INSERT INTO blocks(id, global_index, source_hash)
            VALUES('block-1', 0, 'source-hash');
            """
        )
        create_schema8(connection)


def test_empty_database_initializes_schema9(tmp_path):
    database = V4Database(tmp_path)

    with database.connect() as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        translation_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(translation_versions)"
            )
        }
        memory_versions = connection.execute(
            "SELECT COUNT(*) FROM memory_versions"
        ).fetchone()[0]

    assert version == "9"
    assert {
        "memory_versions",
        "narrative_memories",
        "narrative_memory_evidence",
        "narrative_memory_subjects",
        "narrative_memory_links",
        "narrative_snapshots",
        "premap_results",
        "source_structure",
        "style_snapshots",
    } <= tables
    assert {
        "memory_version",
        "snapshot_id",
        "context_hash",
        "style_snapshot_id",
        "discourse_state_hash",
    } <= translation_columns
    assert memory_versions == 1


def test_schema8_requires_explicit_schema9_migration(tmp_path):
    from src.core.v4.schema_v9 import SchemaUpgradeRequired

    path = tmp_path / "artifacts" / "parallel_v4" / "book.db"
    _schema8_database(path)

    with pytest.raises(SchemaUpgradeRequired, match="schema 8"):
        V4Database(tmp_path)


def test_schema8_preview_and_confirm_migrates_without_losing_rows(tmp_path):
    from src.core.v4.schema_v9 import (
        inspect_schema,
        migrate_schema9,
        preview_schema9,
    )

    path = tmp_path / "book.db"
    _schema8_database(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """INSERT INTO translation_versions(
                   block_id, pipeline, knowledge_version, status,
                   final_translation, active, created_at)
               VALUES('block-1', 'parallel_v4', 1, 'completed',
                      '旧译文', 1, '2026-07-16T00:00:00+00:00')"""
        )
        connection.commit()

    preview = preview_schema9(path)
    assert preview["from_version"] == 8
    assert preview["to_version"] == 9
    assert preview["confirmation_token"]

    result = migrate_schema9(path, preview["confirmation_token"])

    assert result["schema_version"] == 9
    assert inspect_schema(path) == 9
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            """SELECT final_translation, memory_version, context_hash
               FROM translation_versions WHERE block_id='block-1'"""
        ).fetchone()
    assert row == ("旧译文", None, "")


def test_schema9_migration_rejects_wrong_token(tmp_path):
    from src.core.v4.schema_v9 import SchemaMigrationError, migrate_schema9

    path = tmp_path / "book.db"
    _schema8_database(path)

    with pytest.raises(SchemaMigrationError, match="token"):
        migrate_schema9(path, "wrong-token")
