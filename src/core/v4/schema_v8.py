"""Schema 8 creation and the explicit upgrade boundary for parallel_v4."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


SCHEMA_VERSION = 8


class SchemaUpgradeRequired(RuntimeError):
    """An existing database must be migrated by the explicit migration command."""


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def inspect_schema(path: Path) -> int | None:
    """Return the persisted schema version, or ``None`` for an empty database."""

    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    with closing(_connect_readonly(path)) as connection:
        table = connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='schema_meta'"""
        ).fetchone()
        if table is None:
            return None
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise SchemaUpgradeRequired(
            "parallel_v4 database has an invalid schema version; "
            "run migrate-v4 --preview before opening it"
        ) from exc


def _schema8_feature_errors(connection: sqlite3.Connection) -> list[str]:
    required_tables = {
        "lexemes",
        "concept_lexemes",
        "concept_type_observations",
        "coreference_decisions",
        "concept_redirects",
        "form_occurrences",
        "knowledge_changes",
        "revalidation_tasks",
        "source_forms",
        "mentions",
        "rendering_rules",
        "candidate_resolutions",
        "translation_versions",
        "dependencies",
    }
    table_sql = {
        str(row[0]): str(row[1] or "")
        for row in connection.execute(
            """SELECT name, sql FROM sqlite_master
               WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
        )
    }
    errors = [
        f"missing table {name}" for name in sorted(required_tables - table_sql.keys())
    ]

    required_columns = {
        "source_forms": {"lexeme_id", "form", "normalized_form"},
        "mentions": {"lexeme_id", "concept_id", "evidence_id"},
        "rendering_rules": {"lexeme_id", "concept_id"},
        "candidate_resolutions": {"lexeme_id", "concept_id"},
        "translation_versions": {
            "validation_status",
            "validated_knowledge_version",
            "validation_fingerprint",
        },
        "revalidation_tasks": {"status"},
    }
    column_info: dict[str, dict[str, tuple[object, ...]]] = {}
    for table, expected in required_columns.items():
        if table not in table_sql:
            continue
        info = {
            str(row[1]): tuple(row)
            for row in connection.execute(f"PRAGMA table_info('{table}')")
        }
        column_info[table] = info
        for column in sorted(expected - info.keys()):
            errors.append(f"missing column {table}.{column}")
    for table in ("source_forms", "mentions"):
        lexeme = column_info.get(table, {}).get("lexeme_id")
        if lexeme is not None and int(lexeme[3]) != 1:
            errors.append(f"nullable column {table}.lexeme_id")
    if "concept_id" in column_info.get("source_forms", {}):
        errors.append("unexpected column source_forms.concept_id")

    index_requirements = {
        "uq_active_lexeme": (
            "lexemes",
            ("language", "normalized_form"),
            "whereretired_versionisnull",
        ),
        "uq_active_concept_lexeme_role": (
            "concept_lexemes",
            ("concept_id", "lexeme_id", "role"),
            "whereretired_versionisnull",
        ),
    }
    for index_name, (table, expected_columns, expected_predicate) in (
        index_requirements.items()
    ):
        if table not in table_sql:
            continue
        indexes = {
            str(row[1]): tuple(row)
            for row in connection.execute(f"PRAGMA index_list('{table}')")
        }
        index = indexes.get(index_name)
        if index is None or int(index[2]) != 1 or int(index[4]) != 1:
            errors.append(f"missing partial unique index {index_name}")
            continue
        actual_columns = tuple(
            str(row[2])
            for row in connection.execute(f"PRAGMA index_info('{index_name}')")
        )
        if actual_columns != expected_columns:
            errors.append(f"invalid columns for index {index_name}")
        index_sql_row = connection.execute(
            """SELECT sql FROM sqlite_master
               WHERE type='index' AND name=?""",
            (index_name,),
        ).fetchone()
        index_sql = (
            ""
            if index_sql_row is None
            else "".join(str(index_sql_row[0] or "").lower().split())
        )
        if expected_predicate not in index_sql:
            errors.append(f"invalid predicate for index {index_name}")

    compact_sql = {
        table: "".join(sql.lower().split()) for table, sql in table_sql.items()
    }
    rendering_sql = compact_sql.get("rendering_rules", "")
    if "check((lexeme_idisnull)!=(concept_idisnull))" not in rendering_sql:
        errors.append("missing rendering_rules subject CHECK")

    translation_sql = compact_sql.get("translation_versions", "")
    validation_check = (
        "check(validation_statusin"
        "('clean','pending','validating','warning_stale'))"
    )
    if validation_check not in translation_sql:
        errors.append("missing translation_versions validation_status CHECK")

    revalidation_sql = compact_sql.get("revalidation_tasks", "")
    revalidation_check = (
        "check(statusin('pending','validating','resolved_noop','resolved_patch',"
        "'resolved_retranslate','completed_with_warning'))"
    )
    if revalidation_check not in revalidation_sql:
        errors.append("missing revalidation_tasks status CHECK")
    return errors


def _assert_schema8_features(path: Path) -> None:
    with closing(_connect_readonly(path)) as connection:
        errors = _schema8_feature_errors(connection)
    if errors:
        detail = "; ".join(errors[:4])
        raise SchemaUpgradeRequired(
            f"parallel_v4 schema 8 is incomplete or corrupt ({detail}); "
            "run migrate-v4 --preview before opening it"
        )


def create_schema8(connection: sqlite3.Connection) -> None:
    """Create the schema 8 tables that differ from the schema 7 layout."""

    connection.executescript(
        """
        BEGIN IMMEDIATE;

        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS concepts (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            canonical_source TEXT NOT NULL,
            default_target TEXT NOT NULL DEFAULT '',
            working_target TEXT NOT NULL DEFAULT '',
            verified_target TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'provisional',
            scope TEXT NOT NULL DEFAULT 'book',
            locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
            primary_lexeme_id TEXT REFERENCES lexemes(id),
            anchor_mention_id INTEGER REFERENCES mentions(id),
            created_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
            retired_version INTEGER REFERENCES knowledge_versions(id),
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_concepts_active_source
            ON concepts(canonical_source, retired_version, locked);
        CREATE INDEX IF NOT EXISTS idx_concepts_primary_lexeme
            ON concepts(primary_lexeme_id, retired_version);

        CREATE TABLE IF NOT EXISTS lexemes (
            id TEXT PRIMARY KEY,
            language TEXT NOT NULL,
            normalized_form TEXT NOT NULL,
            canonical_form TEXT NOT NULL,
            default_target TEXT NOT NULL DEFAULT '',
            working_target TEXT NOT NULL DEFAULT '',
            verified_target TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'provisional',
            locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
            created_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
            retired_version INTEGER REFERENCES knowledge_versions(id),
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_active_lexeme
            ON lexemes(language, normalized_form) WHERE retired_version IS NULL;

        CREATE TABLE IF NOT EXISTS concept_lexemes (
            concept_id TEXT NOT NULL REFERENCES concepts(id),
            lexeme_id TEXT NOT NULL REFERENCES lexemes(id),
            role TEXT NOT NULL
                CHECK (role IN ('primary', 'alias', 'title', 'uncertain')),
            confidence REAL NOT NULL DEFAULT 1.0
                CHECK (confidence >= 0.0 AND confidence <= 1.0),
            status TEXT NOT NULL DEFAULT 'provisional',
            evidence_id INTEGER REFERENCES evidence(id),
            created_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
            retired_version INTEGER REFERENCES knowledge_versions(id),
            created_at TEXT NOT NULL,
            PRIMARY KEY (concept_id, lexeme_id, role, created_version)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_active_concept_lexeme_role
            ON concept_lexemes(concept_id, lexeme_id, role)
            WHERE retired_version IS NULL;
        CREATE INDEX IF NOT EXISTS idx_concept_lexemes_lexeme
            ON concept_lexemes(lexeme_id, retired_version, concept_id);

        CREATE TABLE IF NOT EXISTS source_forms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lexeme_id TEXT NOT NULL REFERENCES lexemes(id),
            form TEXT NOT NULL,
            normalized_form TEXT NOT NULL,
            grammar_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(lexeme_id, normalized_form, form)
        );
        CREATE INDEX IF NOT EXISTS idx_source_forms_normalized
            ON source_forms(normalized_form);
        CREATE INDEX IF NOT EXISTS idx_source_forms_lexeme
            ON source_forms(lexeme_id, normalized_form);

        CREATE TABLE IF NOT EXISTS mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_id TEXT NOT NULL REFERENCES blocks(id),
            paragraph_id TEXT NOT NULL,
            source_form TEXT NOT NULL,
            normalized_form TEXT NOT NULL,
            discourse_function TEXT NOT NULL,
            lexeme_id TEXT NOT NULL REFERENCES lexemes(id),
            concept_id TEXT REFERENCES concepts(id),
            evidence_id INTEGER NOT NULL REFERENCES evidence(id),
            UNIQUE(block_id, paragraph_id, source_form, evidence_id)
        );
        CREATE INDEX IF NOT EXISTS idx_mentions_lexeme
            ON mentions(lexeme_id, block_id, id);
        CREATE INDEX IF NOT EXISTS idx_mentions_concept
            ON mentions(concept_id, block_id, id);

        CREATE TABLE IF NOT EXISTS concept_type_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            concept_id TEXT REFERENCES concepts(id),
            lexeme_id TEXT NOT NULL REFERENCES lexemes(id),
            mention_id INTEGER REFERENCES mentions(id),
            evidence_id INTEGER REFERENCES evidence(id),
            kind TEXT NOT NULL,
            confidence REAL NOT NULL
                CHECK (confidence >= 0.0 AND confidence <= 1.0),
            source TEXT NOT NULL,
            adjudication_id TEXT REFERENCES candidate_adjudications(id),
            created_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
            retired_version INTEGER REFERENCES knowledge_versions(id),
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_type_observations_lexeme
            ON concept_type_observations(lexeme_id, retired_version, kind);
        CREATE INDEX IF NOT EXISTS idx_type_observations_concept
            ON concept_type_observations(concept_id, retired_version, kind);

        CREATE TABLE IF NOT EXISTS candidate_resolutions (
            id TEXT PRIMARY KEY,
            adjudication_id TEXT NOT NULL REFERENCES candidate_adjudications(id)
                ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES runs(id),
            cluster_id TEXT NOT NULL,
            candidate_id TEXT REFERENCES lexical_candidates(id),
            lexeme_id TEXT REFERENCES lexemes(id),
            concept_id TEXT REFERENCES concepts(id),
            evidence_id INTEGER REFERENCES evidence(id),
            decision TEXT NOT NULL,
            ordinal INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(adjudication_id, ordinal),
            FOREIGN KEY(cluster_id)
                REFERENCES candidate_clusters(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_candidate_resolutions_cluster
            ON candidate_resolutions(run_id, cluster_id, ordinal);
        CREATE INDEX IF NOT EXISTS idx_candidate_resolutions_candidate
            ON candidate_resolutions(candidate_id, decision);
        CREATE INDEX IF NOT EXISTS idx_candidate_resolutions_lexeme
            ON candidate_resolutions(lexeme_id, decision);
        CREATE INDEX IF NOT EXISTS idx_candidate_resolutions_concept
            ON candidate_resolutions(concept_id, decision);

        CREATE TABLE IF NOT EXISTS rendering_rules (
            id TEXT PRIMARY KEY,
            lexeme_id TEXT REFERENCES lexemes(id),
            concept_id TEXT REFERENCES concepts(id),
            condition_json TEXT NOT NULL,
            target TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'provisional',
            scope TEXT NOT NULL DEFAULT 'book',
            locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
            created_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
            retired_version INTEGER REFERENCES knowledge_versions(id),
            created_at TEXT NOT NULL,
            CHECK ((lexeme_id IS NULL) != (concept_id IS NULL))
        );
        CREATE INDEX IF NOT EXISTS idx_rendering_rules_lexeme
            ON rendering_rules(lexeme_id, retired_version, priority);
        CREATE INDEX IF NOT EXISTS idx_rendering_rules_concept
            ON rendering_rules(concept_id, retired_version, priority);

        CREATE TABLE IF NOT EXISTS coreference_decisions (
            id TEXT PRIMARY KEY,
            lexeme_id TEXT NOT NULL REFERENCES lexemes(id),
            left_anchor_type TEXT NOT NULL
                CHECK (left_anchor_type IN ('concept', 'mention_set')),
            left_anchor_id TEXT NOT NULL,
            right_anchor_type TEXT NOT NULL
                CHECK (right_anchor_type IN ('concept', 'mention_set')),
            right_anchor_id TEXT NOT NULL,
            relation TEXT NOT NULL
                CHECK (relation IN ('same', 'different', 'uncertain', 'non_entity')),
            decision_source TEXT NOT NULL
                CHECK (decision_source IN ('deterministic', 'dual_model', 'human')),
            confidence REAL NOT NULL DEFAULT 0.0
                CHECK (confidence >= 0.0 AND confidence <= 1.0),
            locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
            votes_json TEXT NOT NULL DEFAULT '[]',
            evidence_ids_json TEXT NOT NULL DEFAULT '[]',
            anchor_members_json TEXT NOT NULL DEFAULT '[]',
            payload_hash TEXT NOT NULL,
            created_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
            retired_version INTEGER REFERENCES knowledge_versions(id),
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_active_coreference_payload
            ON coreference_decisions(payload_hash) WHERE retired_version IS NULL;
        CREATE INDEX IF NOT EXISTS idx_coreference_decisions_lexeme
            ON coreference_decisions(lexeme_id, retired_version, relation);

        CREATE TABLE IF NOT EXISTS concept_redirects (
            retired_concept_id TEXT PRIMARY KEY REFERENCES concepts(id),
            canonical_concept_id TEXT NOT NULL REFERENCES concepts(id),
            reason TEXT NOT NULL,
            knowledge_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
            created_at TEXT NOT NULL,
            CHECK (retired_concept_id != canonical_concept_id)
        );
        CREATE INDEX IF NOT EXISTS idx_concept_redirects_canonical
            ON concept_redirects(canonical_concept_id);

        CREATE TABLE IF NOT EXISTS form_occurrences (
            lexeme_id TEXT NOT NULL REFERENCES lexemes(id),
            block_id TEXT NOT NULL REFERENCES blocks(id),
            start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
            end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
            source_form TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (lexeme_id, block_id, start_offset, end_offset)
        );
        CREATE INDEX IF NOT EXISTS idx_form_occurrences_block
            ON form_occurrences(block_id, start_offset, end_offset);

        CREATE TABLE IF NOT EXISTS knowledge_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            knowledge_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            change_kind TEXT NOT NULL,
            old_fingerprint TEXT NOT NULL DEFAULT '',
            new_fingerprint TEXT NOT NULL DEFAULT '',
            impact_level INTEGER NOT NULL CHECK (impact_level BETWEEN 0 AND 3),
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_changes_version
            ON knowledge_changes(knowledge_version, impact_level, id);
        CREATE INDEX IF NOT EXISTS idx_knowledge_changes_subject
            ON knowledge_changes(subject_type, subject_id, knowledge_version);

        CREATE TABLE IF NOT EXISTS translation_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_id TEXT NOT NULL REFERENCES blocks(id),
            pipeline TEXT NOT NULL,
            run_id TEXT REFERENCES runs(id),
            knowledge_version INTEGER,
            status TEXT NOT NULL,
            draft_translation TEXT NOT NULL DEFAULT '',
            final_translation TEXT NOT NULL DEFAULT '',
            analysis TEXT NOT NULL DEFAULT '',
            semantic_obligations TEXT NOT NULL DEFAULT '',
            memory_summary TEXT NOT NULL DEFAULT '',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            validation_status TEXT NOT NULL DEFAULT 'clean'
                CHECK (validation_status IN
                    ('clean', 'pending', 'validating', 'warning_stale')),
            validated_knowledge_version INTEGER REFERENCES knowledge_versions(id),
            validation_fingerprint TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_translation_active
            ON translation_versions(block_id, pipeline, active);
        CREATE INDEX IF NOT EXISTS idx_translation_validation
            ON translation_versions(validation_status, active, block_id);

        CREATE TABLE IF NOT EXISTS dependencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            translation_id INTEGER NOT NULL REFERENCES translation_versions(id),
            dependency_type TEXT NOT NULL,
            dependency_id TEXT NOT NULL,
            knowledge_version INTEGER NOT NULL,
            dependency_fingerprint TEXT NOT NULL DEFAULT '',
            matched_form TEXT NOT NULL DEFAULT '',
            occurrence_count INTEGER NOT NULL DEFAULT 0 CHECK (occurrence_count >= 0),
            rendered_target TEXT NOT NULL DEFAULT '',
            applied_rule_ids_json TEXT NOT NULL DEFAULT '[]',
            source_spans_json TEXT NOT NULL DEFAULT '[]',
            UNIQUE(translation_id, dependency_type, dependency_id)
        );
        CREATE INDEX IF NOT EXISTS idx_dependencies_reverse
            ON dependencies(dependency_type, dependency_id);

        CREATE TABLE IF NOT EXISTS revalidation_tasks (
            id TEXT PRIMARY KEY,
            translation_id INTEGER NOT NULL REFERENCES translation_versions(id),
            block_id TEXT NOT NULL REFERENCES blocks(id),
            from_knowledge_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
            to_knowledge_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
            change_set_hash TEXT NOT NULL,
            impact_level INTEGER NOT NULL CHECK (impact_level BETWEEN 0 AND 3),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN (
                    'pending', 'validating', 'resolved_noop', 'resolved_patch',
                    'resolved_retranslate', 'completed_with_warning'
                )),
            action TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            result_json TEXT NOT NULL DEFAULT '{}',
            replacement_translation_id INTEGER REFERENCES translation_versions(id),
            error TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_revalidation_change_set
            ON revalidation_tasks(translation_id, change_set_hash);
        CREATE INDEX IF NOT EXISTS idx_revalidation_tasks_status
            ON revalidation_tasks(status, impact_level, created_at, id);

        INSERT OR REPLACE INTO schema_meta(key, value)
        VALUES('schema_version', '8');
        COMMIT;
        """
    )


def assert_schema8_or_empty(path: Path) -> None:
    """Create schema 8 for an empty path or reject any older database."""

    path = Path(path)
    version = inspect_schema(path)
    if version == SCHEMA_VERSION:
        _assert_schema8_features(path)
        return
    if version is not None:
        raise SchemaUpgradeRequired(
            f"parallel_v4 schema {version} requires an explicit upgrade; "
            "run migrate-v4 --preview and confirm the migration before opening it"
        )

    if path.exists() and path.stat().st_size:
        with closing(_connect_readonly(path)) as connection:
            user_tables = connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
            ).fetchall()
        if user_tables:
            raise SchemaUpgradeRequired(
                "unversioned parallel_v4 database requires an explicit upgrade; "
                "run migrate-v4 --preview before opening it"
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        create_schema8(connection)
