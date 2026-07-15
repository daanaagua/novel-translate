"""SQLite storage and the single-writer commit surface for parallel_v4."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from copy import deepcopy
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from .audit_archive import (
    AuditArchive,
    AuditArchiveTransaction,
    AuditLocator,
    StorageBudget,
)
from .models import (
    FormOccurrence,
    ScanOutcome,
    TranslationOutcome,
    V4Block,
    V4BlockStatus,
    WorkingTargetRule,
)
from .matcher import ConceptMatcherCache, FrozenConceptIndex
from .schema_v8 import (
    SCHEMA_VERSION,
    SchemaUpgradeRequired,
    assert_schema8_or_empty,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, value: str, length: int = 16) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def normalize_english_form(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).translate(
        str.maketrans(
            {
                "’": "'", "‘": "'", "`": "'", "´": "'",
                "“": '"', "”": '"', "«": '"', "»": '"',
                "–": "-", "—": "-", "−": "-", "\u00a0": " ",
            }
        )
    )
    normalized = " ".join(normalized.strip().casefold().split())
    if normalized.endswith("'s") or normalized.endswith("’s"):
        normalized = normalized[:-2]
    elif normalized.endswith("'") or normalized.endswith("’"):
        normalized = normalized[:-1]
    return normalized


class StaleCandidateSnapshot(RuntimeError):
    """The pending candidate set changed while clusters were being built."""


class StaleAdjudicationCommit(RuntimeError):
    """Another run already committed an active decision for this cluster."""


class StaleAdjudicationLease(RuntimeError):
    """The candidate/source snapshot changed after the model lease was claimed."""


class KnowledgeSnapshotError(RuntimeError):
    """Persisted rendering knowledge cannot be decoded safely."""

    def __init__(self, rule_id: str, detail: str):
        self.rule_id = rule_id
        self.detail = detail
        super().__init__(f"invalid rendering rule {rule_id}: {detail}")


class V4Database:
    """All writes happen through this object on the coordinator thread."""

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root)
        self.root = self.project_root / "artifacts" / "parallel_v4"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "book.db"
        assert_schema8_or_empty(self.path)
        self.audit_archive = AuditArchive(self.root / "audit")
        self._audit_transactions: Dict[int, AuditArchiveTransaction] = {}
        self._audit_transactions_lock = RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        audit_transaction = self.audit_archive.begin()
        registered = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            with self._audit_transactions_lock:
                self._audit_transactions[id(connection)] = audit_transaction
            registered = True
            yield connection
            self._check_storage_budget(connection)
            connection.commit()
            audit_transaction.commit()
        except Exception:
            try:
                connection.rollback()
            finally:
                audit_transaction.rollback()
            raise
        finally:
            if registered:
                with self._audit_transactions_lock:
                    self._audit_transactions.pop(id(connection), None)
            connection.close()

    @staticmethod
    @contextmanager
    def _method_savepoint(
        connection: sqlite3.Connection,
        prefix: str,
    ) -> Iterator[None]:
        name = f"{prefix}_{uuid.uuid4().hex}"
        connection.execute(f"SAVEPOINT {name}")
        try:
            yield
            connection.execute(f"RELEASE SAVEPOINT {name}")
        except BaseException:
            try:
                connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
            except sqlite3.Error:
                pass
            try:
                connection.execute(f"RELEASE SAVEPOINT {name}")
            except sqlite3.Error:
                pass
            raise

    @staticmethod
    def _require_active_transaction(connection: sqlite3.Connection) -> None:
        if not connection.in_transaction:
            raise ValueError(
                "external database connection requires an active transaction"
            )

    def _audit_transaction_for(
        self, connection: sqlite3.Connection
    ) -> AuditArchiveTransaction:
        with self._audit_transactions_lock:
            transaction = self._audit_transactions.get(id(connection))
        if transaction is None:
            raise RuntimeError(
                "accepted audit persistence requires a managed database transaction"
            )
        return transaction

    @staticmethod
    def _source_bytes(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            """SELECT COALESCE(SUM(LENGTH(CAST(b.source_text AS BLOB))), 0)
               FROM blocks b
               JOIN source_editions s ON s.id=b.source_edition_id
               WHERE s.active=1"""
        ).fetchone()
        return int(row[0] or 0)

    @staticmethod
    def _active_database_bytes(connection: sqlite3.Connection) -> int:
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        return page_count * page_size

    def _check_storage_budget(self, connection: sqlite3.Connection) -> None:
        StorageBudget(self._source_bytes(connection)).check(
            self._active_database_bytes(connection)
        )

    @staticmethod
    def _associate_schema8_lexeme(
        connection: sqlite3.Connection,
        lexeme_id: str,
        concept_id: str,
        *,
        knowledge_version: int | None = None,
        created_at: str | None = None,
    ) -> None:
        """Associate an existing lexeme without deriving it from a surface form."""

        concept = connection.execute(
            """SELECT created_version, primary_lexeme_id
               FROM concepts WHERE id=?""",
            (concept_id,),
        ).fetchone()
        if concept is None:
            raise KeyError(f"concept does not exist: {concept_id}")
        if knowledge_version is None:
            knowledge_version = int(concept["created_version"])
        now = created_at or utc_now()
        connection.execute(
            """UPDATE concepts
               SET primary_lexeme_id=COALESCE(primary_lexeme_id, ?)
               WHERE id=?""",
            (lexeme_id, concept_id),
        )
        primary_lexeme_id = connection.execute(
            "SELECT primary_lexeme_id FROM concepts WHERE id=?",
            (concept_id,),
        ).fetchone()[0]
        role = "primary" if primary_lexeme_id == lexeme_id else "alias"
        connection.execute(
            """INSERT OR IGNORE INTO concept_lexemes(
                   concept_id, lexeme_id, role, confidence, status,
                   created_version, created_at)
               VALUES(?, ?, ?, 1.0, 'provisional', ?, ?)""",
            (concept_id, lexeme_id, role, knowledge_version, now),
        )

    def _ensure_lexeme_record(
        self,
        connection: sqlite3.Connection,
        source_form: str,
        *,
        language: str,
        knowledge_version: int | None = None,
        created_at: str | None = None,
    ) -> tuple[str, str]:
        if not isinstance(language, str) or not language.strip():
            raise ValueError("lexeme language cannot be empty")
        if not isinstance(source_form, str) or not source_form:
            raise ValueError("lexeme source form cannot be empty")
        normalized = normalize_english_form(source_form)
        if not normalized:
            raise ValueError("lexeme normalized form cannot be empty")
        lexeme_id = stable_id("lexeme", f"{language}:{normalized}")
        if knowledge_version is None:
            knowledge_version = int(
                connection.execute(
                    "SELECT MAX(id) FROM knowledge_versions"
                ).fetchone()[0]
            )
        connection.execute(
            """INSERT OR IGNORE INTO lexemes(
                   id, language, normalized_form, canonical_form,
                   created_version, created_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (
                lexeme_id,
                language,
                normalized,
                source_form,
                knowledge_version,
                created_at or utc_now(),
            ),
        )
        active = connection.execute(
            """SELECT id FROM lexemes
               WHERE language=? AND normalized_form=?
                     AND retired_version IS NULL""",
            (language, normalized),
        ).fetchone()
        if active is None or str(active[0]) != lexeme_id:
            raise RuntimeError(
                "active lexeme violates the stable ownership identity"
            )
        return lexeme_id, normalized

    def ensure_lexeme(
        self,
        source_form: str,
        *,
        language: str = "en",
        connection: sqlite3.Connection | None = None,
    ) -> str:
        if connection is None:
            with self.transaction() as owned_connection:
                return self.ensure_lexeme(
                    source_form,
                    language=language,
                    connection=owned_connection,
                )
        self._require_active_transaction(connection)
        lexeme_id, normalized = self._ensure_lexeme_record(
            connection,
            source_form,
            language=language,
        )
        connection.execute(
            """INSERT OR IGNORE INTO source_forms(
                   lexeme_id, form, normalized_form, grammar_json)
               VALUES(?, ?, ?, '{}')""",
            (lexeme_id, source_form, normalized),
        )
        return lexeme_id

    def record_type_observation(
        self,
        lexeme_id: str,
        kind: str,
        *,
        confidence: float,
        source: str,
        mention_id: int | None = None,
        concept_id: str | None = None,
        evidence_id: int | None = None,
        adjudication_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        if connection is not None:
            self._require_active_transaction(connection)
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("type observation kind cannot be empty")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("type observation source cannot be empty")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise ValueError("type observation confidence must be numeric") from exc
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("type observation confidence must be between 0 and 1")
        if connection is None:
            with self.transaction() as owned_connection:
                return self.record_type_observation(
                    lexeme_id,
                    kind,
                    confidence=confidence,
                    source=source,
                    mention_id=mention_id,
                    concept_id=concept_id,
                    evidence_id=evidence_id,
                    adjudication_id=adjudication_id,
                    connection=owned_connection,
                )
        existing = connection.execute(
            """SELECT id FROM concept_type_observations
               WHERE lexeme_id=? AND kind=? AND confidence=? AND source=?
                     AND mention_id IS ? AND concept_id IS ?
                     AND evidence_id IS ? AND adjudication_id IS ?
                     AND retired_version IS NULL
               ORDER BY id LIMIT 1""",
            (
                lexeme_id,
                kind,
                confidence,
                source,
                mention_id,
                concept_id,
                evidence_id,
                adjudication_id,
            ),
        ).fetchone()
        if existing is not None:
            return int(existing[0])
        version = int(
            connection.execute(
                "SELECT MAX(id) FROM knowledge_versions"
            ).fetchone()[0]
        )
        cursor = connection.execute(
            """INSERT INTO concept_type_observations(
                   concept_id, lexeme_id, mention_id, evidence_id, kind,
                   confidence, source, adjudication_id, created_version,
                   created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                concept_id,
                lexeme_id,
                mention_id,
                evidence_id,
                kind,
                confidence,
                source,
                adjudication_id,
                version,
                utc_now(),
            ),
        )
        return int(cursor.lastrowid)

    def record_form_occurrences(
        self,
        rows: Sequence[FormOccurrence],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        if connection is not None:
            self._require_active_transaction(connection)
        rows = tuple(rows)
        if connection is None:
            with self.transaction() as owned_connection:
                return self.record_form_occurrences(
                    rows,
                    connection=owned_connection,
                )
        if not rows:
            return 0

        blocks: dict[str, Any] = {}
        active_lexemes: set[str] = set()
        for occurrence in rows:
            lexeme_id = occurrence.lexeme_id
            if not isinstance(lexeme_id, str) or not lexeme_id.strip():
                raise ValueError("form occurrence lexeme_id cannot be empty")
            if lexeme_id not in active_lexemes:
                active_lexeme = connection.execute(
                    """SELECT 1 FROM lexemes
                       WHERE id=? AND retired_version IS NULL""",
                    (lexeme_id,),
                ).fetchone()
                if active_lexeme is None:
                    raise KeyError(f"active lexeme does not exist: {lexeme_id}")
                active_lexemes.add(lexeme_id)
            block = blocks.get(occurrence.block_id)
            if block is None:
                block = connection.execute(
                    """SELECT b.source_text, b.source_hash, s.active
                       FROM blocks b
                       JOIN source_editions s ON s.id=b.source_edition_id
                       WHERE b.id=?""",
                    (occurrence.block_id,),
                ).fetchone()
                if block is None:
                    raise KeyError(f"block does not exist: {occurrence.block_id}")
                blocks[occurrence.block_id] = block
            if int(block[2]) != 1:
                raise ValueError(
                    f"block is not part of the active source edition: "
                    f"{occurrence.block_id}"
                )
            if str(block[1]) != occurrence.source_hash:
                raise ValueError(
                    f"source hash does not match block: {occurrence.block_id}"
                )
            start = occurrence.start_offset
            end = occurrence.end_offset
            source_text = str(block[0])
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or not 0 <= start < end <= len(source_text)
            ):
                raise ValueError(
                    f"invalid half-open occurrence offsets for block: "
                    f"{occurrence.block_id}"
                )
            if source_text[start:end] != occurrence.source_form:
                raise ValueError(
                    f"source form does not match block slice: {occurrence.block_id}"
                )

        with self._method_savepoint(connection, "form_occurrences"):
            inserted = 0
            now = utc_now()
            for occurrence in rows:
                cursor = connection.execute(
                    """INSERT INTO form_occurrences(
                           lexeme_id, block_id, start_offset, end_offset,
                           source_form, source_hash, created_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(
                           lexeme_id, block_id, start_offset, end_offset
                       ) DO NOTHING""",
                    (
                        occurrence.lexeme_id,
                        occurrence.block_id,
                        occurrence.start_offset,
                        occurrence.end_offset,
                        occurrence.source_form,
                        occurrence.source_hash,
                        now,
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def _ensure_schema8_lexeme(
        self,
        connection: sqlite3.Connection,
        source_form: str,
        *,
        normalized_form: str | None = None,
        concept_id: str | None = None,
        knowledge_version: int | None = None,
        created_at: str | None = None,
    ) -> str:
        """Create neutral schema-8 ownership for legacy write paths."""

        normalized = normalize_english_form(source_form)
        if normalized_form is not None and normalized_form != normalized:
            raise ValueError("schema8 lexeme normalization does not match source form")
        if knowledge_version is None:
            knowledge_version = int(
                connection.execute(
                    "SELECT MAX(id) FROM knowledge_versions"
                ).fetchone()[0]
            )
        now = created_at or utc_now()
        lexeme_id, _ = self._ensure_lexeme_record(
            connection,
            source_form,
            language="en",
            knowledge_version=knowledge_version,
            created_at=now,
        )
        if concept_id is not None:
            self._associate_schema8_lexeme(
                connection,
                lexeme_id,
                concept_id,
                knowledge_version=knowledge_version,
                created_at=now,
            )
        return lexeme_id

    def initialize(self) -> None:
        with closing(self.connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS source_editions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_sha256 TEXT NOT NULL,
                    normalized_sha256 TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(normalized_sha256, parser_version)
                );

                CREATE TABLE IF NOT EXISTS blocks (
                    id TEXT PRIMARY KEY,
                    legacy_id TEXT NOT NULL,
                    source_edition_id INTEGER NOT NULL REFERENCES source_editions(id),
                    chapter_id TEXT NOT NULL,
                    chapter_title TEXT NOT NULL,
                    chapter_index INTEGER NOT NULL,
                    block_index INTEGER NOT NULL,
                    global_index INTEGER NOT NULL,
                    block_type TEXT NOT NULL DEFAULT 'prose',
                    source_text TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    token_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_edition_id, chapter_id, block_index)
                );
                CREATE INDEX IF NOT EXISTS idx_blocks_order
                    ON blocks(source_edition_id, global_index);

                CREATE TABLE IF NOT EXISTS knowledge_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_id INTEGER REFERENCES knowledge_versions(id),
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    knowledge_version INTEGER,
                    config_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS audit_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT REFERENCES runs(id),
                    block_id TEXT REFERENCES blocks(id),
                    purpose TEXT NOT NULL,
                    model TEXT NOT NULL,
                    knowledge_version INTEGER,
                    request_json TEXT NOT NULL,
                    raw_response TEXT NOT NULL,
                    parsed_json TEXT,
                    accepted INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    elapsed_ms INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    archive_relative_path TEXT,
                    archive_offset INTEGER,
                    archive_compressed_length INTEGER,
                    archive_sha256 TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    block_id TEXT NOT NULL REFERENCES blocks(id),
                    paragraph_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_form TEXT,
                    evidence_quote TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    extractor TEXT NOT NULL,
                    run_id TEXT REFERENCES runs(id),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_block ON evidence(block_id);

                CREATE TABLE IF NOT EXISTS lexical_candidates (
                    id TEXT PRIMARY KEY,
                    block_id TEXT NOT NULL REFERENCES blocks(id),
                    paragraph_id TEXT NOT NULL,
                    start_offset INTEGER NOT NULL,
                    end_offset INTEGER NOT NULL,
                    original_text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    left_context TEXT NOT NULL DEFAULT '',
                    right_context TEXT NOT NULL DEFAULT '',
                    extraction_reason TEXT NOT NULL,
                    book_frequency INTEGER NOT NULL DEFAULT 1,
                    risk_flags_json TEXT NOT NULL DEFAULT '[]',
                    model_status TEXT NOT NULL DEFAULT 'pending',
                    resolution_status TEXT NOT NULL DEFAULT 'pending',
                    selected INTEGER NOT NULL DEFAULT 0,
                    run_id TEXT REFERENCES runs(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(block_id, start_offset, end_offset, normalized_text)
                );
                CREATE INDEX IF NOT EXISTS idx_lexical_candidates_block
                    ON lexical_candidates(block_id, selected, model_status);
                CREATE INDEX IF NOT EXISTS idx_lexical_candidates_normalized
                    ON lexical_candidates(normalized_text);

                CREATE TABLE IF NOT EXISTS candidate_clusters (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    risk_flags_json TEXT NOT NULL DEFAULT '[]',
                    affected_blocks_json TEXT NOT NULL DEFAULT '[]',
                    affected_block_count INTEGER NOT NULL DEFAULT 0,
                    ordinal INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    lease_run_id TEXT REFERENCES runs(id),
                    lease_acquired_at TEXT,
                    lease_snapshot_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, id)
                );
                CREATE INDEX IF NOT EXISTS idx_candidate_clusters_order
                    ON candidate_clusters(run_id, ordinal, id);

                CREATE TABLE IF NOT EXISTS candidate_cluster_members (
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    cluster_id TEXT NOT NULL REFERENCES candidate_clusters(id)
                        ON DELETE CASCADE,
                    candidate_id TEXT NOT NULL REFERENCES lexical_candidates(id),
                    role TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(cluster_id, candidate_id)
                );
                CREATE INDEX IF NOT EXISTS idx_candidate_cluster_members_candidate
                    ON candidate_cluster_members(candidate_id, run_id, cluster_id);
                CREATE INDEX IF NOT EXISTS idx_candidate_cluster_members_cluster
                    ON candidate_cluster_members(run_id, cluster_id, ordinal);

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
                    locked INTEGER NOT NULL DEFAULT 0,
                    created_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
                    retired_version INTEGER REFERENCES knowledge_versions(id),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_concepts_active_source
                    ON concepts(canonical_source, retired_version, locked);

                CREATE TABLE IF NOT EXISTS candidate_adjudications (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    cluster_id TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    selected_candidate_ids_json TEXT NOT NULL DEFAULT '[]',
                    entity_kind TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.0,
                    reason TEXT NOT NULL DEFAULT '',
                    rounds INTEGER NOT NULL DEFAULT 1,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    knowledge_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    superseded_at TEXT,
                    FOREIGN KEY(cluster_id)
                        REFERENCES candidate_clusters(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS candidate_resolutions (
                    id TEXT PRIMARY KEY,
                    adjudication_id TEXT NOT NULL REFERENCES candidate_adjudications(id)
                        ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    cluster_id TEXT NOT NULL,
                    candidate_id TEXT REFERENCES lexical_candidates(id),
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
                CREATE INDEX IF NOT EXISTS idx_candidate_resolutions_concept
                    ON candidate_resolutions(concept_id, decision);

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

                CREATE TABLE IF NOT EXISTS rendering_rules (
                    id TEXT PRIMARY KEY,
                    concept_id TEXT NOT NULL REFERENCES concepts(id),
                    condition_json TEXT NOT NULL,
                    target TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'provisional',
                    scope TEXT NOT NULL DEFAULT 'book',
                    locked INTEGER NOT NULL DEFAULT 0,
                    created_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
                    retired_version INTEGER REFERENCES knowledge_versions(id),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS usage_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mention_id INTEGER NOT NULL REFERENCES mentions(id),
                    rendering TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'provisional',
                    scope TEXT NOT NULL DEFAULT 'occurrence',
                    locked INTEGER NOT NULL DEFAULT 0,
                    created_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
                    retired_version INTEGER REFERENCES knowledge_versions(id),
                    created_at TEXT NOT NULL
                );

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
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_translation_active
                    ON translation_versions(block_id, pipeline, active);

                CREATE TABLE IF NOT EXISTS dependencies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    translation_id INTEGER NOT NULL REFERENCES translation_versions(id),
                    dependency_type TEXT NOT NULL,
                    dependency_id TEXT NOT NULL,
                    knowledge_version INTEGER NOT NULL,
                    UNIQUE(translation_id, dependency_type, dependency_id)
                );
                CREATE INDEX IF NOT EXISTS idx_dependencies_reverse
                    ON dependencies(dependency_type, dependency_id);

                CREATE TABLE IF NOT EXISTS human_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    block_id TEXT REFERENCES blocks(id),
                    kind TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS baseline_documents (
                    id TEXT PRIMARY KEY,
                    source_edition_id INTEGER NOT NULL REFERENCES source_editions(id),
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_sha256 TEXT NOT NULL,
                    paragraph_count INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_edition_id, name, file_sha256)
                );
                CREATE INDEX IF NOT EXISTS idx_baseline_documents_active
                    ON baseline_documents(source_edition_id, active, name);

                CREATE TABLE IF NOT EXISTS source_paragraphs (
                    source_edition_id INTEGER NOT NULL REFERENCES source_editions(id),
                    paragraph_index INTEGER NOT NULL,
                    source_text TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    char_start INTEGER NOT NULL,
                    char_end INTEGER NOT NULL,
                    PRIMARY KEY(source_edition_id, paragraph_index)
                );

                CREATE TABLE IF NOT EXISTS baseline_paragraphs (
                    baseline_document_id TEXT NOT NULL REFERENCES baseline_documents(id),
                    paragraph_index INTEGER NOT NULL,
                    target_text TEXT NOT NULL,
                    target_hash TEXT NOT NULL,
                    PRIMARY KEY(baseline_document_id, paragraph_index)
                );

                CREATE TABLE IF NOT EXISTS block_baseline_links (
                    block_id TEXT NOT NULL REFERENCES blocks(id),
                    baseline_document_id TEXT NOT NULL REFERENCES baseline_documents(id),
                    paragraph_index INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    overlap_start INTEGER NOT NULL,
                    overlap_end INTEGER NOT NULL,
                    partial_start INTEGER NOT NULL DEFAULT 0,
                    partial_end INTEGER NOT NULL DEFAULT 0,
                    alignment_status TEXT NOT NULL DEFAULT 'exact',
                    PRIMARY KEY(block_id, baseline_document_id, paragraph_index)
                );
                CREATE INDEX IF NOT EXISTS idx_block_baseline_links
                    ON block_baseline_links(block_id, baseline_document_id, ordinal);

                CREATE TABLE IF NOT EXISTS claims (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    subject_form TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'proposed',
                    scope TEXT NOT NULL DEFAULT 'book',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    reveal_global_index INTEGER NOT NULL,
                    high_impact INTEGER NOT NULL DEFAULT 0,
                    locked INTEGER NOT NULL DEFAULT 0,
                    created_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
                    retired_version INTEGER REFERENCES knowledge_versions(id),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_claims_reveal
                    ON claims(status, reveal_global_index);

                CREATE TABLE IF NOT EXISTS claim_evidence (
                    claim_id TEXT NOT NULL REFERENCES claims(id),
                    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
                    PRIMARY KEY(claim_id, evidence_id)
                );

                CREATE TABLE IF NOT EXISTS verification_tasks (
                    id TEXT PRIMARY KEY,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    required_votes INTEGER NOT NULL DEFAULT 2,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_verification_tasks_status
                    ON verification_tasks(status, created_at);

                CREATE TABLE IF NOT EXISTS verification_votes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES verification_tasks(id),
                    verifier_index INTEGER NOT NULL,
                    verdict TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    audit_call_id INTEGER REFERENCES audit_calls(id),
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, verifier_index)
                );

                CREATE TABLE IF NOT EXISTS annotations (
                    id TEXT PRIMARY KEY,
                    block_id TEXT NOT NULL REFERENCES blocks(id),
                    paragraph_index INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'proposed',
                    source TEXT NOT NULL DEFAULT 'human',
                    created_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_annotations_block
                    ON annotations(block_id, status, paragraph_index);

                CREATE TABLE IF NOT EXISTS repair_tasks (
                    id TEXT PRIMARY KEY,
                    block_id TEXT NOT NULL REFERENCES blocks(id),
                    status TEXT NOT NULL DEFAULT 'open',
                    issues_json TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    resolved_at TEXT,
                    translation_id INTEGER REFERENCES translation_versions(id)
                );

                CREATE TABLE IF NOT EXISTS comparison_votes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    block_id TEXT NOT NULL UNIQUE REFERENCES blocks(id),
                    candidate_a_origin TEXT NOT NULL,
                    candidate_b_origin TEXT NOT NULL,
                    candidate_a_hash TEXT NOT NULL DEFAULT '',
                    candidate_b_hash TEXT NOT NULL DEFAULT '',
                    choice TEXT NOT NULL,
                    selected_origin TEXT,
                    blinded INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_comparison_votes_choice
                    ON comparison_votes(choice, updated_at);

                CREATE TABLE IF NOT EXISTS comparison_vote_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_vote_id INTEGER NOT NULL,
                    block_id TEXT NOT NULL REFERENCES blocks(id),
                    candidate_a_origin TEXT NOT NULL,
                    candidate_b_origin TEXT NOT NULL,
                    candidate_a_hash TEXT NOT NULL DEFAULT '',
                    candidate_b_hash TEXT NOT NULL DEFAULT '',
                    choice TEXT NOT NULL,
                    selected_origin TEXT,
                    blinded INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_comparison_vote_history_block
                    ON comparison_vote_history(block_id, archived_at);
                """
            )
            if connection.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0] == 0:
                connection.execute(
                    "INSERT INTO knowledge_versions(parent_id, reason, created_at) VALUES(NULL, ?, ?)",
                    ("initialize parallel_v4", utc_now()),
                )
            self._migrate_candidate_cluster_schema(connection)
            self._migrate_candidate_adjudication_schema(connection)
            self._migrate_audit_call_schema(connection)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(blocks)").fetchall()
            }
            if "legacy_id" not in columns:
                connection.execute("ALTER TABLE blocks ADD COLUMN legacy_id TEXT NOT NULL DEFAULT ''")
                connection.execute("UPDATE blocks SET legacy_id=id WHERE legacy_id=''")
            claim_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(claims)").fetchall()
            }
            if "subject_form" not in claim_columns:
                connection.execute(
                    "ALTER TABLE claims ADD COLUMN subject_form TEXT NOT NULL DEFAULT ''"
                )
            translation_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(translation_versions)"
                ).fetchall()
            }
            if "semantic_obligations" not in translation_columns:
                connection.execute(
                    """ALTER TABLE translation_versions
                       ADD COLUMN semantic_obligations TEXT NOT NULL DEFAULT ''"""
                )
            vote_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(comparison_votes)"
                ).fetchall()
            }
            if "candidate_a_hash" not in vote_columns:
                connection.execute(
                    "ALTER TABLE comparison_votes ADD COLUMN candidate_a_hash TEXT NOT NULL DEFAULT ''"
                )
            if "candidate_b_hash" not in vote_columns:
                connection.execute(
                    "ALTER TABLE comparison_votes ADD COLUMN candidate_b_hash TEXT NOT NULL DEFAULT ''"
                )
            lexical_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(lexical_candidates)"
                ).fetchall()
            }
            if "risk_flags_json" not in lexical_columns:
                connection.execute(
                    """ALTER TABLE lexical_candidates
                       ADD COLUMN risk_flags_json TEXT NOT NULL DEFAULT '[]'"""
                )
            if "resolution_status" not in lexical_columns:
                connection.execute(
                    """ALTER TABLE lexical_candidates
                       ADD COLUMN resolution_status TEXT NOT NULL DEFAULT 'pending'"""
                )
            concept_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(concepts)").fetchall()
            }
            if "working_target" not in concept_columns:
                connection.execute(
                    """ALTER TABLE concepts
                       ADD COLUMN working_target TEXT NOT NULL DEFAULT ''"""
                )
            if "verified_target" not in concept_columns:
                connection.execute(
                    """ALTER TABLE concepts
                       ADD COLUMN verified_target TEXT NOT NULL DEFAULT ''"""
                )
            connection.execute(
                """UPDATE concepts
                   SET working_target=CASE
                           WHEN working_target='' THEN default_target
                           ELSE working_target END,
                       verified_target=CASE
                           WHEN verified_target='' THEN default_target
                           ELSE verified_target END
                   WHERE locked=1 AND default_target!=''"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_lexical_candidates_resolution
                   ON lexical_candidates(resolution_status, block_id, id)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_lexical_candidates_pending
                   ON lexical_candidates(resolution_status, block_id, updated_at, id)"""
            )
            legacy_votes = connection.execute(
                """SELECT * FROM comparison_votes
                   WHERE candidate_a_hash='' OR candidate_b_hash=''"""
            ).fetchall()
            for legacy_vote in legacy_votes:
                self._archive_comparison_vote(
                    connection, legacy_vote, utc_now()
                )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.commit()

    def _migrate_candidate_cluster_schema(
        self, connection: sqlite3.Connection
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(candidate_clusters)"
            ).fetchall()
        }
        additions = {
            "state": "TEXT NOT NULL DEFAULT 'pending'",
            "lease_run_id": "TEXT REFERENCES runs(id)",
            "lease_acquired_at": "TEXT",
            "lease_snapshot_hash": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE candidate_clusters ADD COLUMN {name} {declaration}"
                )
        connection.execute(
            """UPDATE candidate_clusters
               SET state=CASE
                   WHEN EXISTS(
                       SELECT 1 FROM candidate_adjudications ca
                       WHERE ca.cluster_id=candidate_clusters.id
                   ) THEN 'adjudicated'
                   WHEN state NOT IN ('pending', 'leased', 'adjudicated', 'stale')
                       THEN 'pending'
                   ELSE state END"""
        )
        connection.execute(
            """UPDATE candidate_clusters
               SET lease_run_id=NULL, lease_acquired_at=NULL,
                   lease_snapshot_hash=NULL
               WHERE state!='leased'"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_candidate_clusters_state
               ON candidate_clusters(state, id, ordinal)"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_candidate_clusters_lease
               ON candidate_clusters(lease_run_id, state, lease_acquired_at)"""
        )

    @staticmethod
    def _candidate_adjudication_table_sql() -> str:
        return """CREATE TABLE candidate_adjudications (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(id),
            cluster_id TEXT NOT NULL,
            verdict TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            selected_candidate_ids_json TEXT NOT NULL DEFAULT '[]',
            entity_kind TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.0,
            reason TEXT NOT NULL DEFAULT '',
            rounds INTEGER NOT NULL DEFAULT 1,
            payload_json TEXT NOT NULL DEFAULT '{}',
            knowledge_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            superseded_at TEXT,
            FOREIGN KEY(cluster_id)
                REFERENCES candidate_clusters(id) ON DELETE CASCADE
        )"""

    @staticmethod
    def _candidate_resolution_table_sql() -> str:
        return """CREATE TABLE candidate_resolutions (
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
        )"""

    def _migrate_candidate_adjudication_schema(
        self, connection: sqlite3.Connection
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(candidate_adjudications)"
            ).fetchall()
        }
        additions = {
            "payload_hash": "TEXT NOT NULL DEFAULT ''",
            "knowledge_version": "INTEGER REFERENCES knowledge_versions(id)",
            "active": "INTEGER NOT NULL DEFAULT 1",
            "superseded_at": "TEXT",
        }
        missing_columns = set(additions).difference(columns)
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE candidate_adjudications ADD COLUMN {name} {declaration}"
                )

        valid_versions = {
            int(row[0])
            for row in connection.execute("SELECT id FROM knowledge_versions")
        }
        fallback_version = max(valid_versions)
        rows = connection.execute(
            """SELECT ca.id, ca.run_id, ca.cluster_id, ca.payload_json,
                      ca.payload_hash, ca.knowledge_version, ca.active, ca.created_at,
                      ca.updated_at, ca.superseded_at, r.knowledge_version run_version
               FROM candidate_adjudications ca
               LEFT JOIN runs r ON r.id=ca.run_id
               ORDER BY ca.cluster_id, ca.updated_at DESC,
                        ca.created_at DESC, ca.id DESC"""
        ).fetchall()
        active_clusters: set[str] = set()
        for row in rows:
            raw_payload = row["payload_json"] or "{}"
            try:
                payload = json.loads(raw_payload)
            except (TypeError, ValueError):
                payload = {"legacy_payload": str(raw_payload)}
            canonical_payload = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            payload_hash = row["payload_hash"]
            if "payload_hash" in missing_columns or not payload_hash:
                payload_hash = hashlib.sha256(
                    canonical_payload.encode("utf-8")
                ).hexdigest()
            knowledge_version = row["knowledge_version"]
            if knowledge_version not in valid_versions:
                matched_version = connection.execute(
                    """SELECT id FROM knowledge_versions
                       WHERE reason=?
                       ORDER BY
                           CASE WHEN julianday(created_at) IS NULL
                                  OR julianday(?) IS NULL THEN 1 ELSE 0 END,
                           abs(julianday(created_at) - julianday(?)),
                           id DESC
                       LIMIT 1""",
                    (
                        f"candidate adjudication {row['run_id']}",
                        row["updated_at"],
                        row["updated_at"],
                    ),
                ).fetchone()
                if matched_version is not None:
                    knowledge_version = int(matched_version["id"])
            if knowledge_version not in valid_versions:
                knowledge_version = row["run_version"]
            if knowledge_version not in valid_versions:
                knowledge_version = fallback_version
            cluster_key = str(row["cluster_id"])
            requested_active = (
                True if "active" in missing_columns else bool(row["active"])
            )
            active = int(requested_active and cluster_key not in active_clusters)
            if active:
                active_clusters.add(cluster_key)
            superseded_at = row["superseded_at"]
            if (
                not active
                and superseded_at is None
            ):
                superseded_at = row["updated_at"] or row["created_at"] or utc_now()
            connection.execute(
                """UPDATE candidate_adjudications
                   SET payload_hash=?, knowledge_version=?, active=?, superseded_at=?
                   WHERE id=?""",
                (
                    payload_hash,
                    int(knowledge_version),
                    active,
                    superseded_at,
                    row["id"],
                ),
            )

        has_legacy_unique = False
        for index in connection.execute(
            "PRAGMA index_list(candidate_adjudications)"
        ).fetchall():
            if not bool(index["unique"]) or bool(index["partial"]):
                continue
            index_columns = tuple(
                row["name"]
                for row in connection.execute(
                    f"PRAGMA index_info('{index['name']}')"
                ).fetchall()
            )
            if index_columns == ("run_id", "cluster_id"):
                has_legacy_unique = True
                break

        if has_legacy_unique:
            connection.execute(
                "ALTER TABLE candidate_resolutions RENAME TO legacy_candidate_resolutions"
            )
            connection.execute(
                "ALTER TABLE candidate_adjudications RENAME TO legacy_candidate_adjudications"
            )
            connection.execute(self._candidate_adjudication_table_sql())
            connection.execute(self._candidate_resolution_table_sql())
            connection.execute(
                """INSERT INTO candidate_adjudications(
                       id, run_id, cluster_id, verdict, payload_hash,
                       selected_candidate_ids_json, entity_kind, confidence,
                       reason, rounds, payload_json, knowledge_version, active,
                       created_at, updated_at, superseded_at)
                   SELECT id, run_id, cluster_id, verdict, payload_hash,
                          selected_candidate_ids_json, entity_kind, confidence,
                          reason, rounds, payload_json, knowledge_version, active,
                          created_at, updated_at, superseded_at
                   FROM legacy_candidate_adjudications"""
            )
            connection.execute(
                """INSERT INTO candidate_resolutions(
                       id, adjudication_id, run_id, cluster_id, candidate_id,
                       lexeme_id, concept_id, evidence_id, decision, ordinal,
                       payload_json, created_at)
                   SELECT id, adjudication_id, run_id, cluster_id, candidate_id,
                          lexeme_id, concept_id, evidence_id, decision, ordinal,
                          payload_json, created_at
                   FROM legacy_candidate_resolutions"""
            )
            connection.execute("DROP TABLE legacy_candidate_resolutions")
            connection.execute("DROP TABLE legacy_candidate_adjudications")

        connection.execute("DROP INDEX IF EXISTS idx_candidate_adjudications_cluster")
        connection.execute("DROP INDEX IF EXISTS idx_candidate_adjudications_active")
        connection.execute(
            """CREATE INDEX idx_candidate_adjudications_cluster
               ON candidate_adjudications(cluster_id, active, created_at)"""
        )
        connection.execute(
            """CREATE UNIQUE INDEX idx_candidate_adjudications_active
               ON candidate_adjudications(cluster_id) WHERE active=1"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_candidate_resolutions_cluster
               ON candidate_resolutions(run_id, cluster_id, ordinal)"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_candidate_resolutions_candidate
               ON candidate_resolutions(candidate_id, decision)"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_candidate_resolutions_concept
               ON candidate_resolutions(concept_id, decision)"""
        )

    @staticmethod
    def _migrate_audit_call_schema(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(audit_calls)").fetchall()
        }
        additions = {
            "archive_relative_path": "TEXT",
            "archive_offset": "INTEGER",
            "archive_compressed_length": "INTEGER",
            "archive_sha256": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE audit_calls ADD COLUMN {name} {declaration}"
                )

    def current_knowledge_version(self) -> int:
        with closing(self.connect()) as connection:
            return int(connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0])

    def active_source_edition_id(self) -> int:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT id FROM source_editions WHERE active=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise ValueError("尚未迁移活动来源版本")
            return int(row["id"])

    def create_knowledge_version(self, reason: str, connection: sqlite3.Connection) -> int:
        parent = connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0]
        cursor = connection.execute(
            "INSERT INTO knowledge_versions(parent_id, reason, created_at) VALUES(?, ?, ?)",
            (parent, reason, utc_now()),
        )
        return int(cursor.lastrowid)

    def ensure_source_edition(
        self,
        raw_sha256: str,
        normalized_sha256: str,
        parser_version: str,
        source_path: str,
    ) -> int:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM source_editions WHERE normalized_sha256=? AND parser_version=?",
                (normalized_sha256, parser_version),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE source_editions SET active=CASE WHEN id=? THEN 1 ELSE 0 END",
                    (int(row["id"]),),
                )
                return int(row["id"])
            connection.execute("UPDATE source_editions SET active=0")
            cursor = connection.execute(
                """INSERT INTO source_editions(
                       raw_sha256, normalized_sha256, parser_version, source_path, active, created_at
                   ) VALUES(?, ?, ?, ?, 1, ?)""",
                (raw_sha256, normalized_sha256, parser_version, source_path, utc_now()),
            )
            return int(cursor.lastrowid)

    def upsert_blocks(self, source_edition_id: int, blocks: Sequence[Dict[str, Any]]) -> None:
        with self.transaction() as connection:
            for item in blocks:
                connection.execute(
                    """INSERT INTO blocks(
                           id, legacy_id, source_edition_id, chapter_id, chapter_title, chapter_index,
                           block_index, global_index, block_type, source_text, source_hash,
                           token_count, status, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           legacy_id=excluded.legacy_id,
                           chapter_title=excluded.chapter_title,
                           chapter_index=excluded.chapter_index,
                           block_index=excluded.block_index,
                           global_index=excluded.global_index,
                           block_type=excluded.block_type,
                           source_text=excluded.source_text,
                           source_hash=excluded.source_hash,
                           token_count=excluded.token_count,
                           updated_at=excluded.updated_at""",
                    (
                        item["id"], item.get("legacy_id", item["id"]), source_edition_id, item["chapter_id"],
                        item["chapter_title"], item["chapter_index"], item["block_index"],
                        item["global_index"], item["block_type"], item["source_text"],
                        item["source_hash"], item.get("token_count", 0),
                        item.get("status", V4BlockStatus.PENDING.value), utc_now(),
                    ),
                )

    @staticmethod
    def _row_to_block(row: sqlite3.Row) -> V4Block:
        return V4Block(
            id=row["id"], source_edition_id=row["source_edition_id"],
            chapter_id=row["chapter_id"], chapter_index=row["chapter_index"],
            block_index=row["block_index"], global_index=row["global_index"],
            block_type=row["block_type"], source_text=row["source_text"],
            source_hash=row["source_hash"], token_count=row["token_count"],
            status=row["status"], legacy_id=row["legacy_id"],
        )

    def list_blocks(self, statuses: Optional[Sequence[str]] = None) -> List[V4Block]:
        query = "SELECT * FROM blocks WHERE source_edition_id=(SELECT id FROM source_editions WHERE active=1)"
        params: List[Any] = []
        if statuses:
            query += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            params.extend(statuses)
        query += " ORDER BY global_index"
        with closing(self.connect()) as connection:
            return [self._row_to_block(row) for row in connection.execute(query, params)]

    def set_block_status(self, block_id: str, status: str, error: Optional[str] = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE blocks SET status=?, last_error=?, updated_at=? WHERE id=?",
                (status, error, utc_now(), block_id),
            )

    def start_run(
        self,
        run_id: str,
        stage: str,
        config: Dict[str, Any],
        knowledge_version: Optional[int] = None,
    ) -> None:
        with self.transaction() as connection:
            version = (
                int(knowledge_version)
                if knowledge_version is not None
                else int(
                    connection.execute(
                        "SELECT MAX(id) FROM knowledge_versions"
                    ).fetchone()[0]
                )
            )
            if connection.execute(
                "SELECT 1 FROM knowledge_versions WHERE id=?", (version,)
            ).fetchone() is None:
                raise ValueError(f"unknown knowledge version: {version}")
            connection.execute(
                """INSERT INTO runs(id, stage, status, knowledge_version, config_json, started_at)
                   VALUES(?, ?, 'running', ?, ?, ?)""",
                (run_id, stage, version, json.dumps(config, ensure_ascii=False), utc_now()),
            )

    def finish_run(self, run_id: str, status: str, error: Optional[str] = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE runs SET status=?, finished_at=?, error=? WHERE id=?",
                (status, utc_now(), error, run_id),
            )

    def fail_run_for_invalid_snapshot(
        self,
        run_id: str,
        stage: str,
        config: Dict[str, Any],
        error: KnowledgeSnapshotError,
    ) -> None:
        """Fail safely and create one blocking review for corrupt persisted knowledge."""

        with self.transaction() as connection:
            version = int(
                connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0]
            )
            connection.execute(
                """INSERT OR IGNORE INTO runs(
                       id, stage, status, knowledge_version, config_json,
                       started_at, finished_at, error
                   ) VALUES(?, ?, 'failed', ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    stage,
                    version,
                    json.dumps(config, ensure_ascii=False),
                    utc_now(),
                    utc_now(),
                    str(error),
                ),
            )
            connection.execute(
                "UPDATE runs SET status='failed', finished_at=?, error=? WHERE id=?",
                (utc_now(), str(error), run_id),
            )
            rows = connection.execute(
                """SELECT id, block_id FROM translation_versions
                   WHERE run_id=? AND pipeline='parallel_v4' AND active=1""",
                (run_id,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE translation_versions SET status=? WHERE id=?",
                    (V4BlockStatus.NEEDS_REVALIDATE.value, row["id"]),
                )
                connection.execute(
                    "UPDATE blocks SET status=?, updated_at=? WHERE id=?",
                    (
                        V4BlockStatus.NEEDS_REVALIDATE.value,
                        utc_now(),
                        row["block_id"],
                    ),
                )
            exists = connection.execute(
                """SELECT 1 FROM human_queue
                   WHERE kind='knowledge_snapshot_invalid' AND status='open'
                     AND json_extract(payload_json, '$.rule_id')=?""",
                (error.rule_id,),
            ).fetchone()
            if exists is None:
                connection.execute(
                    """INSERT INTO human_queue(
                           block_id, kind, severity, payload_json, created_at
                       ) VALUES(NULL, 'knowledge_snapshot_invalid', 'blocking', ?, ?)""",
                    (
                        json.dumps(
                            {
                                "rule_id": error.rule_id,
                                "detail": error.detail,
                                "run_id": run_id,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        utc_now(),
                    ),
                )

    def record_audit_call(
        self,
        run_id: str,
        block_id: Optional[str],
        purpose: str,
        model: str,
        knowledge_version: int,
        request: Dict[str, Any],
        raw_response: str,
        parsed: Optional[Dict[str, Any]],
        accepted: bool,
        attempts: int,
        elapsed_ms: int,
        error: Optional[str],
        connection: Optional[sqlite3.Connection] = None,
    ) -> int:
        if connection is None:
            with self.transaction() as owned_connection:
                return self.record_audit_call(
                    run_id=run_id,
                    block_id=block_id,
                    purpose=purpose,
                    model=model,
                    knowledge_version=knowledge_version,
                    request=request,
                    raw_response=raw_response,
                    parsed=parsed,
                    accepted=accepted,
                    attempts=attempts,
                    elapsed_ms=elapsed_ms,
                    error=error,
                    connection=owned_connection,
                )
        assert connection is not None
        archive_eligible = (
            accepted
            and not error
            and not self._is_human_audit(purpose, model, request, parsed)
        )
        locator: Optional[AuditLocator] = None
        request_json = json.dumps(request, ensure_ascii=False)
        stored_response = raw_response
        parsed_json = (
            json.dumps(parsed, ensure_ascii=False) if parsed is not None else None
        )
        if archive_eligible:
            locator = self._audit_transaction_for(connection).append(
                run_id,
                {
                    "request": request,
                    "raw_response": raw_response,
                    "parsed": parsed,
                },
                stage=purpose,
            )
            request_json = "{}"
            stored_response = ""
            parsed_json = None
        cursor = connection.execute(
            """INSERT INTO audit_calls(
                   run_id, block_id, purpose, model, knowledge_version, request_json,
                   raw_response, parsed_json, accepted, attempts, elapsed_ms, error,
                   archive_relative_path, archive_offset, archive_compressed_length,
                   archive_sha256, created_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, block_id, purpose, model, knowledge_version,
                request_json, stored_response, parsed_json,
                int(accepted), attempts, elapsed_ms, error,
                locator.relative_path if locator else None,
                locator.offset if locator else None,
                locator.compressed_length if locator else None,
                locator.sha256 if locator else None,
                utc_now(),
            ),
        )
        return int(cursor.lastrowid)

    def read_audit_payload(self, audit_call_id: int) -> Dict[str, Any]:
        """Return one full archived or inline audit payload by SQL identity."""
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM audit_calls WHERE id=?", (int(audit_call_id),)
            ).fetchone()
        if row is None:
            raise KeyError(f"audit call does not exist: {audit_call_id}")
        if row["archive_relative_path"]:
            return self.audit_archive.read(
                AuditLocator(
                    relative_path=str(row["archive_relative_path"]),
                    offset=int(row["archive_offset"]),
                    compressed_length=int(row["archive_compressed_length"]),
                    sha256=str(row["archive_sha256"]),
                )
            )
        return {
            "request": json.loads(row["request_json"]),
            "raw_response": str(row["raw_response"]),
            "parsed": (
                json.loads(row["parsed_json"])
                if row["parsed_json"] is not None
                else None
            ),
        }

    @staticmethod
    def _is_human_audit(
        purpose: str,
        model: str,
        request: Dict[str, Any],
        parsed: Optional[Dict[str, Any]],
    ) -> bool:
        values = [
            purpose,
            model,
            request.get("provider"),
            request.get("call_type"),
            request.get("actor_type"),
            (parsed or {}).get("provider"),
            (parsed or {}).get("call_type"),
            (parsed or {}).get("actor_type"),
        ]
        for value in values:
            normalized = str(value or "").strip().casefold()
            tokens = set(re.findall(r"[a-z0-9]+", normalized))
            if {"human", "manual"} & tokens:
                return True
        return False

    @classmethod
    def _audit_row_is_human(cls, row: sqlite3.Row) -> bool:
        """Classify persisted audits with the same policy used at write time."""

        try:
            request = json.loads(row["request_json"] or "{}")
            parsed = (
                json.loads(row["parsed_json"])
                if row["parsed_json"] is not None
                else None
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            # A destructive reset must preserve an audit it cannot classify.
            return True
        if not isinstance(request, dict) or (
            parsed is not None and not isinstance(parsed, dict)
        ):
            return True
        return cls._is_human_audit(
            str(row["purpose"] or ""),
            str(row["model"] or ""),
            request,
            parsed,
        )

    @staticmethod
    def _audit_fields(
        mode: str,
        request: Dict[str, Any],
        raw_response: str,
        parsed: Optional[Dict[str, Any]],
    ) -> tuple[Dict[str, Any], str, Optional[Dict[str, Any]]]:
        if mode not in {"full", "response", "minimal"}:
            raise ValueError("audit_mode 必须是 full、response 或 minimal")
        return request, raw_response, parsed

    def commit_scan_batch(
        self,
        run_id: str,
        outcomes: Sequence[ScanOutcome],
        model: str,
        audit_mode: str = "full",
    ) -> int:
        """Commit worker results in deterministic block order and create one version."""
        ordered = sorted(outcomes, key=lambda item: item.block.global_index)
        with self.transaction() as connection:
            version = (
                self.create_knowledge_version(f"scan batch {run_id}", connection)
                if any(outcome.response is not None for outcome in ordered)
                else int(connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0])
            )
            for outcome in ordered:
                for candidate in outcome.lexical_candidates:
                    connection.execute(
                        """INSERT INTO lexical_candidates(
                               id, block_id, paragraph_id, start_offset, end_offset,
                               original_text, normalized_text, left_context, right_context,
                               extraction_reason, book_frequency, risk_flags_json,
                               model_status, resolution_status, selected, run_id,
                               created_at, updated_at
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(id) DO UPDATE SET
                               paragraph_id=excluded.paragraph_id,
                               start_offset=excluded.start_offset,
                               end_offset=excluded.end_offset,
                               original_text=excluded.original_text,
                               normalized_text=excluded.normalized_text,
                               left_context=excluded.left_context,
                               right_context=excluded.right_context,
                               extraction_reason=excluded.extraction_reason,
                               book_frequency=excluded.book_frequency,
                               risk_flags_json=excluded.risk_flags_json,
                               model_status=CASE
                                   WHEN lexical_candidates.resolution_status='pending'
                                   THEN excluded.model_status
                                   ELSE lexical_candidates.model_status END,
                               resolution_status=CASE
                                   WHEN lexical_candidates.resolution_status='pending'
                                   THEN excluded.resolution_status
                                   ELSE lexical_candidates.resolution_status END,
                               selected=CASE
                                   WHEN lexical_candidates.resolution_status='pending'
                                   THEN excluded.selected
                                   ELSE lexical_candidates.selected END,
                               run_id=excluded.run_id,
                               updated_at=excluded.updated_at""",
                        (
                            candidate["id"], outcome.block.id, candidate["paragraph_id"],
                            int(candidate["start_offset"]), int(candidate["end_offset"]),
                            candidate["original_text"], candidate["normalized_text"],
                            candidate.get("left_context", ""),
                            candidate.get("right_context", ""),
                            candidate["extraction_reason"],
                            int(candidate.get("book_frequency", 1)),
                            (
                                candidate.get("risk_flags_json")
                                if isinstance(candidate.get("risk_flags_json"), str)
                                else json.dumps(
                                    candidate.get("risk_flags", []),
                                    ensure_ascii=False,
                                    sort_keys=True,
                                )
                            ),
                            candidate.get("model_status", "pending"),
                            candidate.get("resolution_status", "pending"),
                            int(bool(candidate.get("selected"))),
                            run_id, utc_now(), utc_now(),
                        ),
                    )
                parsed = outcome.response.model_dump(mode="json") if outcome.response else None
                calls = outcome.audit_calls
                if not calls and outcome.attempts:
                    calls = [
                        {
                            "request": outcome.request_payload,
                            "raw_response": outcome.raw_response,
                            "parsed": parsed,
                            "accepted": outcome.response is not None,
                            "attempts": outcome.attempts,
                            "elapsed_ms": outcome.elapsed_ms,
                            "error": outcome.error,
                        }
                    ]
                for call in calls:
                    audit_request, audit_raw, audit_parsed = self._audit_fields(
                        audit_mode,
                        dict(call.get("request") or {}),
                        str(call.get("raw_response") or ""),
                        call.get("parsed"),
                    )
                    self.record_audit_call(
                        run_id, outcome.block.id, "scan", model, version,
                        audit_request, audit_raw, audit_parsed,
                        bool(call.get("accepted")),
                        int(call.get("attempts") or 1), int(call.get("elapsed_ms") or 0),
                        call.get("error"), connection,
                    )
                if not outcome.response:
                    connection.execute(
                        "UPDATE blocks SET status=?, last_error=?, updated_at=? WHERE id=?",
                        (V4BlockStatus.FAILED_RETRYABLE.value, outcome.error, utc_now(), outcome.block.id),
                    )
                    continue
                for mention in outcome.response.mentions:
                    payload = mention.model_dump(mode="json")
                    cursor = connection.execute(
                        """INSERT INTO evidence(
                               block_id, paragraph_id, kind, source_form, evidence_quote,
                               payload_json, confidence, extractor, run_id, created_at
                           ) VALUES(?, ?, 'mention', ?, ?, ?, ?, 'scan_v4_2', ?, ?)""",
                        (
                            outcome.block.id, mention.paragraph_id, mention.source_form,
                            mention.evidence_quote, json.dumps(payload, ensure_ascii=False),
                            mention.confidence, run_id, utc_now(),
                        ),
                    )
                    evidence_id = int(cursor.lastrowid)
                    normalized_form = normalize_english_form(
                        mention.canonical_form or mention.source_form
                    )
                    lexeme_id = self._ensure_schema8_lexeme(
                        connection,
                        mention.canonical_form or mention.source_form,
                        normalized_form=normalized_form,
                        knowledge_version=version,
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO mentions(
                               block_id, paragraph_id, source_form, normalized_form,
                               discourse_function, lexeme_id, evidence_id
                           ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                        (
                            outcome.block.id, mention.paragraph_id, mention.source_form,
                            normalized_form, mention.discourse_function, lexeme_id,
                            evidence_id,
                        ),
                    )
                for ambiguity in outcome.response.ambiguities:
                    cursor = connection.execute(
                        """INSERT INTO evidence(
                               block_id, paragraph_id, kind, evidence_quote, payload_json,
                               confidence, extractor, run_id, created_at
                           ) VALUES(?, ?, 'ambiguity', ?, ?, ?, 'scan_v4', ?, ?)""",
                        (
                            outcome.block.id, ambiguity.paragraph_id, ambiguity.evidence_quote,
                            json.dumps(ambiguity.model_dump(mode="json"), ensure_ascii=False),
                            ambiguity.confidence, run_id, utc_now(),
                        ),
                    )
                    evidence_id = int(cursor.lastrowid)
                    claim_id = stable_id(
                        "claim",
                        (
                            f"translation_constraint:{outcome.block.id}:"
                            f"{ambiguity.paragraph_id}:{ambiguity.constraint}"
                        ),
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO claims(
                               id, kind, statement, subject_form, status, scope,
                               confidence, reveal_global_index, high_impact, locked,
                               created_version, created_at
                           ) VALUES(?, 'translation_constraint', ?, ?, 'proposed',
                                    'occurrence', ?, ?, 0, 0, ?, ?)""",
                        (
                            claim_id,
                            ambiguity.constraint,
                            ambiguity.evidence_quote,
                            ambiguity.confidence,
                            outcome.block.global_index,
                            version,
                            utc_now(),
                        ),
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO claim_evidence(claim_id, evidence_id)
                           VALUES(?, ?)""",
                        (claim_id, evidence_id),
                    )
                    verification_payload = {
                        "kind": "translation_constraint",
                        "statement": ambiguity.constraint,
                        "subject_form": ambiguity.evidence_quote,
                        "reveal_global_index": outcome.block.global_index,
                        "evidence": [
                            {
                                "block": outcome.block.id,
                                "paragraph_id": ambiguity.paragraph_id,
                                "evidence_quote": ambiguity.evidence_quote,
                            }
                        ],
                    }
                    task_id = stable_id("verify", f"claim:{claim_id}", length=24)
                    connection.execute(
                        """INSERT OR IGNORE INTO verification_tasks(
                               id, subject_type, subject_id, payload_json, status,
                               required_votes, created_at
                           ) VALUES(?, 'claim', ?, ?, 'open', 2, ?)""",
                        (
                            task_id,
                            claim_id,
                            json.dumps(
                                verification_payload, ensure_ascii=False, sort_keys=True
                            ),
                            utc_now(),
                        ),
                    )
                connection.execute(
                    "UPDATE blocks SET status=?, last_error=NULL, updated_at=? WHERE id=?",
                    (V4BlockStatus.SCANNED.value, utc_now(), outcome.block.id),
                )
            return version

    def commit_candidate_index_batch(
        self, run_id: str, outcomes: Sequence[ScanOutcome]
    ) -> Dict[str, int]:
        """Atomically persist deterministic candidates and block scan states.

        This deliberately creates neither a knowledge version nor model audit rows:
        candidate indexing is local evidence collection, not a knowledge decision.
        """
        ordered = sorted(outcomes, key=lambda item: item.block.global_index)
        indexed = failed = lexical_count = 0
        with self.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM runs WHERE id=?", (run_id,)
            ).fetchone() is None:
                raise ValueError(f"unknown run: {run_id}")
            for outcome in ordered:
                if outcome.error is not None or outcome.response is None:
                    connection.execute(
                        """UPDATE blocks
                           SET status=CASE
                                   WHEN status IN (?, ?) THEN ? ELSE status END,
                               last_error=CASE
                                   WHEN status IN (?, ?) THEN ? ELSE last_error END,
                               updated_at=CASE
                                   WHEN status IN (?, ?) THEN ? ELSE updated_at END
                           WHERE id=?""",
                        (
                            V4BlockStatus.PENDING.value,
                            V4BlockStatus.FAILED_RETRYABLE.value,
                            V4BlockStatus.FAILED_RETRYABLE.value,
                            V4BlockStatus.PENDING.value,
                            V4BlockStatus.FAILED_RETRYABLE.value,
                            outcome.error or "candidate indexing failed",
                            V4BlockStatus.PENDING.value,
                            V4BlockStatus.FAILED_RETRYABLE.value,
                            utc_now(),
                            outcome.block.id,
                        ),
                    )
                    failed += 1
                    continue
                indexed_candidates = [
                    dict(candidate) for candidate in outcome.lexical_candidates
                ]
                incoming_ids = sorted(
                    {str(candidate["id"]) for candidate in indexed_candidates}
                )
                existing_by_id: Dict[str, sqlite3.Row] = {}
                if incoming_ids:
                    placeholders = ",".join("?" for _ in incoming_ids)
                    existing_by_id = {
                        row["id"]: row
                        for row in connection.execute(
                            f"""SELECT lc.id, lc.block_id, lc.paragraph_id,
                                       lc.start_offset, lc.end_offset,
                                       lc.original_text, lc.normalized_text,
                                       MAX(CASE WHEN cc.state='leased' THEN 1 ELSE 0 END)
                                           AS leased
                                FROM lexical_candidates lc
                                LEFT JOIN candidate_cluster_members cm
                                  ON cm.candidate_id=lc.id
                                LEFT JOIN candidate_clusters cc ON cc.id=cm.cluster_id
                                WHERE lc.id IN ({placeholders})
                                GROUP BY lc.id""",
                            incoming_ids,
                        ).fetchall()
                    }
                for candidate in indexed_candidates:
                    base_id = str(candidate["id"])
                    existing = existing_by_id.get(base_id)
                    if existing is None or not bool(existing["leased"]):
                        continue
                    identity = (
                        outcome.block.id,
                        str(candidate["paragraph_id"]),
                        int(candidate["start_offset"]),
                        int(candidate["end_offset"]),
                        str(candidate["original_text"]),
                        str(candidate["normalized_text"]),
                    )
                    existing_identity = (
                        existing["block_id"],
                        existing["paragraph_id"],
                        int(existing["start_offset"]),
                        int(existing["end_offset"]),
                        existing["original_text"],
                        existing["normalized_text"],
                    )
                    if identity != existing_identity:
                        encoded_identity = json.dumps(
                            [base_id, *identity],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        candidate["id"] = "cand_" + hashlib.sha256(
                            encoded_identity.encode("utf-8")
                        ).hexdigest()[:20]
                current_candidate_ids = {
                    str(candidate["id"]) for candidate in indexed_candidates
                }
                self._remove_stale_pending_candidates(
                    connection, outcome.block.id, current_candidate_ids
                )
                for candidate in indexed_candidates:
                    risk_flags = candidate.get("risk_flags_json")
                    if not isinstance(risk_flags, str):
                        risk_flags = json.dumps(
                            candidate.get("risk_flags", []),
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    now = utc_now()
                    connection.execute(
                        """INSERT INTO lexical_candidates(
                               id, block_id, paragraph_id, start_offset, end_offset,
                               original_text, normalized_text, left_context, right_context,
                               extraction_reason, book_frequency, risk_flags_json,
                               model_status, resolution_status, selected, run_id,
                               created_at, updated_at
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending',
                                    'pending', 0, ?, ?, ?)
                           ON CONFLICT(id) DO UPDATE SET
                               paragraph_id=excluded.paragraph_id,
                               start_offset=excluded.start_offset,
                               end_offset=excluded.end_offset,
                               original_text=excluded.original_text,
                               normalized_text=excluded.normalized_text,
                               left_context=excluded.left_context,
                               right_context=excluded.right_context,
                               extraction_reason=excluded.extraction_reason,
                               book_frequency=excluded.book_frequency,
                               risk_flags_json=excluded.risk_flags_json,
                               model_status=CASE
                                   WHEN lexical_candidates.resolution_status='pending'
                                   THEN 'pending' ELSE lexical_candidates.model_status END,
                               selected=CASE
                                   WHEN lexical_candidates.resolution_status='pending'
                                   THEN 0 ELSE lexical_candidates.selected END,
                                run_id=CASE
                                    WHEN lexical_candidates.resolution_status='pending'
                                    THEN excluded.run_id ELSE lexical_candidates.run_id END,
                                updated_at=CASE
                                    WHEN lexical_candidates.paragraph_id=excluded.paragraph_id
                                     AND lexical_candidates.start_offset=excluded.start_offset
                                     AND lexical_candidates.end_offset=excluded.end_offset
                                     AND lexical_candidates.original_text=excluded.original_text
                                     AND lexical_candidates.normalized_text=excluded.normalized_text
                                     AND lexical_candidates.left_context=excluded.left_context
                                     AND lexical_candidates.right_context=excluded.right_context
                                     AND lexical_candidates.risk_flags_json=excluded.risk_flags_json
                                    THEN lexical_candidates.updated_at
                                    ELSE excluded.updated_at END""",
                        (
                            candidate["id"],
                            outcome.block.id,
                            candidate["paragraph_id"],
                            int(candidate["start_offset"]),
                            int(candidate["end_offset"]),
                            candidate["original_text"],
                            candidate["normalized_text"],
                            candidate.get("left_context", ""),
                            candidate.get("right_context", ""),
                            candidate["extraction_reason"],
                            int(candidate.get("book_frequency", 1)),
                            risk_flags,
                            run_id,
                            now,
                            now,
                        ),
                    )
                    lexical_count += 1
                is_front_matter = bool(
                    outcome.request_payload.get("front_matter", False)
                )
                status = (
                    V4BlockStatus.READY.value
                    if is_front_matter
                    else V4BlockStatus.SCANNED.value
                )
                connection.execute(
                    """UPDATE blocks
                       SET status=CASE
                               WHEN status IN (?, ?) THEN ? ELSE status END,
                           last_error=CASE
                               WHEN status IN (?, ?) THEN NULL ELSE last_error END,
                           updated_at=CASE
                               WHEN status IN (?, ?) THEN ? ELSE updated_at END
                       WHERE id=?""",
                    (
                        V4BlockStatus.PENDING.value,
                        V4BlockStatus.FAILED_RETRYABLE.value,
                        status,
                        V4BlockStatus.PENDING.value,
                        V4BlockStatus.FAILED_RETRYABLE.value,
                        V4BlockStatus.PENDING.value,
                        V4BlockStatus.FAILED_RETRYABLE.value,
                        utc_now(),
                        outcome.block.id,
                    ),
                )
                indexed += 1
        return {"indexed": indexed, "failed": failed, "candidates": lexical_count}

    def advance_prepared_blocks(
        self, block_ids: Optional[Sequence[str]] = None
    ) -> Dict[str, int]:
        """Move only provably complete scanned blocks into the translation queue.

        A supplied scope is the successful block set from one local scan run.
        Standalone target resolution may omit it to reconsider all still-scanned
        blocks, but every block is checked against the same persisted invariants.
        """

        scoped_ids = tuple(sorted({str(block_id) for block_id in block_ids or ()}))
        if block_ids is not None and not scoped_ids:
            return {"ready": 0, "blocked": 0}
        with self.transaction() as connection:
            scope_join = ""
            if block_ids is not None:
                connection.execute(
                    "CREATE TEMP TABLE preparation_scope(block_id TEXT PRIMARY KEY)"
                )
                connection.executemany(
                    "INSERT INTO preparation_scope(block_id) VALUES(?)",
                    ((block_id,) for block_id in scoped_ids),
                )
                scope_join = "JOIN preparation_scope ps ON ps.block_id=b.id"

            scanned_rows = connection.execute(
                f"""SELECT b.id
                    FROM blocks b
                    JOIN source_editions se
                      ON se.id=b.source_edition_id AND se.active=1
                    {scope_join}
                    WHERE b.status=?
                    ORDER BY b.global_index, b.id""",
                (V4BlockStatus.SCANNED.value,),
            ).fetchall()
            scanned_ids = [str(row["id"]) for row in scanned_rows]
            if not scanned_ids:
                return {"ready": 0, "blocked": 0}

            ready_rows = connection.execute(
                f"""SELECT b.id
                    FROM blocks b
                    JOIN source_editions se
                      ON se.id=b.source_edition_id AND se.active=1
                    {scope_join}
                    WHERE b.status=?
                      AND b.last_error IS NULL
                      AND NOT EXISTS(
                          SELECT 1 FROM lexical_candidates lc
                          WHERE lc.block_id=b.id
                            AND lc.resolution_status NOT IN (
                                'promoted', 'rejected', 'superseded', 'deferred'
                            )
                      )
                      AND NOT EXISTS(
                          SELECT 1
                          FROM lexical_candidates lc
                          JOIN candidate_cluster_members cm
                            ON cm.candidate_id=lc.id
                          JOIN candidate_clusters cc ON cc.id=cm.cluster_id
                          WHERE lc.block_id=b.id
                            AND cc.state IN ('pending', 'leased', 'stale')
                      )
                      AND NOT EXISTS(
                          SELECT 1
                          FROM lexical_candidates lc
                          JOIN candidate_cluster_members cm
                            ON cm.candidate_id=lc.id
                          JOIN candidate_adjudications ca
                            ON ca.cluster_id=cm.cluster_id AND ca.active=1
                          WHERE lc.block_id=b.id
                            AND ca.reason='model_protocol_failure'
                      )
                      AND NOT EXISTS(
                          SELECT 1
                          FROM lexical_candidates lc
                          JOIN candidate_resolutions cr
                            ON cr.candidate_id=lc.id
                          JOIN candidate_adjudications ca
                            ON ca.id=cr.adjudication_id AND ca.active=1
                          JOIN human_queue hq
                            ON hq.kind='working_target_required'
                           AND hq.status='open'
                           AND json_extract(hq.payload_json, '$.concept_id')=
                               cr.concept_id
                          WHERE lc.block_id=b.id
                            AND cr.concept_id IS NOT NULL
                      )
                      AND NOT EXISTS(
                          SELECT 1
                          FROM mentions m
                          JOIN human_queue hq
                            ON hq.kind='working_target_required'
                           AND hq.status='open'
                           AND json_extract(hq.payload_json, '$.concept_id')=
                               m.concept_id
                          WHERE m.block_id=b.id AND m.concept_id IS NOT NULL
                      )
                      AND NOT EXISTS(
                          SELECT 1 FROM human_queue hq
                          WHERE hq.block_id=b.id AND hq.status='open'
                            AND hq.severity='blocking'
                      )
                    ORDER BY b.global_index, b.id""",
                (V4BlockStatus.SCANNED.value,),
            ).fetchall()
            ready_ids = [str(row["id"]) for row in ready_rows]
            now = utc_now()
            connection.executemany(
                """UPDATE blocks SET status=?, last_error=NULL, updated_at=?
                   WHERE id=? AND status=?""",
                (
                    (
                        V4BlockStatus.READY.value,
                        now,
                        block_id,
                        V4BlockStatus.SCANNED.value,
                    )
                    for block_id in ready_ids
                ),
            )
            return {
                "ready": len(ready_ids),
                "blocked": len(scanned_ids) - len(ready_ids),
            }

    @staticmethod
    def _remove_stale_pending_candidates(
        connection: sqlite3.Connection,
        block_id: str,
        current_candidate_ids: set[str],
    ) -> None:
        params: List[Any] = [block_id]
        current_clause = ""
        if current_candidate_ids:
            placeholders = ",".join("?" for _ in current_candidate_ids)
            current_clause = f" AND lc.id NOT IN ({placeholders})"
            params.extend(sorted(current_candidate_ids))
        rows = connection.execute(
            """SELECT lc.id
               FROM lexical_candidates lc
               WHERE lc.block_id=? AND lc.resolution_status='pending'"""
            + current_clause
            + """
                 AND NOT EXISTS(
                     SELECT 1 FROM candidate_resolutions cr
                     WHERE cr.candidate_id=lc.id
                 )
                 AND NOT EXISTS(
                     SELECT 1
                     FROM candidate_cluster_members cm
                     JOIN candidate_clusters cc ON cc.id=cm.cluster_id
                     WHERE cm.candidate_id=lc.id
                       AND (
                           cc.state!='pending'
                           OR EXISTS(
                               SELECT 1 FROM candidate_adjudications ca
                               WHERE ca.cluster_id=cc.id
                           )
                       )
                 )
               ORDER BY lc.id""",
            params,
        ).fetchall()
        stale_ids = [row["id"] for row in rows]
        if not stale_ids:
            return
        placeholders = ",".join("?" for _ in stale_ids)
        connection.execute(
            f"""DELETE FROM candidate_clusters
                WHERE state='pending'
                  AND NOT EXISTS(
                      SELECT 1 FROM candidate_adjudications ca
                      WHERE ca.cluster_id=candidate_clusters.id
                  )
                  AND id IN (
                      SELECT cluster_id FROM candidate_cluster_members
                      WHERE candidate_id IN ({placeholders})
                  )""",
            stale_ids,
        )
        connection.execute(
            f"DELETE FROM lexical_candidates WHERE id IN ({placeholders})",
            stale_ids,
        )

    @staticmethod
    def _safe_string_tuple_json(value: Any) -> tuple[str, ...]:
        """Parse bounded string-list JSON without trusting malformed storage."""
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError):
            return ()
        if not isinstance(parsed, list) or any(
            not isinstance(item, str) for item in parsed
        ):
            return ()
        return tuple(sorted(set(parsed)))

    @classmethod
    def _row_to_lexical_candidate(cls, row: sqlite3.Row) -> Any:
        from .lexical_index import LexicalCandidate

        return LexicalCandidate(
            id=row["id"],
            block_id=row["block_id"],
            paragraph_id=row["paragraph_id"],
            start_offset=int(row["start_offset"]),
            end_offset=int(row["end_offset"]),
            original_text=row["original_text"],
            normalized_text=row["normalized_text"],
            left_context=row["left_context"] or "",
            right_context=row["right_context"] or "",
            extraction_reason=row["extraction_reason"],
            book_frequency=int(row["book_frequency"] or 1),
            score=0,
            risk_flags=cls._safe_string_tuple_json(row["risk_flags_json"]),
        )

    @staticmethod
    def _pending_lexical_rows(
        connection: sqlite3.Connection,
    ) -> List[sqlite3.Row]:
        return connection.execute(
            """SELECT lc.* FROM lexical_candidates lc
               JOIN blocks b ON b.id=lc.block_id
               WHERE lc.resolution_status='pending'
                 AND b.source_edition_id=(
                     SELECT id FROM source_editions WHERE active=1
                 )
                 AND NOT EXISTS (
                     SELECT 1
                     FROM candidate_cluster_members cm
                     JOIN candidate_adjudications ca
                       ON ca.cluster_id=cm.cluster_id
                     WHERE cm.candidate_id=lc.id AND ca.active=1
                 )
                 AND substr(
                       b.source_text,
                       lc.start_offset + 1,
                       lc.end_offset - lc.start_offset
                     )=lc.original_text
               ORDER BY b.global_index, lc.start_offset, lc.end_offset, lc.id"""
        ).fetchall()

    @staticmethod
    def _candidate_snapshot_token(rows: Sequence[sqlite3.Row]) -> str:
        payload = [
            [row["id"], row["updated_at"], row["resolution_status"]]
            for row in rows
        ]
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def load_pending_candidate_snapshot(self) -> tuple[str, List[Any]]:
        """Read candidates and their CAS token from one SQLite snapshot."""
        with closing(self.connect()) as connection:
            rows = self._pending_lexical_rows(connection)
        return (
            self._candidate_snapshot_token(rows),
            [self._row_to_lexical_candidate(row) for row in rows],
        )

    def load_pending_lexical_candidates(self) -> List[Any]:
        """Return every unresolved, unclaimed local candidate."""
        return self.load_pending_candidate_snapshot()[1]

    @staticmethod
    def _candidate_cluster_members(cluster: Any) -> List[Dict[str, Any]]:
        from .candidate_clusters import CandidateClusterBuilder

        members: Dict[str, Dict[str, Any]] = {}
        alternatives_by_key = {
            CandidateClusterBuilder._alternative_equivalence_key(alternative): alternative.id
            for alternative in cluster.alternatives
        }
        for candidate in tuple(getattr(cluster, "members", ()) or ()):
            representative_id = alternatives_by_key.get(
                CandidateClusterBuilder._alternative_equivalence_key(candidate)
            )
            members[candidate.id] = {
                "candidate_id": candidate.id,
                "role": "support",
                "block_id": candidate.block_id,
                "context": {
                    "representative_candidate_id": representative_id,
                    "support_member": True,
                },
            }
        for alternative in cluster.alternatives:
            existing = members.get(alternative.id)
            members[alternative.id] = {
                "candidate_id": alternative.id,
                "role": "alternative",
                "block_id": alternative.block_id,
                "context": {
                    **(existing["context"] if existing else {}),
                    "representative_candidate_id": alternative.id,
                    "support_member": True,
                },
            }
        for context in cluster.contexts:
            existing = members.get(context.candidate_id)
            existing_context = dict(existing["context"]) if existing else {}
            payload = {
                "candidate_id": context.candidate_id,
                "role": (
                    "both"
                    if existing and existing["role"] == "alternative"
                    else "context"
                ),
                "block_id": context.block_id,
                "context": {
                    **existing_context,
                    "block_id": context.block_id,
                    "paragraph_id": context.paragraph_id,
                    "original_text": context.original_text,
                    "left_context": context.left_context,
                    "right_context": context.right_context,
                    "risk_flags": list(context.risk_flags),
                },
            }
            members[context.candidate_id] = payload
        return [members[candidate_id] for candidate_id in sorted(members)]

    def persist_candidate_clusters(
        self, run_id: str, clusters: Sequence[Any]
    ) -> Dict[str, int]:
        """Persist bounded cluster decisions without removing source occurrences."""
        with self.transaction() as connection:
            return self._persist_candidate_clusters(connection, run_id, clusters)

    def _persist_candidate_clusters(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        clusters: Sequence[Any],
    ) -> Dict[str, int]:
        ordered = sorted(clusters, key=lambda cluster: cluster.id)
        if len({cluster.id for cluster in ordered}) != len(ordered):
            raise ValueError("duplicate candidate cluster id")
        member_count = 0
        if connection.execute(
            "SELECT 1 FROM runs WHERE id=?", (run_id,)
        ).fetchone() is None:
            raise ValueError(f"unknown run: {run_id}")
        for ordinal, cluster in enumerate(ordered):
            members = self._candidate_cluster_members(cluster)
            if not members:
                raise ValueError(f"candidate cluster has no members: {cluster.id}")
            candidate_ids = [member["candidate_id"] for member in members]
            placeholders = ",".join("?" for _ in candidate_ids)
            existing_ids = {
                row[0]
                for row in connection.execute(
                    f"SELECT id FROM lexical_candidates WHERE id IN ({placeholders})",
                    candidate_ids,
                ).fetchall()
            }
            missing = sorted(set(candidate_ids) - existing_ids)
            if missing:
                raise ValueError(
                    f"unknown lexical candidate(s) in cluster {cluster.id}: {', '.join(missing)}"
                )
            block_ids = sorted({member["block_id"] for member in members})
            now = utc_now()
            connection.execute(
                """INSERT INTO candidate_clusters(
                           run_id, id, risk_flags_json, affected_blocks_json,
                           affected_block_count, ordinal, created_at, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           run_id=excluded.run_id,
                           risk_flags_json=excluded.risk_flags_json,
                           affected_blocks_json=excluded.affected_blocks_json,
                           affected_block_count=excluded.affected_block_count,
                           ordinal=excluded.ordinal,
                           updated_at=excluded.updated_at""",
                (
                    run_id,
                    cluster.id,
                    json.dumps(
                        sorted(set(cluster.risk_flags)),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        block_ids,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    int(cluster.affected_blocks),
                    ordinal,
                    now,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM candidate_cluster_members WHERE cluster_id=?",
                (cluster.id,),
            )
            for member_ordinal, member in enumerate(members):
                connection.execute(
                    """INSERT INTO candidate_cluster_members(
                               run_id, cluster_id, candidate_id, role, ordinal,
                               context_json, created_at
                           ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        cluster.id,
                        member["candidate_id"],
                        member["role"],
                        member_ordinal,
                        json.dumps(
                            member["context"],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        now,
                    ),
                )
            member_count += len(members)
        return {"clusters": len(ordered), "members": member_count}

    def replace_pending_candidate_clusters(
        self,
        run_id: str,
        clusters: Sequence[Any],
        *,
        expected_snapshot_token: Optional[str] = None,
    ) -> Dict[str, int]:
        """Atomically rebuild undecided clusters while preserving all history."""
        with self.transaction() as connection:
            if expected_snapshot_token is not None:
                current_rows = self._pending_lexical_rows(connection)
                current_token = self._candidate_snapshot_token(current_rows)
                if current_token != expected_snapshot_token:
                    raise StaleCandidateSnapshot(
                        "pending candidate snapshot changed during clustering"
                    )
            stale_candidate_ids = [
                row["candidate_id"]
                for row in connection.execute(
                    """SELECT DISTINCT cm.candidate_id
                       FROM candidate_cluster_members cm
                       JOIN candidate_clusters cc ON cc.id=cm.cluster_id
                       WHERE cc.state='stale'
                         AND NOT EXISTS(
                             SELECT 1 FROM candidate_adjudications ca
                             WHERE ca.cluster_id=cc.id
                         )
                       ORDER BY cm.candidate_id"""
                ).fetchall()
            ]
            refreshed_clusters = []
            for cluster in clusters:
                proposed = cluster
                seen_ids = set()
                while True:
                    collision = connection.execute(
                        """SELECT state, lease_snapshot_hash
                           FROM candidate_clusters
                           WHERE id=? AND state IN ('leased', 'stale')""",
                        (proposed.id,),
                    ).fetchone()
                    if collision is None:
                        refreshed_clusters.append(proposed)
                        break
                    incoming_hash = self._incoming_candidate_cluster_snapshot(
                        connection, proposed
                    )
                    if (
                        collision["state"] == "leased"
                        and collision["lease_snapshot_hash"] == incoming_hash
                    ):
                        break
                    if proposed.id in seen_ids:
                        raise ValueError("candidate cluster refresh generation cycle")
                    seen_ids.add(proposed.id)
                    refreshed_id = stable_id(
                        "cluster",
                        f"refreshed:{proposed.id}:{incoming_hash}",
                        length=20,
                    )
                    proposed = replace(proposed, id=refreshed_id)
            connection.execute(
                """DELETE FROM candidate_clusters
                   WHERE state IN ('pending', 'stale')
                     AND NOT EXISTS (
                       SELECT 1 FROM candidate_adjudications ca
                       WHERE ca.cluster_id=candidate_clusters.id
                   )"""
            )
            latest_history: Dict[str, str] = {}
            for row in connection.execute(
                """SELECT cluster_id, id
                   FROM candidate_adjudications
                   ORDER BY cluster_id, created_at DESC, id DESC"""
            ).fetchall():
                latest_history.setdefault(row["cluster_id"], row["id"])
            safe_clusters = []
            for cluster in refreshed_clusters:
                reopened_id = cluster.id
                member_ids = sorted(
                    member["candidate_id"]
                    for member in self._candidate_cluster_members(cluster)
                )
                seen_ids = set()
                while reopened_id in latest_history:
                    if reopened_id in seen_ids:
                        raise ValueError("candidate cluster generation cycle")
                    seen_ids.add(reopened_id)
                    reopened_id = stable_id(
                        "cluster",
                        "reopened:"
                        + reopened_id
                        + ":"
                        + latest_history[reopened_id]
                        + ":"
                        + "|".join(member_ids),
                        length=20,
                    )
                safe_clusters.append(
                    cluster
                    if reopened_id == cluster.id
                    else replace(cluster, id=reopened_id)
                )
            summary = self._persist_candidate_clusters(
                connection, run_id, safe_clusters
            )
            if stale_candidate_ids:
                placeholders = ",".join("?" for _ in stale_candidate_ids)
                connection.execute(
                    f"""DELETE FROM lexical_candidates
                        WHERE id IN ({placeholders})
                          AND resolution_status='pending'
                          AND NOT EXISTS(
                              SELECT 1 FROM candidate_resolutions cr
                              WHERE cr.candidate_id=lexical_candidates.id
                          )
                          AND NOT EXISTS(
                              SELECT 1 FROM candidate_cluster_members cm
                              WHERE cm.candidate_id=lexical_candidates.id
                          )""",
                    stale_candidate_ids,
                )
            return summary

    def load_pending_candidate_clusters(
        self, max_clusters: Optional[int] = None
    ) -> List[Any]:
        """Bulk-rehydrate never-adjudicated pending clusters."""
        query = """SELECT cc.* FROM candidate_clusters cc
                   WHERE cc.state='pending'
                     AND NOT EXISTS (
                       SELECT 1 FROM candidate_adjudications ca
                       WHERE ca.cluster_id=cc.id
                   )
                     AND EXISTS (
                       SELECT 1
                       FROM candidate_cluster_members cm
                       JOIN lexical_candidates lc ON lc.id=cm.candidate_id
                       JOIN blocks b ON b.id=lc.block_id
                       WHERE cm.cluster_id=cc.id
                         AND cm.role IN ('alternative', 'both')
                         AND lc.resolution_status='pending'
                         AND b.source_edition_id=(
                             SELECT id FROM source_editions WHERE active=1
                         )
                   )
                   ORDER BY cc.id"""
        params: List[Any] = []
        if max_clusters is not None:
            query += " LIMIT ?"
            params.append(max(0, int(max_clusters)))
        with closing(self.connect()) as connection:
            cluster_rows = connection.execute(query, params).fetchall()
            return self._hydrate_candidate_clusters(connection, cluster_rows)

    def _hydrate_candidate_clusters(
        self,
        connection: sqlite3.Connection,
        cluster_rows: Sequence[sqlite3.Row],
    ) -> List[Any]:
        from .candidate_clusters import CandidateCluster, CandidateContext

        if not cluster_rows:
            return []
        cluster_ids = [row["id"] for row in cluster_rows]
        placeholders = ",".join("?" for _ in cluster_ids)
        member_rows = connection.execute(
            f"""SELECT cm.cluster_id, cm.role, cm.ordinal, cm.context_json, lc.*
                FROM candidate_cluster_members cm
                JOIN lexical_candidates lc ON lc.id=cm.candidate_id
                WHERE cm.cluster_id IN ({placeholders})
                ORDER BY cm.cluster_id, cm.ordinal, lc.id""",
            cluster_ids,
        ).fetchall()
        members_by_cluster: Dict[str, List[sqlite3.Row]] = {}
        for member in member_rows:
            members_by_cluster.setdefault(member["cluster_id"], []).append(member)
        clusters: List[Any] = []
        for cluster_row in cluster_rows:
            alternatives = []
            contexts = []
            complete_members = []
            for member in members_by_cluster.get(cluster_row["id"], []):
                candidate = self._row_to_lexical_candidate(member)
                role = member["role"]
                try:
                    context_data = json.loads(member["context_json"] or "{}")
                except (TypeError, ValueError):
                    context_data = {}
                if not isinstance(context_data, dict):
                    context_data = {}
                if role in {"alternative", "both"} or bool(
                    context_data.get("support_member")
                ):
                    complete_members.append(candidate)
                if (
                    role in {"alternative", "both"}
                    and member["resolution_status"] == "pending"
                ):
                    alternatives.append(candidate)
                if role not in {"context", "both"}:
                    continue

                def context_string(field: str, fallback: str) -> str:
                    value = context_data.get(field, fallback)
                    return value if isinstance(value, str) else fallback

                contexts.append(
                    CandidateContext(
                        candidate_id=candidate.id,
                        block_id=context_string("block_id", candidate.block_id),
                        paragraph_id=context_string(
                            "paragraph_id", candidate.paragraph_id
                        ),
                        original_text=context_string(
                            "original_text", candidate.original_text
                        ),
                        left_context=context_string(
                            "left_context", candidate.left_context
                        ),
                        right_context=context_string(
                            "right_context", candidate.right_context
                        ),
                        risk_flags=self._safe_string_tuple_json(
                            context_data.get("risk_flags", [])
                        ),
                    )
                )
            if not alternatives:
                continue
            clusters.append(
                CandidateCluster(
                    id=cluster_row["id"],
                    alternatives=tuple(alternatives),
                    contexts=tuple(contexts),
                    risk_flags=self._safe_string_tuple_json(
                        cluster_row["risk_flags_json"]
                    ),
                    affected_blocks=int(cluster_row["affected_block_count"] or 0),
                    members=tuple(complete_members),
                )
            )
        return clusters

    @staticmethod
    def _candidate_cluster_snapshot_digest(
        cluster_id: str,
        risk_flags_json: str,
        affected_blocks_json: str,
        affected_block_count: int,
        members: Sequence[Dict[str, Any]],
    ) -> str:
        payload = {
            "cluster_id": cluster_id,
            "risk_flags_json": risk_flags_json,
            "affected_blocks_json": affected_blocks_json,
            "affected_block_count": int(affected_block_count),
            "members": list(members),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _candidate_lexical_snapshot(member: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": member["lexical_id"],
            "block_id": member["block_id"],
            "paragraph_id": member["paragraph_id"],
            "start_offset": int(member["start_offset"]),
            "end_offset": int(member["end_offset"]),
            "original_text": member["original_text"],
            "normalized_text": member["normalized_text"],
            "left_context": member["left_context"],
            "right_context": member["right_context"],
            "risk_flags_json": member["lexical_risk_flags_json"],
            "updated_at": member["lexical_updated_at"],
            "source_hash": member["source_hash"],
            "source_edition_id": int(member["source_edition_id"]),
        }

    def _incoming_candidate_cluster_snapshot(
        self, connection: sqlite3.Connection, cluster: Any
    ) -> str:
        members = self._candidate_cluster_members(cluster)
        candidate_ids = [member["candidate_id"] for member in members]
        if not candidate_ids:
            raise ValueError(f"candidate cluster has no members: {cluster.id}")
        placeholders = ",".join("?" for _ in candidate_ids)
        lexical_by_id = {
            row["lexical_id"]: row
            for row in connection.execute(
                f"""SELECT lc.id AS lexical_id, lc.block_id, lc.paragraph_id,
                           lc.start_offset, lc.end_offset, lc.original_text,
                           lc.normalized_text, lc.left_context, lc.right_context,
                           lc.risk_flags_json AS lexical_risk_flags_json,
                           lc.updated_at AS lexical_updated_at,
                           b.source_hash, b.source_edition_id
                    FROM lexical_candidates lc
                    JOIN blocks b ON b.id=lc.block_id
                    WHERE lc.id IN ({placeholders})""",
                candidate_ids,
            ).fetchall()
        }
        payload_members: List[Dict[str, Any]] = []
        for ordinal, member in enumerate(members):
            item: Dict[str, Any] = {
                "candidate_id": member["candidate_id"],
                "role": member["role"],
                "ordinal": ordinal,
                "context_json": json.dumps(
                    member["context"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            lexical = lexical_by_id.get(member["candidate_id"])
            if lexical is None:
                raise ValueError(
                    f"unknown lexical candidate: {member['candidate_id']}"
                )
            item["lexical"] = self._candidate_lexical_snapshot(lexical)
            payload_members.append(item)
        affected_blocks = sorted({member["block_id"] for member in members})
        return self._candidate_cluster_snapshot_digest(
            cluster.id,
            json.dumps(
                sorted(set(cluster.risk_flags)),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            json.dumps(
                affected_blocks,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            int(cluster.affected_blocks),
            payload_members,
        )

    @staticmethod
    def _candidate_cluster_lease_snapshot(
        connection: sqlite3.Connection, cluster_id: str
    ) -> tuple[str, bool]:
        """Hash exactly the persisted candidate/source view leased to the model."""
        cluster = connection.execute(
            """SELECT id, risk_flags_json, affected_blocks_json,
                      affected_block_count
               FROM candidate_clusters WHERE id=?""",
            (cluster_id,),
        ).fetchone()
        if cluster is None:
            raise ValueError(f"unknown candidate cluster: {cluster_id}")
        members = connection.execute(
            """SELECT cm.candidate_id, cm.role, cm.ordinal, cm.context_json,
                      lc.id AS lexical_id, lc.block_id, lc.paragraph_id,
                      lc.start_offset, lc.end_offset, lc.original_text,
                      lc.normalized_text, lc.left_context, lc.right_context,
                      lc.risk_flags_json AS lexical_risk_flags_json,
                      lc.updated_at AS lexical_updated_at,
                      b.source_hash, b.source_edition_id, b.source_text
               FROM candidate_cluster_members cm
               JOIN lexical_candidates lc ON lc.id=cm.candidate_id
               JOIN blocks b ON b.id=lc.block_id
               WHERE cm.cluster_id=?
               ORDER BY cm.ordinal, cm.candidate_id""",
            (cluster_id,),
        ).fetchall()
        payload_members: List[Dict[str, Any]] = []
        source_slices_current = True
        for member in members:
            item: Dict[str, Any] = {
                "candidate_id": member["candidate_id"],
                "role": member["role"],
                "ordinal": int(member["ordinal"]),
                "context_json": member["context_json"],
            }
            start = int(member["start_offset"])
            end = int(member["end_offset"])
            source_text = member["source_text"] or ""
            original_text = member["original_text"]
            source_slices_current = source_slices_current and (
                0 <= start <= end <= len(source_text)
                and source_text[start:end] == original_text
            )
            if member["role"] in {"context", "both"}:
                try:
                    leased_context = json.loads(member["context_json"] or "{}")
                except (TypeError, ValueError):
                    leased_context = {}
                if isinstance(leased_context, dict):
                    leased_original = leased_context.get("original_text")
                    if isinstance(leased_original, str):
                        source_slices_current = source_slices_current and (
                            leased_original == original_text
                        )
            item["lexical"] = V4Database._candidate_lexical_snapshot(member)
            payload_members.append(item)
        return (
            V4Database._candidate_cluster_snapshot_digest(
                cluster["id"],
                cluster["risk_flags_json"],
                cluster["affected_blocks_json"],
                int(cluster["affected_block_count"]),
                payload_members,
            ),
            source_slices_current,
        )

    @staticmethod
    def _leased_cluster_has_pending_replacement(
        connection: sqlite3.Connection, cluster_id: str
    ) -> bool:
        return connection.execute(
            """SELECT 1
               FROM candidate_cluster_members old_cm
               JOIN lexical_candidates old_lc ON old_lc.id=old_cm.candidate_id
               JOIN candidate_clusters replacement ON replacement.state='pending'
               JOIN candidate_cluster_members new_cm
                 ON new_cm.cluster_id=replacement.id
                AND new_cm.role IN ('alternative', 'both')
               JOIN lexical_candidates new_lc ON new_lc.id=new_cm.candidate_id
               WHERE old_cm.cluster_id=?
                 AND old_cm.role IN ('alternative', 'both')
                 AND replacement.id!=?
                 AND old_lc.block_id=new_lc.block_id
                 AND old_lc.paragraph_id=new_lc.paragraph_id
                 AND old_lc.start_offset < new_lc.end_offset
                 AND new_lc.start_offset < old_lc.end_offset
               LIMIT 1""",
            (cluster_id, cluster_id),
        ).fetchone() is not None

    def _release_or_stale_cluster_leases(
        self,
        connection: sqlite3.Connection,
        cluster_ids: Sequence[str],
    ) -> int:
        now = utc_now()
        released = 0
        for cluster_id in cluster_ids:
            lease = connection.execute(
                """SELECT lease_snapshot_hash FROM candidate_clusters
                   WHERE id=? AND state='leased'""",
                (cluster_id,),
            ).fetchone()
            if lease is None:
                continue
            current_hash, source_current = self._candidate_cluster_lease_snapshot(
                connection, cluster_id
            )
            replacement_exists = self._leased_cluster_has_pending_replacement(
                connection, cluster_id
            )
            next_state = "pending"
            if (
                not lease["lease_snapshot_hash"]
                or lease["lease_snapshot_hash"] != current_hash
                or not source_current
                or replacement_exists
            ):
                next_state = "stale"
            cursor = connection.execute(
                """UPDATE candidate_clusters
                   SET state=?, lease_run_id=NULL, lease_acquired_at=NULL,
                       lease_snapshot_hash=NULL, updated_at=?
                   WHERE id=? AND state='leased'""",
                (next_state, now, cluster_id),
            )
            released += int(cursor.rowcount)
        return released

    def claim_pending_candidate_clusters(
        self, run_id: str, limit: int = 12
    ) -> List[Any]:
        """Atomically lease at most twelve clusters to one adjudication run."""
        bounded_limit = max(0, min(int(limit), 12))
        if bounded_limit == 0:
            return []
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT stage, status FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None or run["stage"] != "adjudicate" or run["status"] != "running":
                raise ValueError(f"run is not a running adjudication: {run_id}")
            cluster_rows = connection.execute(
                """SELECT cc.* FROM candidate_clusters cc
                   WHERE cc.state='pending'
                     AND NOT EXISTS(
                         SELECT 1 FROM candidate_adjudications ca
                         WHERE ca.cluster_id=cc.id
                     )
                     AND EXISTS(
                         SELECT 1
                         FROM candidate_cluster_members cm
                         JOIN lexical_candidates lc ON lc.id=cm.candidate_id
                         JOIN blocks b ON b.id=lc.block_id
                         WHERE cm.cluster_id=cc.id
                           AND cm.role IN ('alternative', 'both')
                           AND lc.resolution_status='pending'
                           AND b.source_edition_id=(
                               SELECT id FROM source_editions WHERE active=1
                           )
                     )
                   ORDER BY cc.id
                   LIMIT ?""",
                (bounded_limit,),
            ).fetchall()
            if not cluster_rows:
                return []
            now = utc_now()
            for cluster_row in cluster_rows:
                snapshot_hash, _source_current = (
                    self._candidate_cluster_lease_snapshot(
                        connection, cluster_row["id"]
                    )
                )
                cursor = connection.execute(
                    """UPDATE candidate_clusters
                       SET state='leased', lease_run_id=?, lease_acquired_at=?,
                           lease_snapshot_hash=?, updated_at=?
                       WHERE state='pending' AND id=?""",
                    (run_id, now, snapshot_hash, now, cluster_row["id"]),
                )
                if cursor.rowcount != 1:
                    raise StaleCandidateSnapshot("candidate cluster lease race")
            return self._hydrate_candidate_clusters(connection, cluster_rows)

    def has_claimable_candidate_clusters(self) -> bool:
        with closing(self.connect()) as connection:
            return connection.execute(
                """SELECT 1 FROM candidate_clusters cc
                   WHERE cc.state='pending'
                     AND NOT EXISTS(
                         SELECT 1 FROM candidate_adjudications ca
                         WHERE ca.cluster_id=cc.id
                     )
                     AND EXISTS(
                         SELECT 1
                         FROM candidate_cluster_members cm
                         JOIN lexical_candidates lc ON lc.id=cm.candidate_id
                         JOIN blocks b ON b.id=lc.block_id
                         WHERE cm.cluster_id=cc.id
                           AND cm.role IN ('alternative', 'both')
                           AND lc.resolution_status='pending'
                           AND b.source_edition_id=(
                               SELECT id FROM source_editions WHERE active=1
                           )
                     )
                   LIMIT 1"""
            ).fetchone() is not None

    def release_adjudication_leases(self, run_id: str) -> int:
        with self.transaction() as connection:
            cluster_ids = [
                row["id"]
                for row in connection.execute(
                    """SELECT id FROM candidate_clusters
                       WHERE state='leased' AND lease_run_id=?
                         AND NOT EXISTS(
                             SELECT 1 FROM candidate_adjudications ca
                             WHERE ca.cluster_id=candidate_clusters.id
                         )
                       ORDER BY id""",
                    (run_id,),
                ).fetchall()
            ]
            return self._release_or_stale_cluster_leases(connection, cluster_ids)

    def recover_stale_candidate_leases(self, max_age_seconds: int = 3600) -> int:
        """Explicitly recover abandoned leases without touching active decisions."""
        cutoff_seconds = max(0, int(max_age_seconds))
        with self.transaction() as connection:
            cluster_ids = [
                row["id"]
                for row in connection.execute(
                    """SELECT id FROM candidate_clusters
                       WHERE state='leased'
                         AND NOT EXISTS(
                             SELECT 1 FROM candidate_adjudications ca
                             WHERE ca.cluster_id=candidate_clusters.id
                         )
                         AND (
                             NOT EXISTS(
                                 SELECT 1 FROM runs r
                                 WHERE r.id=candidate_clusters.lease_run_id
                                   AND r.status='running'
                             )
                             OR julianday(lease_acquired_at) <=
                                julianday(?) - (? / 86400.0)
                         )
                       ORDER BY id""",
                    (utc_now(), cutoff_seconds),
                ).fetchall()
            ]
            return self._release_or_stale_cluster_leases(connection, cluster_ids)

    def finalize_adjudication_run(
        self, run_id: str, status: str, error: Optional[str] = None
    ) -> None:
        if status not in {"completed", "completed_with_errors", "failed"}:
            raise ValueError(f"invalid adjudication run status: {status}")
        with self.transaction() as connection:
            cluster_ids = [
                row["id"]
                for row in connection.execute(
                    """SELECT id FROM candidate_clusters
                       WHERE state='leased' AND lease_run_id=?
                         AND NOT EXISTS(
                             SELECT 1 FROM candidate_adjudications ca
                             WHERE ca.cluster_id=candidate_clusters.id
                         )
                       ORDER BY id""",
                    (run_id,),
                ).fetchall()
            ]
            self._release_or_stale_cluster_leases(connection, cluster_ids)
            connection.execute(
                """UPDATE runs SET status=?, finished_at=?, error=?
                   WHERE id=? AND stage='adjudicate'""",
                (status, utc_now(), error, run_id),
            )

    def source_texts_for_candidate_clusters(
        self, clusters: Sequence[Any]
    ) -> Dict[str, str]:
        block_ids = sorted(
            {
                candidate.block_id
                for cluster in clusters
                for candidate in cluster.alternatives
            }
        )
        if not block_ids:
            return {}
        placeholders = ",".join("?" for _ in block_ids)
        with closing(self.connect()) as connection:
            return {
                row["id"]: row["source_text"]
                for row in connection.execute(
                    f"SELECT id, source_text FROM blocks WHERE id IN ({placeholders})",
                    block_ids,
                ).fetchall()
            }

    @staticmethod
    def _adjudication_value(result: Any, field: str, default: Any = None) -> Any:
        if isinstance(result, dict):
            return result.get(field, default)
        return getattr(result, field, default)

    @staticmethod
    def _cluster_member_is_selected(
        member: sqlite3.Row, selected_ids: set[str]
    ) -> bool:
        return str(member["id"]) in selected_ids

    @staticmethod
    def _active_adjudication_matches(
        connection: sqlite3.Connection,
        adjudication_id: str,
        result: Dict[str, Any],
        members: Sequence[sqlite3.Row],
    ) -> bool:
        resolution_rows = connection.execute(
            """SELECT candidate_id, decision, lexeme_id, concept_id, evidence_id
               FROM candidate_resolutions WHERE adjudication_id=?""",
            (adjudication_id,),
        ).fetchall()
        member_ids = {str(row["id"]) for row in members}
        resolution_candidate_ids = [
            str(row["candidate_id"]) for row in resolution_rows
        ]
        if (
            len(resolution_rows) != len(members)
            or len(set(resolution_candidate_ids)) != len(resolution_candidate_ids)
            or set(resolution_candidate_ids) != member_ids
        ):
            return False
        resolutions = {
            str(row["candidate_id"]): row
            for row in resolution_rows
        }
        selected = set(result["selected_ids"])
        selected_evidence_ids = [
            resolutions[candidate_id]["evidence_id"] for candidate_id in selected
        ]
        if any(evidence_id is None for evidence_id in selected_evidence_ids) or len(
            set(selected_evidence_ids)
        ) != len(selected_evidence_ids):
            return False
        chain_rows_by_candidate: Dict[str, List[sqlite3.Row]] = {
            candidate_id: [] for candidate_id in selected
        }
        for row in connection.execute(
            """SELECT cr.candidate_id,
                      e.block_id AS evidence_block_id,
                      e.paragraph_id AS evidence_paragraph_id,
                      e.kind AS evidence_kind,
                      e.source_form AS evidence_source_form,
                      e.evidence_quote,
                      e.payload_json AS evidence_payload_json,
                      e.confidence AS evidence_confidence,
                      e.extractor AS evidence_extractor,
                      e.run_id AS evidence_run_id,
                      ca.run_id AS adjudication_run_id,
                      m.id AS mention_id,
                      m.block_id AS mention_block_id,
                      m.paragraph_id AS mention_paragraph_id,
                      m.source_form AS mention_source_form,
                      m.normalized_form AS mention_normalized_form,
                      m.discourse_function,
                      m.concept_id AS mention_concept_id,
                      cto.id AS observation_id,
                      cto.mention_id AS observation_mention_id,
                      cto.kind AS observation_kind,
                      cto.confidence AS observation_confidence,
                      cto.source AS observation_source,
                      cto.concept_id AS observation_concept_id,
                      cto.retired_version AS observation_retired_version
               FROM candidate_resolutions cr
               JOIN candidate_adjudications ca ON ca.id=cr.adjudication_id
               LEFT JOIN evidence e ON e.id=cr.evidence_id
               LEFT JOIN concept_type_observations cto
                 ON cto.adjudication_id=cr.adjudication_id
                    AND cto.lexeme_id=cr.lexeme_id
                    AND cto.evidence_id=cr.evidence_id
               LEFT JOIN mentions m
                 ON m.evidence_id=cr.evidence_id AND m.lexeme_id=cr.lexeme_id
               WHERE cr.adjudication_id=?""",
            (adjudication_id,),
        ).fetchall():
            candidate_id = str(row["candidate_id"])
            if candidate_id in chain_rows_by_candidate:
                chain_rows_by_candidate[candidate_id].append(row)
        active_observation_ids = {
            int(row["id"])
            for row in connection.execute(
                """SELECT id FROM concept_type_observations
                   WHERE adjudication_id=? AND source='candidate_adjudication'
                         AND retired_version IS NULL""",
                (adjudication_id,),
            ).fetchall()
        }
        consumed_mention_ids: set[int] = set()
        consumed_observation_ids: set[int] = set()
        for member in members:
            candidate_id = str(member["id"])
            resolution = resolutions[candidate_id]
            if V4Database._cluster_member_is_selected(member, selected):
                expected_lexeme_id = stable_id(
                    "lexeme",
                    f"en:{normalize_english_form(member['original_text'])}",
                )
                if (
                    resolution["decision"] != result["verdict"]
                    or resolution["lexeme_id"] != expected_lexeme_id
                    or resolution["concept_id"] is not None
                    or resolution["evidence_id"] is None
                    or member["resolution_status"] != "promoted"
                    or member["model_status"] != "accepted"
                    or not member["selected"]
                ):
                    return False
                chain_rows = chain_rows_by_candidate.get(str(candidate_id), [])
                if len(chain_rows) != 1:
                    return False
                chain = chain_rows[0]
                normalized_form = normalize_english_form(member["original_text"])
                if (
                    chain["evidence_block_id"] != member["block_id"]
                    or chain["evidence_paragraph_id"] != member["paragraph_id"]
                    or chain["evidence_kind"] != "candidate_adjudication"
                    or chain["evidence_source_form"] != member["original_text"]
                    or chain["evidence_quote"] != member["original_text"]
                    or chain["evidence_payload_json"] != result["payload_json"]
                    or chain["evidence_confidence"] != result["confidence"]
                    or chain["evidence_extractor"] != "candidate_adjudication"
                    or chain["evidence_run_id"] != chain["adjudication_run_id"]
                    or chain["mention_id"] is None
                    or chain["mention_block_id"] != member["block_id"]
                    or chain["mention_paragraph_id"] != member["paragraph_id"]
                    or chain["mention_source_form"] != member["original_text"]
                    or chain["mention_normalized_form"] != normalized_form
                    or chain["discourse_function"] != "referential"
                    or chain["mention_concept_id"] is not None
                    or chain["observation_id"] is None
                    or chain["observation_mention_id"] != chain["mention_id"]
                    or chain["observation_kind"] != result["entity_kind"]
                    or chain["observation_confidence"] != result["confidence"]
                    or chain["observation_source"] != "candidate_adjudication"
                    or chain["observation_concept_id"] is not None
                    or chain["observation_retired_version"] is not None
                ):
                    return False
                mention_id = int(chain["mention_id"])
                observation_id = int(chain["observation_id"])
                if (
                    mention_id in consumed_mention_ids
                    or observation_id in consumed_observation_ids
                ):
                    return False
                consumed_mention_ids.add(mention_id)
                consumed_observation_ids.add(observation_id)
                occurrence = connection.execute(
                    """SELECT 1 FROM form_occurrences
                       WHERE lexeme_id=? AND block_id=? AND start_offset=?
                             AND end_offset=? AND source_form=? AND source_hash=?""",
                    (
                        resolution["lexeme_id"],
                        member["block_id"],
                        member["start_offset"],
                        member["end_offset"],
                        member["original_text"],
                        member["block_source_hash"],
                    ),
                ).fetchone()
                if occurrence is None:
                    return False
            else:
                expected = (
                    "deferred"
                    if result["verdict"] == "defer"
                    else "superseded"
                    if result["verdict"] == "supersede"
                    else "rejected"
                )
                if (
                    resolution["decision"] != expected
                    or resolution["lexeme_id"] is not None
                    or resolution["concept_id"] is not None
                    or resolution["evidence_id"] is not None
                    or member["resolution_status"] != expected
                    or member["model_status"] != expected
                    or member["selected"]
                ):
                    return False
        return consumed_observation_ids == active_observation_ids

    def commit_adjudications(
        self,
        run_id: str,
        results: Sequence[Any],
        *,
        audit_attempts: Sequence[Any] = (),
        require_lease: bool = False,
        finalize_run_status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomically persist adjudications as lexeme-owned span/type evidence."""
        ordered = sorted(
            results,
            key=lambda result: str(self._adjudication_value(result, "cluster_id", "")),
        )
        cluster_ids = [
            str(self._adjudication_value(result, "cluster_id", ""))
            for result in ordered
        ]
        if not all(cluster_ids) or len(set(cluster_ids)) != len(cluster_ids):
            raise ValueError("adjudication results require unique cluster ids")
        valid_verdicts = {"promote", "split", "supersede", "reject", "defer"}
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
                raise ValueError(f"unknown run: {run_id}")
            cluster_members: Dict[str, List[sqlite3.Row]] = {}
            for cluster_id in cluster_ids:
                cluster_row = connection.execute(
                    """SELECT state, lease_run_id, lease_snapshot_hash
                       FROM candidate_clusters
                       WHERE id=?""",
                    (cluster_id,),
                ).fetchone()
                if cluster_row is None:
                    raise ValueError(f"unknown candidate cluster: {cluster_id}")
                if require_lease and (
                    cluster_row["state"] != "leased"
                    or cluster_row["lease_run_id"] != run_id
                ):
                    raise StaleAdjudicationCommit(
                        f"cluster lease is not owned by {run_id}: {cluster_id}"
                    )
                if require_lease:
                    current_snapshot_hash, source_current = (
                        self._candidate_cluster_lease_snapshot(
                            connection, cluster_id
                        )
                    )
                    if (
                        not cluster_row["lease_snapshot_hash"]
                        or cluster_row["lease_snapshot_hash"]
                        != current_snapshot_hash
                        or not source_current
                    ):
                        raise StaleAdjudicationLease(
                            f"candidate/source lease snapshot changed: {cluster_id}"
                        )
                rows = connection.execute(
                    """SELECT lc.*, cm.role AS member_role,
                              cm.context_json AS member_context_json,
                              b.source_hash AS block_source_hash
                       FROM candidate_cluster_members cm
                       JOIN lexical_candidates lc ON lc.id=cm.candidate_id
                       JOIN blocks b ON b.id=lc.block_id
                       WHERE cm.cluster_id=?
                         AND (
                             cm.role IN ('alternative', 'both')
                             OR json_extract(
                                 cm.context_json, '$.support_member'
                             )=1
                         )
                       ORDER BY cm.ordinal, lc.id""",
                    (cluster_id,),
                ).fetchall()
                if not rows:
                    raise ValueError(
                        f"candidate cluster has no persisted alternatives: {cluster_id}"
                    )
                cluster_members[cluster_id] = list(rows)

            for audit in audit_attempts:
                messages = self._adjudication_value(audit, "messages", ())
                parsed = self._adjudication_value(audit, "parsed")
                self.record_audit_call(
                    run_id=run_id,
                    block_id=None,
                    purpose="candidate_adjudication",
                    model=str(self._adjudication_value(audit, "model", "unknown")),
                    knowledge_version=int(
                        self._adjudication_value(audit, "knowledge_version", 0)
                    ),
                    request={
                        "messages": [dict(message) for message in messages],
                        "audit_mode": str(
                            self._adjudication_value(audit, "audit_mode", "full")
                        ),
                    },
                    raw_response=str(
                        self._adjudication_value(audit, "raw_response", "") or ""
                    ),
                    parsed=dict(parsed) if parsed is not None else None,
                    accepted=bool(
                        self._adjudication_value(audit, "accepted", False)
                    ),
                    attempts=max(
                        1, int(self._adjudication_value(audit, "attempt", 1))
                    ),
                    elapsed_ms=max(
                        0, int(self._adjudication_value(audit, "elapsed_ms", 0))
                    ),
                    error=self._adjudication_value(audit, "error"),
                    connection=connection,
                )

            normalized_results: List[Dict[str, Any]] = []
            for result, cluster_id in zip(ordered, cluster_ids):
                verdict = str(self._adjudication_value(result, "verdict", ""))
                reason = str(self._adjudication_value(result, "reason", "") or "")
                selected_ids = tuple(
                    str(value)
                    for value in self._adjudication_value(
                        result, "selected_candidate_ids", ()
                    )
                )
                if verdict not in valid_verdicts:
                    raise ValueError(f"invalid adjudication verdict: {verdict}")
                if len(set(selected_ids)) != len(selected_ids):
                    raise ValueError(f"duplicate selected candidate in cluster {cluster_id}")
                selected_id_set = set(selected_ids)
                representative_rows = [
                    row
                    for row in cluster_members[cluster_id]
                    if row["member_role"] in {"alternative", "both"}
                ]
                representative_ids = {row["id"] for row in representative_rows}
                missing = sorted(selected_id_set - representative_ids)
                if missing:
                    raise ValueError(
                        f"selected candidate(s) are not alternatives in cluster "
                        f"{cluster_id}: {', '.join(missing)}"
                    )
                selected_rows = [
                    row
                    for row in representative_rows
                    if row["id"] in selected_id_set
                ]
                if reason == "missing_span" and verdict != "defer":
                    raise ValueError("missing_span requires defer")
                if verdict == "promote" and len(selected_rows) != 1:
                    raise ValueError("promote requires exactly one candidate")
                if verdict in {"reject", "defer"} and selected_rows:
                    raise ValueError(f"{verdict} cannot select candidates")
                if verdict == "split":
                    if len(selected_rows) < 2:
                        raise ValueError("split requires at least two candidates")
                    if any(
                        left["block_id"] == right["block_id"]
                        and left["start_offset"] < right["end_offset"]
                        and right["start_offset"] < left["end_offset"]
                        for index, left in enumerate(selected_rows)
                        for right in selected_rows[index + 1 :]
                    ):
                        raise ValueError("split candidates overlap")
                if verdict == "supersede":
                    if len(selected_rows) != 1:
                        raise ValueError("supersede requires exactly one candidate")
                    selected_row = selected_rows[0]
                    if not any(
                        other["id"] not in selected_id_set
                        and selected_row["block_id"] == other["block_id"]
                        and selected_row["start_offset"] <= other["start_offset"]
                        and other["end_offset"] <= selected_row["end_offset"]
                        for other in representative_rows
                    ):
                        raise ValueError(
                            "supersede candidate does not contain an unselected alternative"
                        )
                normalized = {
                    "cluster_id": cluster_id,
                    "verdict": verdict,
                    "selected_ids": tuple(sorted(selected_ids)),
                    "entity_kind": str(
                        self._adjudication_value(result, "entity_kind", "") or "concept"
                    ),
                    "confidence": float(
                        self._adjudication_value(result, "confidence", 0.0)
                    ),
                    "reason": reason,
                    "rounds": max(
                        1, int(self._adjudication_value(result, "rounds", 1) or 1)
                    ),
                }
                payload = {
                    "cluster_id": cluster_id,
                    "verdict": verdict,
                    "selected_candidate_ids": list(normalized["selected_ids"]),
                    "entity_kind": normalized["entity_kind"],
                    "confidence": normalized["confidence"],
                    "reason": normalized["reason"],
                    "rounds": normalized["rounds"],
                }
                payload_json = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                normalized["payload"] = payload
                normalized["payload_json"] = payload_json
                normalized["payload_hash"] = hashlib.sha256(
                    payload_json.encode("utf-8")
                ).hexdigest()
                normalized["active"] = connection.execute(
                    """SELECT id, run_id, payload_hash, knowledge_version
                       FROM candidate_adjudications
                       WHERE cluster_id=? AND active=1""",
                    (cluster_id,),
                ).fetchone()
                if (
                    normalized["active"] is not None
                    and normalized["active"]["run_id"] != run_id
                ):
                    raise StaleAdjudicationCommit(
                        f"cluster already has an active adjudication: {cluster_id}"
                    )
                normalized["state_matches"] = bool(
                    normalized["active"]
                    and self._active_adjudication_matches(
                        connection,
                        normalized["active"]["id"],
                        normalized,
                        cluster_members[cluster_id],
                    )
                )
                normalized_results.append(normalized)

            changed_results = [
                result
                for result in normalized_results
                if result["active"] is None
                or result["active"]["payload_hash"] != result["payload_hash"]
                or not result["state_matches"]
            ]
            if not changed_results:
                version = (
                    max(int(result["active"]["knowledge_version"]) for result in normalized_results)
                    if normalized_results
                    else int(connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0])
                )
                self._complete_committed_clusters(
                    connection,
                    run_id,
                    cluster_ids,
                    finalize_run_status=finalize_run_status,
                )
                return {
                    "knowledge_version": version,
                    "adjudications": len(normalized_results),
                    "concept_ids": [],
                    "changed": 0,
                }

            version = self.create_knowledge_version(
                f"candidate adjudication {run_id}", connection
            )
            for result in changed_results:
                cluster_id = result["cluster_id"]
                now = utc_now()
                previous_concept_ids: List[str] = []
                if result["active"] is not None:
                    previous_concept_ids = [
                        row[0]
                        for row in connection.execute(
                            """SELECT DISTINCT concept_id FROM candidate_resolutions
                               WHERE adjudication_id=? AND concept_id IS NOT NULL""",
                            (result["active"]["id"],),
                        ).fetchall()
                    ]
                    connection.execute(
                        """UPDATE candidate_adjudications
                           SET active=0, superseded_at=?, updated_at=? WHERE id=?""",
                        (now, now, result["active"]["id"]),
                    )
                adjudication_id = stable_id(
                    "adjud",
                    f"{run_id}:{cluster_id}:{version}:{result['payload_hash']}",
                    length=24,
                )
                connection.execute(
                    """INSERT INTO candidate_adjudications(
                           id, run_id, cluster_id, verdict, payload_hash,
                           selected_candidate_ids_json, entity_kind, confidence,
                           reason, rounds, payload_json, knowledge_version,
                           active, created_at, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        adjudication_id,
                        run_id,
                        cluster_id,
                        result["verdict"],
                        result["payload_hash"],
                        json.dumps(result["selected_ids"], ensure_ascii=False),
                        result["entity_kind"],
                        result["confidence"],
                        result["reason"],
                        result["rounds"],
                        result["payload_json"],
                        version,
                        now,
                        now,
                    ),
                )

                selected = set(result["selected_ids"])
                member_rows = cluster_members[cluster_id]
                for ordinal, candidate in enumerate(member_rows):
                    candidate_id = candidate["id"]
                    candidate_lexeme_id: Optional[str] = None
                    evidence_id: Optional[int] = None
                    if self._cluster_member_is_selected(candidate, selected):
                        normalized_form = normalize_english_form(candidate["original_text"])
                        candidate_lexeme_id = self.ensure_lexeme(
                            candidate["original_text"],
                            connection=connection,
                        )
                        cursor = connection.execute(
                            """INSERT INTO evidence(
                                   block_id, paragraph_id, kind, source_form,
                                   evidence_quote, payload_json, confidence,
                                   extractor, run_id, created_at
                               ) VALUES(?, ?, 'candidate_adjudication', ?, ?, ?, ?,
                                        'candidate_adjudication', ?, ?)""",
                            (
                                candidate["block_id"],
                                candidate["paragraph_id"],
                                candidate["original_text"],
                                candidate["original_text"],
                                result["payload_json"],
                                result["confidence"],
                                run_id,
                                now,
                            ),
                        )
                        evidence_id = int(cursor.lastrowid)
                        mention_cursor = connection.execute(
                            """INSERT INTO mentions(
                                   block_id, paragraph_id, source_form,
                                   normalized_form, discourse_function,
                                   lexeme_id, concept_id, evidence_id
                               ) VALUES(?, ?, ?, ?, 'referential', ?, NULL, ?)""",
                            (
                                candidate["block_id"],
                                candidate["paragraph_id"],
                                candidate["original_text"],
                                normalized_form,
                                candidate_lexeme_id,
                                evidence_id,
                            ),
                        )
                        mention_id = int(mention_cursor.lastrowid)
                        self.record_form_occurrences(
                            [
                                FormOccurrence(
                                    lexeme_id=candidate_lexeme_id,
                                    block_id=str(candidate["block_id"]),
                                    start_offset=int(candidate["start_offset"]),
                                    end_offset=int(candidate["end_offset"]),
                                    source_form=str(candidate["original_text"]),
                                    source_hash=str(candidate["block_source_hash"]),
                                )
                            ],
                            connection=connection,
                        )
                        self.record_type_observation(
                            candidate_lexeme_id,
                            result["entity_kind"],
                            confidence=result["confidence"],
                            source="candidate_adjudication",
                            mention_id=mention_id,
                            evidence_id=evidence_id,
                            adjudication_id=adjudication_id,
                            connection=connection,
                        )
                        connection.execute(
                            """UPDATE lexical_candidates
                               SET resolution_status='promoted', model_status='accepted',
                                   selected=1, updated_at=?
                               WHERE id=?""",
                            (now, candidate_id),
                        )
                        member_decision = result["verdict"]
                    else:
                        if result["verdict"] == "defer":
                            status = "deferred"
                        elif result["verdict"] == "supersede":
                            status = "superseded"
                        else:
                            status = "rejected"
                        connection.execute(
                            """UPDATE lexical_candidates
                               SET resolution_status=?, model_status=?, selected=0,
                                   updated_at=? WHERE id=?""",
                            (status, status, now, candidate_id),
                        )
                        member_decision = status
                    resolution_id = stable_id(
                        "resolution",
                        f"{adjudication_id}:{ordinal}:{candidate_id}",
                        length=24,
                    )
                    connection.execute(
                        """INSERT INTO candidate_resolutions(
                               id, adjudication_id, run_id, cluster_id, candidate_id,
                               lexeme_id, concept_id, evidence_id, decision, ordinal,
                               payload_json, created_at
                            ) VALUES(?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)""",
                        (
                            resolution_id,
                            adjudication_id,
                            run_id,
                            cluster_id,
                            candidate_id,
                            candidate_lexeme_id,
                            evidence_id,
                            member_decision,
                            ordinal,
                            result["payload_json"],
                            now,
                        ),
                    )
                for previous_concept_id in previous_concept_ids:
                    connection.execute(
                        """UPDATE concepts SET status='retired', retired_version=?
                           WHERE id=? AND locked=0 AND retired_version IS NULL
                             AND default_target='' AND working_target=''
                             AND verified_target=''
                             AND NOT EXISTS(
                                 SELECT 1 FROM candidate_resolutions cr
                                 JOIN candidate_adjudications ca
                                   ON ca.id=cr.adjudication_id
                                 WHERE cr.concept_id=concepts.id AND ca.active=1)
                             AND NOT EXISTS(
                                 SELECT 1 FROM mentions m JOIN evidence e
                                   ON e.id=m.evidence_id
                                 WHERE m.concept_id=concepts.id
                                   AND e.extractor!='candidate_adjudication')
                             AND NOT EXISTS(
                                 SELECT 1 FROM rendering_rules rr
                                 WHERE rr.concept_id=concepts.id
                                   AND rr.retired_version IS NULL)""",
                        (version, previous_concept_id),
                    )
            self._complete_committed_clusters(
                connection,
                run_id,
                cluster_ids,
                finalize_run_status=finalize_run_status,
            )
            return {
                "knowledge_version": version,
                "adjudications": len(changed_results),
                "concept_ids": [],
                "changed": len(changed_results),
            }

    @staticmethod
    def _complete_committed_clusters(
        connection: sqlite3.Connection,
        run_id: str,
        cluster_ids: Sequence[str],
        *,
        finalize_run_status: Optional[str],
    ) -> None:
        if cluster_ids:
            placeholders = ",".join("?" for _ in cluster_ids)
            connection.execute(
                f"""UPDATE candidate_clusters
                    SET state='adjudicated', lease_run_id=NULL,
                        lease_acquired_at=NULL, lease_snapshot_hash=NULL,
                        updated_at=?
                    WHERE id IN ({placeholders})""",
                [utc_now(), *cluster_ids],
            )
        if finalize_run_status is not None:
            if finalize_run_status not in {"completed", "completed_with_errors"}:
                raise ValueError(
                    f"invalid adjudication final status: {finalize_run_status}"
                )
            connection.execute(
                """UPDATE runs SET status=?, finished_at=?, error=NULL
                   WHERE id=? AND stage='adjudicate'""",
                (finalize_run_status, utc_now(), run_id),
            )

    @staticmethod
    def _scan_evidence_where(alias: str = "e") -> str:
        return (
            f"({alias}.extractor LIKE 'scan_%' "
            f"OR {alias}.extractor='candidate_adjudication')"
        )

    def _prepare_scan_reset_selection(self, connection: sqlite3.Connection) -> None:
        evidence_where = self._scan_evidence_where("e")
        statements = (
            """CREATE TEMP TABLE reset_scan_audit_runs AS
               SELECT id FROM runs
               WHERE stage IN (
                   'scan', 'candidate_adjudication', 'adjudicate', 'working_target'
               )""",
            """CREATE TEMP TABLE reset_scan_blocks AS
               SELECT b.id FROM blocks b
               WHERE (b.status!='pending' OR b.last_error IS NOT NULL)
                 AND NOT EXISTS(
                     SELECT 1 FROM translation_versions t WHERE t.block_id=b.id)""",
            """CREATE TEMP TABLE reset_protected_type_observations AS
               SELECT cto.id FROM concept_type_observations cto
               LEFT JOIN concepts c ON c.id=cto.concept_id
               WHERE cto.source!='candidate_adjudication'
                  OR COALESCE(c.locked, 0)=1""",
            """CREATE TEMP TABLE reset_protected_adjudications AS
               SELECT DISTINCT cto.adjudication_id AS id
               FROM concept_type_observations cto
               WHERE cto.id IN (
                         SELECT id FROM reset_protected_type_observations)
                 AND cto.adjudication_id IS NOT NULL""",
            """CREATE TEMP TABLE reset_protected_clusters AS
               SELECT DISTINCT ca.cluster_id AS id
               FROM candidate_adjudications ca
               WHERE ca.id IN (SELECT id FROM reset_protected_adjudications)""",
            """CREATE TEMP TABLE reset_protected_candidates AS
               SELECT cm.candidate_id AS id
               FROM candidate_cluster_members cm
               WHERE cm.cluster_id IN (SELECT id FROM reset_protected_clusters)
               UNION
               SELECT cr.candidate_id AS id FROM candidate_resolutions cr
               WHERE cr.adjudication_id IN (
                         SELECT id FROM reset_protected_adjudications)
                 AND cr.candidate_id IS NOT NULL""",
            """CREATE TEMP TABLE reset_scan_candidate_adjudications AS
               SELECT id FROM candidate_adjudications
               WHERE id NOT IN (SELECT id FROM reset_protected_adjudications)""",
            """CREATE TEMP TABLE reset_scan_candidate_resolutions AS
               SELECT id FROM candidate_resolutions
               WHERE adjudication_id IN (
                   SELECT id FROM reset_scan_candidate_adjudications)""",
            """CREATE TEMP TABLE reset_scan_candidate_clusters AS
               SELECT id FROM candidate_clusters
               WHERE id NOT IN (SELECT id FROM reset_protected_clusters)""",
            """CREATE TEMP TABLE reset_scan_candidate_cluster_members AS
               SELECT cluster_id, candidate_id FROM candidate_cluster_members
               WHERE cluster_id IN (SELECT id FROM reset_scan_candidate_clusters)""",
            """CREATE TEMP TABLE reset_scan_lexical_candidates AS
               SELECT id FROM lexical_candidates
               WHERE id NOT IN (SELECT id FROM reset_protected_candidates)""",
            """CREATE TEMP TABLE reset_protected_mentions AS
               SELECT DISTINCT m.id FROM mentions m
               JOIN usage_decisions ud ON ud.mention_id=m.id
               WHERE ud.locked=1 OR ud.status='verified'
               UNION
               SELECT cto.mention_id FROM concept_type_observations cto
               WHERE cto.id IN (
                         SELECT id FROM reset_protected_type_observations)
                 AND cto.mention_id IS NOT NULL""",
            """CREATE TEMP TABLE reset_protected_evidence AS
               SELECT ce.evidence_id FROM claim_evidence ce
               JOIN claims c ON c.id=ce.claim_id
               WHERE c.locked=1
               UNION
               SELECT m.evidence_id FROM mentions m
               WHERE m.id IN (SELECT id FROM reset_protected_mentions)
               UNION
               SELECT cto.evidence_id FROM concept_type_observations cto
               WHERE cto.id IN (
                         SELECT id FROM reset_protected_type_observations)
                 AND cto.evidence_id IS NOT NULL
               UNION
               SELECT cr.evidence_id FROM candidate_resolutions cr
               WHERE cr.adjudication_id IN (
                         SELECT id FROM reset_protected_adjudications)
                 AND cr.evidence_id IS NOT NULL""",
            f"""CREATE TEMP TABLE reset_protected_concepts AS
                 SELECT id FROM concepts WHERE locked=1
                 UNION SELECT m.concept_id FROM mentions m
                       WHERE m.id IN (SELECT id FROM reset_protected_mentions)
                         AND m.concept_id IS NOT NULL
                 UNION SELECT cto.concept_id FROM concept_type_observations cto
                       WHERE cto.id IN (
                                 SELECT id FROM reset_protected_type_observations)
                         AND cto.concept_id IS NOT NULL
                 UNION SELECT DISTINCT m.concept_id FROM mentions m
                       JOIN evidence e ON e.id=m.evidence_id
                       WHERE m.concept_id IS NOT NULL AND NOT {evidence_where}
                 UNION SELECT concept_id FROM rendering_rules
                       WHERE locked=1 OR status='verified'""",
            f"""CREATE TEMP TABLE reset_scan_evidence AS
                 SELECT e.id FROM evidence e WHERE {evidence_where}
                   AND e.id NOT IN (SELECT evidence_id FROM reset_protected_evidence)
                   AND NOT EXISTS(
                       SELECT 1 FROM mentions m
                       WHERE m.evidence_id=e.id
                         AND m.id IN (SELECT id FROM reset_protected_mentions))""",
            """CREATE TEMP TABLE reset_scan_mentions AS
               SELECT m.id FROM mentions m
               WHERE m.evidence_id IN (SELECT id FROM reset_scan_evidence)
                 AND m.id NOT IN (SELECT id FROM reset_protected_mentions)""",
            """CREATE TEMP TABLE reset_scan_claims AS
               SELECT DISTINCT c.id FROM claims c
               JOIN claim_evidence ce ON ce.claim_id=c.id
               WHERE c.locked=0
                 AND ce.evidence_id IN (SELECT id FROM reset_scan_evidence)""",
            """CREATE TEMP TABLE reset_scan_concepts AS
               SELECT DISTINCT c.id FROM concepts c
               WHERE c.locked=0
                 AND c.id NOT IN (SELECT id FROM reset_protected_concepts)
                  AND (EXISTS(SELECT 1 FROM candidate_resolutions cr
                              WHERE cr.concept_id=c.id
                                AND cr.id IN (
                                    SELECT id FROM reset_scan_candidate_resolutions))
                      OR EXISTS(SELECT 1 FROM mentions m JOIN evidence e
                                ON e.id=m.evidence_id
                                WHERE m.concept_id=c.id
                                  AND (e.extractor LIKE 'scan_%'
                                        OR e.extractor='candidate_adjudication')))""",
            """CREATE TEMP TABLE reset_scan_type_observations AS
               SELECT cto.id FROM concept_type_observations cto
               LEFT JOIN concepts c ON c.id=cto.concept_id
               WHERE cto.source='candidate_adjudication'
                 AND COALESCE(c.locked, 0)=0
                 AND cto.id NOT IN (
                     SELECT id FROM reset_protected_type_observations)""",
            """CREATE TEMP TABLE reset_scan_lexemes AS
               SELECT DISTINCT l.id FROM lexemes l
               WHERE (
                   EXISTS(SELECT 1 FROM candidate_resolutions cr
                          WHERE cr.lexeme_id=l.id
                            AND cr.id IN (
                                SELECT id FROM reset_scan_candidate_resolutions))
                   OR EXISTS(SELECT 1 FROM mentions m
                             WHERE m.lexeme_id=l.id
                               AND m.id IN (SELECT id FROM reset_scan_mentions))
                   OR EXISTS(SELECT 1 FROM concept_type_observations cto
                             WHERE cto.lexeme_id=l.id
                               AND cto.id IN (
                                   SELECT id FROM reset_scan_type_observations)))
                 AND NOT EXISTS(
                     SELECT 1 FROM concepts c
                     WHERE c.primary_lexeme_id=l.id
                       AND c.id NOT IN (SELECT id FROM reset_scan_concepts))
                 AND NOT EXISTS(
                     SELECT 1 FROM concept_lexemes cl
                     WHERE cl.lexeme_id=l.id
                       AND cl.concept_id NOT IN (SELECT id FROM reset_scan_concepts))
                 AND NOT EXISTS(
                     SELECT 1 FROM mentions m
                     WHERE m.lexeme_id=l.id
                       AND m.id NOT IN (SELECT id FROM reset_scan_mentions))
                 AND NOT EXISTS(
                     SELECT 1 FROM form_occurrences fo
                     WHERE fo.lexeme_id=l.id)
                 AND NOT EXISTS(
                     SELECT 1 FROM concept_type_observations cto
                     WHERE cto.lexeme_id=l.id
                       AND cto.id NOT IN (
                           SELECT id FROM reset_scan_type_observations))""",
            """CREATE TEMP TABLE reset_scan_usage_decisions AS
               SELECT ud.id FROM usage_decisions ud
               WHERE ud.mention_id IN (SELECT id FROM reset_scan_mentions)
                 AND ud.locked=0 AND ud.status!='verified'""",
            """CREATE TEMP TABLE reset_scan_claim_evidence AS
               SELECT ce.claim_id, ce.evidence_id FROM claim_evidence ce
               WHERE ce.evidence_id IN (SELECT id FROM reset_scan_evidence)
                  OR ce.claim_id IN (SELECT id FROM reset_scan_claims)
               EXCEPT
               SELECT ce.claim_id, ce.evidence_id FROM claim_evidence ce
               JOIN claims c ON c.id=ce.claim_id WHERE c.locked=1""",
            """CREATE TEMP TABLE reset_scan_source_forms AS
               SELECT sf.id FROM source_forms sf
               WHERE sf.lexeme_id IN (SELECT id FROM reset_scan_lexemes)
                  OR (EXISTS(
                   SELECT 1 FROM concept_lexemes cl
                   WHERE cl.lexeme_id=sf.lexeme_id
                     AND cl.concept_id IN (SELECT id FROM reset_scan_concepts))
                 AND NOT EXISTS(
                   SELECT 1 FROM concept_lexemes cl
                   WHERE cl.lexeme_id=sf.lexeme_id
                     AND cl.concept_id NOT IN (SELECT id FROM reset_scan_concepts)
                     AND cl.retired_version IS NULL))""",
            """CREATE TEMP TABLE reset_scan_rendering_rules AS
               SELECT id FROM rendering_rules
               WHERE concept_id IN (SELECT id FROM reset_scan_concepts)""",
            """CREATE TEMP TABLE reset_scan_verification_tasks AS
               SELECT id FROM verification_tasks
               WHERE (subject_type='claim'
                      AND subject_id IN (SELECT id FROM reset_scan_claims))
                  OR (subject_type='concept'
                      AND subject_id IN (SELECT id FROM reset_scan_concepts))""",
            """CREATE TEMP TABLE reset_scan_verification_votes AS
               SELECT id FROM verification_votes
               WHERE task_id IN (SELECT id FROM reset_scan_verification_tasks)""",
        )
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "CREATE TEMP TABLE reset_scan_audit_calls(id INTEGER PRIMARY KEY)"
        )
        rows = connection.execute(
            """SELECT id, purpose, model, request_json, parsed_json
               FROM audit_calls
               WHERE run_id IN (SELECT id FROM reset_scan_audit_runs)
               ORDER BY id"""
        ).fetchall()
        connection.executemany(
            "INSERT INTO reset_scan_audit_calls(id) VALUES(?)",
            [
                (int(row["id"]),)
                for row in rows
                if not self._audit_row_is_human(row)
            ],
        )
        connection.execute(
            """CREATE TEMP TABLE reset_scan_runs AS
               SELECT r.id FROM runs r
               WHERE r.id IN (SELECT id FROM reset_scan_audit_runs)
                 AND NOT EXISTS(
                     SELECT 1 FROM audit_calls a
                     WHERE a.run_id=r.id
                       AND a.id NOT IN (SELECT id FROM reset_scan_audit_calls))
                 AND NOT EXISTS(
                     SELECT 1 FROM evidence e
                     WHERE e.run_id=r.id
                       AND e.id NOT IN (SELECT id FROM reset_scan_evidence))
                 AND NOT EXISTS(
                     SELECT 1 FROM translation_versions t WHERE t.run_id=r.id)"""
        )

    @staticmethod
    def _scan_reset_queries() -> Dict[str, str]:
        return {
            "audit_calls": """SELECT id,run_id,purpose,model,request_json,
                                      raw_response,parsed_json,accepted,attempts,
                                      elapsed_ms,error,archive_relative_path,
                                      archive_offset,archive_compressed_length,
                                      archive_sha256,created_at
                               FROM audit_calls
                               WHERE id IN (SELECT id FROM reset_scan_audit_calls)
                               ORDER BY id""",
            "runs": "SELECT id,stage,status,started_at,finished_at FROM runs WHERE id IN (SELECT id FROM reset_scan_runs) ORDER BY id",
            "blocks_reset": "SELECT id,status,last_error,updated_at FROM blocks WHERE id IN (SELECT id FROM reset_scan_blocks) ORDER BY id",
            "lexical_candidates": "SELECT id,updated_at,resolution_status,selected FROM lexical_candidates WHERE id IN (SELECT id FROM reset_scan_lexical_candidates) ORDER BY id",
            "candidate_clusters": "SELECT id,run_id,updated_at FROM candidate_clusters WHERE id IN (SELECT id FROM reset_scan_candidate_clusters) ORDER BY id",
            "candidate_cluster_members": """SELECT cluster_id,candidate_id,role
                FROM candidate_cluster_members m WHERE EXISTS(
                    SELECT 1 FROM reset_scan_candidate_cluster_members r
                    WHERE r.cluster_id=m.cluster_id AND r.candidate_id=m.candidate_id)
                ORDER BY cluster_id,candidate_id""",
            "candidate_adjudications": "SELECT id,active,payload_hash,updated_at FROM candidate_adjudications WHERE id IN (SELECT id FROM reset_scan_candidate_adjudications) ORDER BY id",
            "candidate_resolutions": "SELECT id,adjudication_id,decision FROM candidate_resolutions WHERE id IN (SELECT id FROM reset_scan_candidate_resolutions) ORDER BY id",
            "concept_type_observations": "SELECT id,lexeme_id,mention_id,evidence_id,adjudication_id FROM concept_type_observations WHERE id IN (SELECT id FROM reset_scan_type_observations) ORDER BY id",
            "usage_decisions": "SELECT id,mention_id,status,locked,created_at FROM usage_decisions WHERE id IN (SELECT id FROM reset_scan_usage_decisions) ORDER BY id",
            "mentions": "SELECT id,evidence_id,concept_id FROM mentions WHERE id IN (SELECT id FROM reset_scan_mentions) ORDER BY id",
            "evidence": "SELECT id,extractor,created_at FROM evidence WHERE id IN (SELECT id FROM reset_scan_evidence) ORDER BY id",
            "claim_evidence": "SELECT claim_id,evidence_id FROM reset_scan_claim_evidence ORDER BY claim_id,evidence_id",
            "claims": "SELECT id,status,created_at FROM claims WHERE id IN (SELECT id FROM reset_scan_claims) ORDER BY id",
            "source_forms": "SELECT id,lexeme_id,normalized_form FROM source_forms WHERE id IN (SELECT id FROM reset_scan_source_forms) ORDER BY id",
            "lexemes": "SELECT id,normalized_form,created_version FROM lexemes WHERE id IN (SELECT id FROM reset_scan_lexemes) ORDER BY id",
            "rendering_rules": "SELECT id,concept_id,status,locked FROM rendering_rules WHERE id IN (SELECT id FROM reset_scan_rendering_rules) ORDER BY id",
            "verification_votes": "SELECT id,task_id,verdict FROM verification_votes WHERE id IN (SELECT id FROM reset_scan_verification_votes) ORDER BY id",
            "verification_tasks": "SELECT id,subject_type,subject_id,status FROM verification_tasks WHERE id IN (SELECT id FROM reset_scan_verification_tasks) ORDER BY id",
            "concepts": "SELECT id,status,retired_version FROM concepts WHERE id IN (SELECT id FROM reset_scan_concepts) ORDER BY id",
        }

    def _scan_reset_preview(
        self, connection: sqlite3.Connection
    ) -> Dict[str, Any]:
        self._prepare_scan_reset_selection(connection)
        queries = self._scan_reset_queries()
        preview: Dict[str, Any] = {}
        row_digests: Dict[str, str] = {}
        for table, query in queries.items():
            row_hasher = hashlib.sha256()
            count = 0
            for row in connection.execute(query):
                count += 1
                row_hasher.update(
                    json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                )
            preview[table] = count
            row_digests[table] = row_hasher.hexdigest()
        retained_queries = {
            "preserved_source_editions": "SELECT COUNT(*) FROM source_editions",
            "preserved_blocks": "SELECT COUNT(*) FROM blocks",
            "preserved_source_paragraphs": "SELECT COUNT(*) FROM source_paragraphs",
            "preserved_baseline_documents": "SELECT COUNT(*) FROM baseline_documents",
            "preserved_baseline_paragraphs": "SELECT COUNT(*) FROM baseline_paragraphs",
            "preserved_block_baseline_links": "SELECT COUNT(*) FROM block_baseline_links",
            "preserved_locked_concepts": """SELECT COUNT(*) FROM concepts
                WHERE locked=1 AND retired_version IS NULL""",
            "preserved_locked_usage_decisions": """SELECT COUNT(*) FROM usage_decisions
                WHERE locked=1 OR status='verified'""",
        }
        preview.update(
            {
                key: int(connection.execute(query).fetchone()[0])
                for key, query in retained_queries.items()
            }
        )
        hasher = hashlib.sha256()
        hasher.update(
            json.dumps(preview, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        for table, digest in row_digests.items():
            hasher.update(table.encode("utf-8"))
            hasher.update(digest.encode("ascii"))
        preview["token"] = hasher.hexdigest()
        return preview

    def preview_scan_reset(self) -> Dict[str, Any]:
        """Return a snapshot-bound destructive-reset preview and confirmation token."""
        with closing(self.connect()) as connection:
            connection.execute("BEGIN")
            try:
                preview = self._scan_reset_preview(connection)
                connection.commit()
                return preview
            except Exception:
                connection.rollback()
                raise

    def reset_scan_derivatives(self, expected_token: str) -> Dict[str, int]:
        """Clear model-derived scan state only when the preview snapshot still matches."""
        if not expected_token:
            raise ValueError("scan reset requires a non-empty preview token")
        with self.transaction() as connection:
            preview = self._scan_reset_preview(connection)
            if preview["token"] != expected_token:
                raise ValueError(
                    "scan reset token mismatch; request a fresh preview before retrying"
                )
            deleted: Dict[str, int] = {}
            connection.execute(
                "DELETE FROM audit_calls WHERE id IN (SELECT id FROM reset_scan_audit_calls)"
            )
            deletion_statements = (
                ("verification_votes", "DELETE FROM verification_votes WHERE id IN (SELECT id FROM reset_scan_verification_votes)"),
                ("verification_tasks", "DELETE FROM verification_tasks WHERE id IN (SELECT id FROM reset_scan_verification_tasks)"),
                ("concept_type_observations", "DELETE FROM concept_type_observations WHERE id IN (SELECT id FROM reset_scan_type_observations)"),
                ("candidate_resolutions", "DELETE FROM candidate_resolutions WHERE id IN (SELECT id FROM reset_scan_candidate_resolutions)"),
                ("candidate_adjudications", "DELETE FROM candidate_adjudications WHERE id IN (SELECT id FROM reset_scan_candidate_adjudications)"),
                ("candidate_cluster_members", """DELETE FROM candidate_cluster_members AS m WHERE EXISTS(
                    SELECT 1 FROM reset_scan_candidate_cluster_members r
                    WHERE r.cluster_id=m.cluster_id AND r.candidate_id=m.candidate_id)"""),
                ("candidate_clusters", "DELETE FROM candidate_clusters WHERE id IN (SELECT id FROM reset_scan_candidate_clusters)"),
                ("usage_decisions", "DELETE FROM usage_decisions WHERE id IN (SELECT id FROM reset_scan_usage_decisions)"),
                ("mentions", "DELETE FROM mentions WHERE id IN (SELECT id FROM reset_scan_mentions)"),
                ("claim_evidence", """DELETE FROM claim_evidence WHERE EXISTS(
                    SELECT 1 FROM reset_scan_claim_evidence r
                    WHERE r.claim_id=claim_evidence.claim_id
                      AND r.evidence_id=claim_evidence.evidence_id)"""),
                ("claims", "DELETE FROM claims WHERE id IN (SELECT id FROM reset_scan_claims)"),
                ("rendering_rules", "DELETE FROM rendering_rules WHERE id IN (SELECT id FROM reset_scan_rendering_rules)"),
                ("source_forms", "DELETE FROM source_forms WHERE id IN (SELECT id FROM reset_scan_source_forms)"),
            )
            for key, statement in deletion_statements:
                deleted[key] = connection.execute(statement).rowcount
            connection.execute(
                """UPDATE mentions SET concept_id=NULL
                   WHERE concept_id IN (SELECT id FROM reset_scan_concepts)"""
            )
            connection.execute(
                """DELETE FROM concept_lexemes
                   WHERE concept_id IN (SELECT id FROM reset_scan_concepts)"""
            )
            deleted["concepts"] = connection.execute(
                "DELETE FROM concepts WHERE id IN (SELECT id FROM reset_scan_concepts)"
            ).rowcount
            deleted["evidence"] = connection.execute(
                "DELETE FROM evidence WHERE id IN (SELECT id FROM reset_scan_evidence)"
            ).rowcount
            deleted["lexemes"] = connection.execute(
                "DELETE FROM lexemes WHERE id IN (SELECT id FROM reset_scan_lexemes)"
            ).rowcount
            deleted["lexical_candidates"] = connection.execute(
                "DELETE FROM lexical_candidates WHERE id IN (SELECT id FROM reset_scan_lexical_candidates)"
            ).rowcount
            deleted["blocks_reset"] = connection.execute(
                """UPDATE blocks SET status=?, last_error=NULL, updated_at=?
                   WHERE id IN (SELECT id FROM reset_scan_blocks)""",
                (
                    V4BlockStatus.PENDING.value,
                    utc_now(),
                ),
            ).rowcount
            connection.execute(
                """DELETE FROM runs
                   WHERE id IN (SELECT id FROM reset_scan_runs)
                     AND NOT EXISTS(SELECT 1 FROM audit_calls a WHERE a.run_id=runs.id)
                     AND NOT EXISTS(SELECT 1 FROM evidence e WHERE e.run_id=runs.id)
                     AND NOT EXISTS(SELECT 1 FROM lexical_candidates l WHERE l.run_id=runs.id)
                     AND NOT EXISTS(SELECT 1 FROM candidate_clusters c
                                    WHERE c.run_id=runs.id OR c.lease_run_id=runs.id)
                     AND NOT EXISTS(SELECT 1 FROM candidate_cluster_members m
                                    WHERE m.run_id=runs.id)
                     AND NOT EXISTS(SELECT 1 FROM candidate_adjudications a
                                    WHERE a.run_id=runs.id)
                     AND NOT EXISTS(SELECT 1 FROM candidate_resolutions r
                                    WHERE r.run_id=runs.id)
                     AND NOT EXISTS(SELECT 1 FROM translation_versions t
                                    WHERE t.run_id=runs.id)"""
            )
            return deleted

    def source_block_count(self) -> int:
        with closing(self.connect()) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM blocks").fetchone()[0])

    def locked_concept_count(self) -> int:
        with closing(self.connect()) as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM concepts WHERE locked=1 AND retired_version IS NULL"
                ).fetchone()[0]
            )

    def reconcile_exact_forms(self, reason: str = "reconcile exact English forms") -> int:
        """Conservative reconciliation: never infer aliases or identities."""
        with self.transaction() as connection:
            rows = connection.execute(
                """SELECT m.id mention_id, m.source_form, m.normalized_form,
                          m.discourse_function, m.lexeme_id,
                          e.payload_json, e.confidence
                   FROM mentions m JOIN evidence e ON e.id=m.evidence_id
                   WHERE m.concept_id IS NULL
                   ORDER BY m.normalized_form, m.id"""
            ).fetchall()
            version = (
                self.create_knowledge_version(reason, connection)
                if rows
                else int(connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0])
            )
            for row in rows:
                payload = json.loads(row["payload_json"])
                concept_id = stable_id("concept", row["normalized_form"])
                connection.execute(
                    """INSERT OR IGNORE INTO concepts(
                           id, kind, canonical_source, default_target, description,
                           status, scope, locked, created_version, created_at
                       ) VALUES(?, ?, ?, ?, ?, 'provisional', 'book', 0, ?, ?)""",
                    (
                        concept_id, payload.get("category", "concept"),
                        payload.get("canonical_form") or row["source_form"],
                        "", payload.get("description", ""),
                        version, utc_now(),
                    ),
                )
                lexeme_id = str(row["lexeme_id"])
                self._associate_schema8_lexeme(
                    connection,
                    lexeme_id,
                    concept_id,
                    knowledge_version=version,
                )
                connection.execute(
                    """INSERT OR IGNORE INTO source_forms(
                           lexeme_id, form, normalized_form, grammar_json
                       ) VALUES(?, ?, ?, '{}')""",
                    (
                        lexeme_id,
                        row["source_form"],
                        normalize_english_form(row["source_form"]),
                    ),
                )
                connection.execute(
                    "UPDATE mentions SET concept_id=? WHERE id=?",
                    (concept_id, row["mention_id"]),
                )
            connection.execute(
                "UPDATE blocks SET status=?, updated_at=? WHERE status=?",
                (V4BlockStatus.READY.value, utc_now(), V4BlockStatus.SCANNED.value),
            )
            concept_rows = connection.execute(
                """SELECT c.id, c.canonical_source, c.default_target, c.locked,
                          COUNT(DISTINCT m.block_id) affected_blocks,
                          COUNT(DISTINCT CASE
                              WHEN json_extract(e.payload_json, '$.suggested_target') != ''
                              THEN json_extract(e.payload_json, '$.suggested_target') END
                          ) target_count
                   FROM concepts c
                   JOIN mentions m ON m.concept_id=c.id
                   JOIN evidence e ON e.id=m.evidence_id
                   WHERE c.retired_version IS NULL
                   GROUP BY c.id"""
            ).fetchall()
            for concept in concept_rows:
                target_votes = connection.execute(
                    """SELECT json_extract(e.payload_json, '$.suggested_target') target,
                              COUNT(DISTINCT m.block_id) block_count
                       FROM mentions m JOIN evidence e ON e.id=m.evidence_id
                       WHERE m.concept_id=?
                         AND COALESCE(json_extract(e.payload_json, '$.suggested_target'), '')!=''
                       GROUP BY target ORDER BY block_count DESC, target""",
                    (concept["id"],),
                ).fetchall()
                current_target = str(concept["default_target"] or "")
                high_impact = (
                    int(concept["affected_blocks"]) >= 3
                    or int(concept["target_count"]) > 1
                    or bool(concept["locked"])
                )
                if not high_impact or not target_votes:
                    continue
                evidence_rows = connection.execute(
                    """SELECT e.evidence_quote, e.payload_json, b.legacy_id
                       FROM mentions m
                       JOIN evidence e ON e.id=m.evidence_id
                       JOIN blocks b ON b.id=m.block_id
                       WHERE m.concept_id=? ORDER BY b.global_index, e.id LIMIT 20""",
                    (concept["id"],),
                ).fetchall()
                payload = {
                    "source": concept["canonical_source"],
                    "target": current_target,
                    "target_candidates": [
                        {
                            "target": row["target"],
                            "block_count": int(row["block_count"]),
                        }
                        for row in target_votes
                    ],
                    "affected_blocks": int(concept["affected_blocks"]),
                    "target_conflict": int(concept["target_count"]) > 1,
                    "evidence": [dict(row) for row in evidence_rows],
                }
                task_id = stable_id(
                    "verify",
                    f"concept:{concept['id']}:{json.dumps(payload, sort_keys=True)}",
                    length=24,
                )
                connection.execute(
                    """INSERT OR IGNORE INTO verification_tasks(
                           id, subject_type, subject_id, payload_json, status,
                           required_votes, created_at
                       ) VALUES(?, 'concept', ?, ?, 'open', 2, ?)""",
                    (
                        task_id,
                        concept["id"],
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        utc_now(),
                    ),
                )
            return version

    def import_legacy_concept(
        self,
        source: str,
        target: str,
        kind: str,
        description: str,
        status: str = "legacy_provisional",
    ) -> str:
        normalized = normalize_english_form(source)
        concept_id = stable_id("concept", normalized)
        with self.transaction() as connection:
            version = int(connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0])
            connection.execute(
                """INSERT OR IGNORE INTO concepts(
                       id, kind, canonical_source, default_target, description,
                       status, scope, locked, created_version, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, 'book', 0, ?, ?)""",
                (concept_id, kind, source, target, description, status, version, utc_now()),
            )
            lexeme_id = self._ensure_schema8_lexeme(
                connection,
                source,
                normalized_form=normalized,
                concept_id=concept_id,
                knowledge_version=version,
            )
            connection.execute(
                """INSERT OR IGNORE INTO source_forms(
                       lexeme_id, form, normalized_form, grammar_json
                   ) VALUES(?, ?, ?, '{}')""",
                (lexeme_id, source, normalized),
            )
            if target:
                rule_id = stable_id("rule", f"{concept_id}:default:{target}")
                connection.execute(
                    """INSERT OR IGNORE INTO rendering_rules(
                           id, concept_id, condition_json, target, priority, status,
                           scope, locked, created_version, created_at
                       ) VALUES(?, ?, '{}', ?, 0, ?, 'book', 0, ?, ?)""",
                    (rule_id, concept_id, target, status, version, utc_now()),
                )
        return concept_id

    def lock_concept_translation(
        self,
        source: str,
        target: str,
        kind: str = "concept",
        description: str = "",
    ) -> Dict[str, Any]:
        """人工确认一个全书级译名，并使依赖旧译名的V4译文失效。"""
        source = source.strip()
        target = target.strip()
        if not source or not target:
            raise ValueError("人工锁定术语时source和target均不能为空")
        normalized = normalize_english_form(source)
        concept_id = stable_id("concept", normalized)
        with self.transaction() as connection:
            version = self.create_knowledge_version(
                f"human lock concept translation: {source}", connection
            )
            existing = connection.execute(
                "SELECT id FROM concepts WHERE id=? AND retired_version IS NULL",
                (concept_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO concepts(
                           id, kind, canonical_source, default_target,
                           working_target, verified_target, description,
                           status, scope, locked, created_version, created_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, 'verified', 'book', 1, ?, ?)""",
                    (
                        concept_id,
                        kind,
                        source,
                        target,
                        target,
                        target,
                        description,
                        version,
                        utc_now(),
                    ),
                )
            else:
                connection.execute(
                    """UPDATE concepts SET default_target=?, working_target=?,
                           verified_target=?,
                           description=CASE WHEN ?!='' THEN ? ELSE description END,
                           status='verified', locked=1
                        WHERE id=?""",
                    (target, target, target, description, description, concept_id),
                )
            lexeme_id = self._ensure_schema8_lexeme(
                connection,
                source,
                normalized_form=normalized,
                concept_id=concept_id,
                knowledge_version=version,
            )
            connection.execute(
                """INSERT OR IGNORE INTO source_forms(
                       lexeme_id, form, normalized_form, grammar_json
                   ) VALUES(?, ?, ?, '{}')""",
                (lexeme_id, source, normalized),
            )
            connection.execute(
                """UPDATE rendering_rules SET retired_version=?
                   WHERE concept_id=? AND retired_version IS NULL AND target!=?""",
                (version, concept_id, target),
            )
            rule_id = stable_id("rule", f"{concept_id}:human-default:{target}")
            connection.execute(
                """INSERT INTO rendering_rules(
                       id, concept_id, condition_json, target, priority, status,
                       scope, locked, created_version, retired_version, created_at
                   ) VALUES(?, ?, '{}', ?, 100, 'verified', 'book', 1, ?, NULL, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       target=excluded.target, priority=100, status='verified',
                       scope='book', locked=1, retired_version=NULL""",
                (rule_id, concept_id, target, version, utc_now()),
            )
            connection.execute(
                """UPDATE verification_tasks
                   SET status='resolved', resolved_at=?
                   WHERE subject_type='concept' AND subject_id=?
                     AND status IN ('open','needs_human')""",
                (utc_now(), concept_id),
            )
            affected_rows = connection.execute(
                """SELECT DISTINCT tv.id translation_id, tv.block_id
                   FROM dependencies d
                   JOIN translation_versions tv ON tv.id=d.translation_id
                   WHERE d.dependency_type='concept' AND d.dependency_id=?
                     AND tv.active=1 AND tv.pipeline='parallel_v4'""",
                (concept_id,),
            ).fetchall()
            for row in affected_rows:
                connection.execute(
                    "UPDATE translation_versions SET status=? WHERE id=?",
                    (V4BlockStatus.NEEDS_REVALIDATE.value, row["translation_id"]),
                )
                connection.execute(
                    "UPDATE blocks SET status=?, updated_at=? WHERE id=?",
                    (V4BlockStatus.NEEDS_REVALIDATE.value, utc_now(), row["block_id"]),
                )
        return {
            "concept_id": concept_id,
            "source": source,
            "target": target,
            "knowledge_version": version,
            "affected_translations": len(affected_rows),
        }

    def merge_concept_forms(
        self,
        canonical_source: str,
        aliases: Sequence[str],
    ) -> Dict[str, Any]:
        """Human-confirmed merge of inflections or spelling variants.

        Automatic reconciliation intentionally remains exact-only.  This method
        supplies the explicit human decision needed to join forms such as a
        singular and plural without teaching the scanner unsafe alias guesses.
        """
        canonical_source = canonical_source.strip()
        alias_values = [value.strip() for value in aliases if value.strip()]
        if not canonical_source or not alias_values:
            raise ValueError("合并概念词形时必须提供核心词形和至少一个别名")
        canonical_id = stable_id(
            "concept", normalize_english_form(canonical_source)
        )
        with self.transaction() as connection:
            canonical = connection.execute(
                """SELECT * FROM concepts
                   WHERE id=? AND retired_version IS NULL""",
                (canonical_id,),
            ).fetchone()
            if canonical is None:
                raise KeyError(f"核心概念不存在: {canonical_source}")
            version = self.create_knowledge_version(
                f"human merge concept forms: {canonical_source}", connection
            )
            merged_ids: List[str] = []
            affected_translation_ids: set[int] = set()
            for alias in dict.fromkeys(alias_values):
                normalized_alias = normalize_english_form(alias)
                alias_id = stable_id("concept", normalized_alias)
                alias_lexeme_id = self._ensure_schema8_lexeme(
                    connection,
                    alias,
                    normalized_form=normalized_alias,
                    concept_id=canonical_id,
                    knowledge_version=version,
                )
                connection.execute(
                    """INSERT OR IGNORE INTO source_forms(
                           lexeme_id, form, normalized_form, grammar_json
                       ) VALUES(?, ?, ?, '{}')""",
                    (alias_lexeme_id, alias, normalized_alias),
                )
                if alias_id == canonical_id:
                    continue
                alias_concept = connection.execute(
                    """SELECT id FROM concepts
                       WHERE id=? AND retired_version IS NULL""",
                    (alias_id,),
                ).fetchone()
                if alias_concept is None:
                    continue
                for form in connection.execute(
                    """SELECT sf.lexeme_id, sf.form, sf.normalized_form,
                              sf.grammar_json
                       FROM source_forms sf
                       JOIN concept_lexemes cl ON cl.lexeme_id=sf.lexeme_id
                       WHERE cl.concept_id=? AND cl.retired_version IS NULL""",
                    (alias_id,),
                ).fetchall():
                    connection.execute(
                        """INSERT OR IGNORE INTO concept_lexemes(
                               concept_id, lexeme_id, role, confidence, status,
                               created_version, created_at)
                           VALUES(?, ?, 'alias', 1.0, 'verified', ?, ?)""",
                        (
                            canonical_id,
                            form["lexeme_id"],
                            version,
                            utc_now(),
                        ),
                    )
                dependent_rows = connection.execute(
                    """SELECT DISTINCT translation_id FROM dependencies
                       WHERE dependency_type='concept' AND dependency_id=?""",
                    (alias_id,),
                ).fetchall()
                for dependent in dependent_rows:
                    translation_id = int(dependent["translation_id"])
                    affected_translation_ids.add(translation_id)
                    connection.execute(
                        """INSERT OR IGNORE INTO dependencies(
                               translation_id, dependency_type, dependency_id,
                               knowledge_version
                           ) SELECT translation_id, dependency_type, ?, ?
                             FROM dependencies
                            WHERE translation_id=? AND dependency_type='concept'
                              AND dependency_id=?""",
                        (
                            canonical_id,
                            version,
                            translation_id,
                            alias_id,
                        ),
                    )
                connection.execute(
                    "DELETE FROM dependencies WHERE dependency_type='concept' AND dependency_id=?",
                    (alias_id,),
                )
                connection.execute(
                    "UPDATE mentions SET concept_id=? WHERE concept_id=?",
                    (canonical_id, alias_id),
                )
                connection.execute(
                    """UPDATE concept_lexemes SET retired_version=?
                       WHERE concept_id=? AND retired_version IS NULL""",
                    (version, alias_id),
                )
                connection.execute(
                    """UPDATE rendering_rules SET retired_version=?
                       WHERE concept_id=? AND retired_version IS NULL""",
                    (version, alias_id),
                )
                connection.execute(
                    """UPDATE concepts SET status='merged', retired_version=?
                       WHERE id=?""",
                    (version, alias_id),
                )
                connection.execute(
                    """UPDATE verification_tasks
                       SET status='resolved', resolved_at=?
                       WHERE subject_type='concept' AND subject_id=?
                         AND status IN ('open','needs_human')""",
                    (utc_now(), alias_id),
                )
                merged_ids.append(alias_id)
            if affected_translation_ids:
                placeholders = ",".join("?" for _ in affected_translation_ids)
                rows = connection.execute(
                    f"""SELECT id, block_id FROM translation_versions
                        WHERE id IN ({placeholders}) AND active=1
                          AND pipeline='parallel_v4'""",
                    list(affected_translation_ids),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        "UPDATE translation_versions SET status=? WHERE id=?",
                        (V4BlockStatus.NEEDS_REVALIDATE.value, row["id"]),
                    )
                    connection.execute(
                        "UPDATE blocks SET status=?, updated_at=? WHERE id=?",
                        (
                            V4BlockStatus.NEEDS_REVALIDATE.value,
                            utc_now(),
                            row["block_id"],
                        ),
                    )
        return {
            "canonical_id": canonical_id,
            "canonical_source": canonical_source,
            "aliases": list(dict.fromkeys(alias_values)),
            "merged_concept_ids": merged_ids,
            "knowledge_version": version,
            "affected_translations": len(affected_translation_ids),
        }

    def import_legacy_translation(
        self,
        block_id: str,
        status: str,
        draft: str,
        final: str,
        analysis: str,
        warnings: Sequence[str],
    ) -> None:
        if not final:
            return
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT 1 FROM translation_versions
                   WHERE block_id=? AND pipeline='serial_v3' AND active=1""",
                (block_id,),
            ).fetchone()
            if existing:
                return
            version = int(connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0])
            connection.execute(
                """INSERT INTO translation_versions(
                       block_id, pipeline, knowledge_version, status, draft_translation,
                       final_translation, analysis, warnings_json, active, created_at
                   ) VALUES(?, 'serial_v3', ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    block_id, version, status, draft, final,
                    analysis, json.dumps(list(warnings), ensure_ascii=False), utc_now(),
                ),
            )

    @staticmethod
    def _representative_paragraph(source_text: str, source_form: str) -> str:
        import re

        paragraphs = [
            part.strip()
            for part in re.split(r"\n\s*\n", source_text or "")
            if part.strip()
        ]
        for paragraph in paragraphs:
            if re.search(rf"(?<!\w){re.escape(source_form)}(?!\w)", paragraph, re.I):
                return paragraph[:1200]
        return (paragraphs[0] if paragraphs else source_text)[:1200]

    def working_target_candidates(self) -> List[Dict[str, Any]]:
        """Return active, targetless concepts that cannot safely remain unnamed."""

        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT c.id, c.kind, c.canonical_source, c.description,
                          c.default_target, c.working_target, c.verified_target,
                          c.status, c.locked,
                          COUNT(m.id) mention_count,
                          COUNT(DISTINCT m.block_id) mentioned_blocks,
                          COALESCE((
                              SELECT MAX(cc.affected_block_count)
                              FROM candidate_resolutions cr
                              JOIN candidate_adjudications ca
                                ON ca.id=cr.adjudication_id AND ca.active=1
                              JOIN candidate_clusters cc ON cc.id=cr.cluster_id
                              WHERE cr.concept_id=c.id
                          ), 0) cluster_blocks
                   FROM concepts c
                   LEFT JOIN mentions m ON m.concept_id=c.id
                   WHERE c.retired_version IS NULL
                   GROUP BY c.id
                   ORDER BY lower(c.canonical_source), c.id"""
            ).fetchall()
            candidates: List[Dict[str, Any]] = []
            for row in rows:
                working = str(row["working_target"] or "").strip()
                verified = str(row["verified_target"] or "").strip()
                effective = verified or working
                if verified or bool(row["locked"]):
                    continue
                affected_blocks = max(
                    int(row["mentioned_blocks"] or 0),
                    int(row["cluster_blocks"] or 0),
                )
                repeated = int(row["mention_count"] or 0) >= 2 or affected_blocks >= 2
                identity_kind = str(row["kind"] or "concept") in {
                    "person", "place", "organization", "group", "unit"
                }
                high_impact = affected_blocks >= 3
                required = (identity_kind and repeated) or high_impact
                if effective or not required:
                    continue
                candidates.append(
                    {
                        "concept_id": str(row["id"]),
                        "source": str(row["canonical_source"]),
                        "kind": str(row["kind"]),
                        "description": str(row["description"] or ""),
                        "contexts": [],
                        "context_block_ids": [],
                        "affected_blocks": affected_blocks,
                        "high_impact": high_impact,
                    }
                )
            if not candidates:
                return []

            concept_ids = [item["concept_id"] for item in candidates]
            placeholders = ",".join("?" for _ in concept_ids)
            context_rows = connection.execute(
                f"""WITH ranked AS (
                           SELECT m.concept_id, b.id block_id, b.source_text,
                                  b.global_index,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY m.concept_id
                                      ORDER BY b.global_index, b.id
                                  ) occurrence_rank
                           FROM mentions m JOIN blocks b ON b.id=m.block_id
                           WHERE m.concept_id IN ({placeholders})
                           GROUP BY m.concept_id, b.id
                       )
                       SELECT concept_id, block_id, source_text, global_index
                       FROM ranked WHERE occurrence_rank<=3
                       ORDER BY concept_id, global_index, block_id""",
                concept_ids,
            ).fetchall()
            candidates_by_id = {
                item["concept_id"]: item for item in candidates
            }
            for row in context_rows:
                candidate = candidates_by_id[str(row["concept_id"])]
                candidate["context_block_ids"].append(str(row["block_id"]))
                candidate["contexts"].append(
                    self._representative_paragraph(
                        str(row["source_text"]), candidate["source"]
                    )
                )

            block_ids = list(
                dict.fromkeys(
                    block_id
                    for item in candidates
                    for block_id in item["context_block_ids"]
                )
            )
            references: Dict[str, str] = {}
            if block_ids:
                block_placeholders = ",".join("?" for _ in block_ids)
                baseline_rows = connection.execute(
                    f"""SELECT l.block_id, d.id document_id, d.name,
                               d.created_at, p.target_text, l.ordinal,
                               l.partial_start, l.partial_end, l.alignment_status
                        FROM block_baseline_links l
                        JOIN baseline_documents d
                          ON d.id=l.baseline_document_id AND d.active=1
                        JOIN baseline_paragraphs p
                          ON p.baseline_document_id=l.baseline_document_id
                         AND p.paragraph_index=l.paragraph_index
                        WHERE l.block_id IN ({block_placeholders})
                        ORDER BY l.block_id, d.created_at DESC, d.id, l.ordinal""",
                    block_ids,
                ).fetchall()
                documents: Dict[tuple[str, str], Dict[str, Any]] = {}
                document_order: Dict[str, List[str]] = {}
                for row in baseline_rows:
                    block_id = str(row["block_id"])
                    document_id = str(row["document_id"])
                    key = (block_id, document_id)
                    if key not in documents:
                        documents[key] = {"parts": [], "exact": True}
                        document_order.setdefault(block_id, []).append(document_id)
                    documents[key]["parts"].append(str(row["target_text"] or ""))
                    if (
                        bool(row["partial_start"])
                        or bool(row["partial_end"])
                        or str(row["alignment_status"]) != "exact"
                    ):
                        documents[key]["exact"] = False
                for block_id, ordered_documents in document_order.items():
                    for document_id in ordered_documents:
                        document = documents[(block_id, document_id)]
                        text = "\n\n".join(document["parts"]).strip()
                        if document["exact"] and text:
                            references[block_id] = text
                            break

                missing = [item for item in block_ids if item not in references]
                if missing:
                    missing_placeholders = ",".join("?" for _ in missing)
                    for row in connection.execute(
                        f"""SELECT block_id, final_translation FROM (
                                   SELECT block_id, final_translation,
                                          ROW_NUMBER() OVER (
                                              PARTITION BY block_id ORDER BY id DESC
                                          ) version_rank
                                   FROM translation_versions
                                   WHERE block_id IN ({missing_placeholders})
                                     AND pipeline='serial_v3' AND active=1
                                     AND final_translation!=''
                               ) WHERE version_rank=1""",
                        missing,
                    ).fetchall():
                        references[str(row["block_id"])] = str(
                            row["final_translation"]
                        ).strip()

            for candidate in candidates:
                baselines: List[str] = []
                for block_id in candidate.pop("context_block_ids"):
                    text = references.get(block_id, "").strip()
                    if text and text not in baselines:
                        baselines.append(text[:1600])
                    if len(baselines) >= 2:
                        break
                candidate["baseline_translations"] = baselines
            return candidates

    def enqueue_working_target_review(
        self, concept_ids: Sequence[str], error: str
    ) -> int:
        queued = 0
        with self.transaction() as connection:
            for concept_id in dict.fromkeys(str(value) for value in concept_ids):
                exists = connection.execute(
                    """SELECT 1 FROM human_queue
                       WHERE kind='working_target_required' AND status='open'
                         AND json_extract(payload_json, '$.concept_id')=?""",
                    (concept_id,),
                ).fetchone()
                if exists:
                    continue
                concept = connection.execute(
                    """SELECT canonical_source, kind FROM concepts
                       WHERE id=? AND retired_version IS NULL""",
                    (concept_id,),
                ).fetchone()
                if concept is None:
                    continue
                payload = {
                    "concept_id": concept_id,
                    "source": concept["canonical_source"],
                    "kind": concept["kind"],
                    "error": error[:1000],
                }
                connection.execute(
                    """INSERT INTO human_queue(
                           block_id, kind, severity, payload_json, created_at
                       ) VALUES(NULL, 'working_target_required', 'blocking', ?, ?)""",
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True), utc_now()),
                )
                queued += 1
        return queued

    @staticmethod
    def _normalized_working_rules(rules: Sequence[Dict[str, Any]]) -> tuple[str, ...]:
        normalized = []
        for rule in rules:
            condition = rule.get("condition") or {}
            target = str(rule.get("target") or "").strip()
            normalized.append(
                json.dumps(
                    {"condition": condition, "target": target},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        return tuple(sorted(normalized))

    def _invalidate_working_target_dependents(
        self,
        connection: sqlite3.Connection,
        concept_ids: Sequence[str],
    ) -> int:
        import re

        affected_blocks: set[str] = set()
        for concept_id in concept_ids:
            affected_blocks.update(
                str(row["block_id"])
                for row in connection.execute(
                    """SELECT DISTINCT tv.block_id
                       FROM dependencies d
                       JOIN translation_versions tv ON tv.id=d.translation_id
                       WHERE d.dependency_type='concept' AND d.dependency_id=?""",
                    (concept_id,),
                ).fetchall()
            )
            forms = [
                str(row["form"])
                for row in connection.execute(
                    """SELECT DISTINCT sf.form FROM source_forms sf
                       JOIN concept_lexemes cl ON cl.lexeme_id=sf.lexeme_id
                       WHERE cl.concept_id=? AND cl.retired_version IS NULL""",
                    (concept_id,),
                ).fetchall()
                if str(row["form"] or "").strip()
            ]
            if not forms:
                continue
            patterns = [
                re.compile(rf"(?<!\w){re.escape(form)}(?!\w)", re.I)
                for form in forms
            ]
            for block in connection.execute(
                """SELECT id, source_text, status FROM blocks
                   WHERE source_edition_id=(
                       SELECT id FROM source_editions WHERE active=1
                   )"""
            ).fetchall():
                if block["status"] not in {
                    V4BlockStatus.READY.value,
                    V4BlockStatus.TRANSLATING.value,
                }:
                    continue
                if any(pattern.search(str(block["source_text"])) for pattern in patterns):
                    affected_blocks.add(str(block["id"]))

        for block_id in sorted(affected_blocks):
            connection.execute(
                """UPDATE translation_versions SET status=?
                   WHERE block_id=? AND pipeline='parallel_v4' AND active=1""",
                (V4BlockStatus.NEEDS_REVALIDATE.value, block_id),
            )
            connection.execute(
                "UPDATE blocks SET status=?, updated_at=? WHERE id=?",
                (V4BlockStatus.NEEDS_REVALIDATE.value, utc_now(), block_id),
            )
        return len(affected_blocks)

    def apply_working_target_decisions(
        self, decisions: Sequence[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Atomically install provisional translations without touching human locks."""

        validated: List[Dict[str, Any]] = []
        for decision in decisions:
            raw_rules = list(decision.get("rules") or [])
            if len(raw_rules) > 6:
                raise ValueError("working targets allow at most six rendering rules")
            item = dict(decision)
            item["rules"] = [
                WorkingTargetRule.model_validate(rule).model_dump()
                for rule in raw_rules
            ]
            validated.append(item)
        ordered = sorted(
            validated, key=lambda item: str(item.get("concept_id") or "")
        )
        with self.transaction() as connection:
            changed: List[Dict[str, Any]] = []
            processed_ids: List[str] = []
            resolved = 0
            for decision in ordered:
                concept_id = str(decision.get("concept_id") or "")
                target = str(decision.get("target") or "").strip()
                rules = list(decision.get("rules") or [])
                if not concept_id or not target:
                    raise ValueError("working target decisions require concept_id and target")
                concept = connection.execute(
                    """SELECT id, working_target, verified_target, locked
                       FROM concepts WHERE id=? AND retired_version IS NULL""",
                    (concept_id,),
                ).fetchone()
                if concept is None:
                    raise KeyError(f"active concept not found: {concept_id}")
                if bool(concept["locked"]) or str(concept["verified_target"] or "").strip():
                    continue
                resolved += 1
                processed_ids.append(concept_id)
                existing_rules = [
                    {
                        "condition": json.loads(row["condition_json"]),
                        "target": row["target"],
                    }
                    for row in connection.execute(
                        """SELECT condition_json, target FROM rendering_rules
                           WHERE concept_id=? AND retired_version IS NULL
                             AND locked=0 AND status='provisional'""",
                        (concept_id,),
                    ).fetchall()
                ]
                if (
                    str(concept["working_target"] or "") == target
                    and self._normalized_working_rules(existing_rules)
                    == self._normalized_working_rules(rules)
                ):
                    continue
                changed.append(
                    {"concept_id": concept_id, "target": target, "rules": rules}
                )

            resolved_at = utc_now()
            for concept_id in processed_ids:
                connection.execute(
                    """UPDATE human_queue
                       SET status='resolved', resolved_at=?
                       WHERE kind='working_target_required' AND status='open'
                         AND json_extract(payload_json, '$.concept_id')=?""",
                    (resolved_at, concept_id),
                )

            if not changed:
                return {
                    "resolved": resolved,
                    "changed": 0,
                    "knowledge_version": None,
                    "affected_blocks": 0,
                }
            version = self.create_knowledge_version(
                "resolve provisional working translations", connection
            )
            for decision in changed:
                concept_id = decision["concept_id"]
                connection.execute(
                    """UPDATE concepts
                       SET working_target=?, default_target=?
                       WHERE id=? AND locked=0 AND verified_target=''""",
                    (decision["target"], decision["target"], concept_id),
                )
                connection.execute(
                    """UPDATE rendering_rules SET retired_version=?
                       WHERE concept_id=? AND retired_version IS NULL AND locked=0""",
                    (version, concept_id),
                )
                for ordinal, rule in enumerate(decision["rules"]):
                    condition = rule.get("condition") or {}
                    target = str(rule.get("target") or "").strip()
                    if not condition or not target:
                        raise ValueError("working rendering rules cannot be empty")
                    condition_json = json.dumps(
                        condition, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    rule_id = stable_id(
                        "rule",
                        f"working:{concept_id}:{version}:{ordinal}:{condition_json}:{target}",
                        length=24,
                    )
                    connection.execute(
                        """INSERT INTO rendering_rules(
                               id, concept_id, condition_json, target, priority,
                               status, scope, locked, created_version, created_at
                           ) VALUES(?, ?, ?, ?, ?, 'provisional', 'book', 0, ?, ?)""",
                        (
                            rule_id,
                            concept_id,
                            condition_json,
                            target,
                            50 - ordinal,
                            version,
                            utc_now(),
                        ),
                    )
            affected = self._invalidate_working_target_dependents(
                connection, [item["concept_id"] for item in changed]
            )
            return {
                "resolved": resolved,
                "changed": len(changed),
                "knowledge_version": version,
                "affected_blocks": affected,
            }

    def _concept_snapshot_from_connection(
        self, connection: sqlite3.Connection
    ) -> List[Dict[str, Any]]:
        rows = connection.execute(
            """SELECT c.id, c.kind, c.canonical_source, c.default_target,
                      c.working_target, c.verified_target, c.description,
                      c.status, c.locked, sf.form, sf.normalized_form
               FROM concepts c
               JOIN concept_lexemes cl
                 ON cl.concept_id=c.id AND cl.retired_version IS NULL
               JOIN source_forms sf ON sf.lexeme_id=cl.lexeme_id
               JOIN knowledge_versions kv ON kv.id=c.created_version
               WHERE c.retired_version IS NULL
                 AND NOT (
                     kv.reason LIKE 'translation proposals %'
                     AND c.status='provisional' AND c.locked=0
                     AND c.working_target='' AND c.verified_target=''
                 )
               ORDER BY lower(c.canonical_source), c.id, lower(sf.form), sf.id"""
        ).fetchall()
        concepts: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            working = str(row["working_target"] or "").strip()
            verified = str(row["verified_target"] or "").strip()
            locked_fallback = (
                str(row["default_target"] or "").strip()
                if bool(row["locked"])
                else ""
            )
            effective = verified or working or locked_fallback
            item = concepts.setdefault(
                str(row["id"]),
                {
                    "id": str(row["id"]),
                    "kind": str(row["kind"]),
                    "source": str(row["canonical_source"]),
                    "working_target": working,
                    "verified_target": verified,
                    "default_target": effective,
                    "target_strength": (
                        "verified"
                        if verified or (bool(row["locked"]) and effective)
                        else "working" if working else "unset"
                    ),
                    "description": str(row["description"] or ""),
                    "status": str(row["status"]),
                    "locked": bool(row["locked"]),
                    "forms": [],
                    "rules": [],
                    "verification_pending": False,
                },
            )
            item["forms"].append(str(row["form"]))
        if not concepts:
            return []
        placeholders = ",".join("?" for _ in concepts)
        for pending in connection.execute(
            f"""SELECT DISTINCT subject_id FROM verification_tasks
                WHERE subject_type='concept' AND status IN ('open','needs_human')
                  AND subject_id IN ({placeholders})""",
            list(concepts),
        ).fetchall():
            concept = concepts[str(pending["subject_id"])]
            if not concept["locked"]:
                concept["verification_pending"] = True
        for row in connection.execute(
            f"""SELECT id, concept_id, condition_json, target, priority,
                       status, locked
                FROM rendering_rules
                WHERE retired_version IS NULL AND concept_id IN ({placeholders})
                ORDER BY priority DESC, id""",
            list(concepts),
        ).fetchall():
            target = str(row["target"] or "").strip()
            if not target:
                continue
            rule_id = str(row["id"])
            try:
                condition = json.loads(row["condition_json"])
            except (json.JSONDecodeError, TypeError) as exc:
                raise KnowledgeSnapshotError(rule_id, str(exc)) from exc
            if not isinstance(condition, dict):
                raise KnowledgeSnapshotError(
                    rule_id, "condition_json must decode to an object"
                )
            concepts[str(row["concept_id"])]["rules"].append(
                {
                    "id": rule_id,
                    "condition": condition,
                    "target": target,
                    "priority": int(row["priority"]),
                    "status": str(row["status"]),
                    "locked": bool(row["locked"]),
                }
            )
        return list(concepts.values())

    def concept_snapshot(self) -> List[Dict[str, Any]]:
        """Materialize active rendering state from one SQLite read snapshot."""

        connection = self.connect()
        try:
            connection.execute("BEGIN")
            return self._concept_snapshot_from_connection(connection)
        finally:
            connection.rollback()
            connection.close()

    def freeze_translation_knowledge(
        self,
    ) -> tuple[int, FrozenConceptIndex, str]:
        """Atomically freeze the version, concept rendering state, and signature."""

        connection = self.connect()
        try:
            connection.execute("BEGIN")
            version = int(
                connection.execute(
                    "SELECT MAX(id) FROM knowledge_versions"
                ).fetchone()[0]
            )
            snapshot = self._concept_snapshot_from_connection(connection)
            frozen = FrozenConceptIndex.compile(
                snapshot, self.target_snapshot_signature
            )
            return version, frozen, frozen.signature
        finally:
            connection.rollback()
            connection.close()

    @staticmethod
    def target_snapshot_signature(snapshot: Sequence[Dict[str, Any]]) -> str:
        payload: List[Dict[str, Any]] = []
        for concept in sorted(snapshot, key=lambda item: str(item.get("id") or "")):
            rules = []
            for rule in concept.get("rules", []):
                rules.append(
                    {
                        "id": str(rule.get("id") or ""),
                        "condition": deepcopy(rule.get("condition") or {}),
                        "target": str(rule.get("target") or "").strip(),
                        "priority": int(rule.get("priority") or 0),
                        "status": str(rule.get("status") or ""),
                        "locked": bool(rule.get("locked")),
                    }
                )
            rules.sort(
                key=lambda item: (
                    item["id"],
                    -item["priority"],
                    json.dumps(
                        item["condition"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    item["target"],
                )
            )
            payload.append(
                {
                    "id": str(concept.get("id") or ""),
                    "source": str(concept.get("source") or ""),
                    "forms": sorted(
                        {
                            str(form)
                            for form in concept.get("forms", [])
                            if str(form)
                        },
                        key=lambda value: (value.casefold(), value),
                    ),
                    "kind": str(concept.get("kind") or ""),
                    "description": str(concept.get("description") or ""),
                    "status": str(concept.get("status") or ""),
                    "locked": bool(concept.get("locked")),
                    "working_target": str(concept.get("working_target") or ""),
                    "verified_target": str(concept.get("verified_target") or ""),
                    "default_target": str(concept.get("default_target") or ""),
                    "target_strength": str(
                        concept.get("target_strength") or "unset"
                    ),
                    "verification_pending": bool(
                        concept.get("verification_pending")
                    ),
                    "rules": rules,
                }
            )
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def concepts_for_text(
        self,
        text: str,
        concept_snapshot: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        if isinstance(concept_snapshot, FrozenConceptIndex):
            return concept_snapshot.matched_concepts(text)
        snapshot = (
            list(concept_snapshot)
            if concept_snapshot is not None
            else self.concept_snapshot()
        )
        signature = self.target_snapshot_signature(snapshot)
        matcher = ConceptMatcherCache.get(signature, snapshot)
        by_id = {str(concept.get("id") or ""): concept for concept in snapshot}
        return [
            deepcopy(by_id[concept_id])
            for concept_id in matcher.match(text)
            if concept_id in by_id
        ]

    def finish_translation_run_atomically(
        self,
        run_id: str,
        expected_signature: str,
        desired_status: str,
        error: Optional[str] = None,
        force_revalidate: bool = False,
    ) -> tuple[str, bool]:
        """Finalize a translation run against knowledge read in the same write txn."""

        with self.transaction() as connection:
            snapshot = self._concept_snapshot_from_connection(connection)
            current_signature = self.target_snapshot_signature(snapshot)
            stale = force_revalidate or current_signature != expected_signature
            persisted_status = (
                "completed_with_errors" if stale else desired_status
            )
            persisted_error = error
            if stale:
                persisted_error = persisted_error or "frozen knowledge changed during run"
                rows = connection.execute(
                    """SELECT id, block_id FROM translation_versions
                       WHERE run_id=? AND pipeline='parallel_v4' AND active=1""",
                    (run_id,),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        "UPDATE translation_versions SET status=? WHERE id=?",
                        (V4BlockStatus.NEEDS_REVALIDATE.value, row["id"]),
                    )
                    connection.execute(
                        "UPDATE blocks SET status=?, updated_at=? WHERE id=?",
                        (
                            V4BlockStatus.NEEDS_REVALIDATE.value,
                            utc_now(),
                            row["block_id"],
                        ),
                    )
            connection.execute(
                "UPDATE runs SET status=?, finished_at=?, error=? WHERE id=?",
                (persisted_status, utc_now(), persisted_error, run_id),
            )
            return persisted_status, stale

    def invalidate_translation_run(self, run_id: str) -> int:
        """Mark all output already committed by a stale frozen run for revalidation."""

        with self.transaction() as connection:
            rows = connection.execute(
                """SELECT id, block_id FROM translation_versions
                   WHERE run_id=? AND pipeline='parallel_v4' AND active=1""",
                (run_id,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE translation_versions SET status=? WHERE id=?",
                    (V4BlockStatus.NEEDS_REVALIDATE.value, row["id"]),
                )
                connection.execute(
                    "UPDATE blocks SET status=?, updated_at=? WHERE id=?",
                    (V4BlockStatus.NEEDS_REVALIDATE.value, utc_now(), row["block_id"]),
                )
            return len(rows)

    def prior_concept_source_evidence(
        self,
        block: V4Block,
        concepts: Sequence[Dict[str, Any]],
        max_chars: int = 3600,
        max_paragraphs_per_concept: int = 2,
    ) -> List[Dict[str, Any]]:
        """Retrieve grounded prior paragraphs for concepts used by the current block.

        The earliest occurrence often defines a coined term; the latest occurrence
        shows its current use.  Keeping both is more reliable than asking a rolling
        plot summary to preserve every world-mechanism detail.
        """
        import re

        if not concepts or max_chars <= 0 or max_paragraphs_per_concept <= 0:
            return []
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT legacy_id, global_index, source_text
                   FROM blocks
                   WHERE source_edition_id=? AND global_index<?
                   ORDER BY global_index""",
                (block.source_edition_id, block.global_index),
            ).fetchall()

        results: List[Dict[str, Any]] = []
        seen_paragraphs: set[tuple[int, int]] = set()
        used_chars = 0
        for concept in concepts:
            forms = sorted(
                {str(form).strip() for form in concept.get("forms", []) if str(form).strip()},
                key=len,
                reverse=True,
            )
            if not forms:
                continue
            patterns = [
                re.compile(rf"(?<!\w){re.escape(form)}(?!\w)", re.IGNORECASE)
                for form in forms
            ]
            matches: List[Dict[str, Any]] = []
            for row in rows:
                paragraphs = [
                    part.strip()
                    for part in re.split(r"\n\s*\n", row["source_text"].strip())
                    if part.strip()
                ]
                for paragraph_index, paragraph in enumerate(paragraphs):
                    if any(pattern.search(paragraph) for pattern in patterns):
                        matches.append(
                            {
                                "concept_id": concept["id"],
                                "concept_source": concept["source"],
                                "legacy_id": row["legacy_id"],
                                "global_index": int(row["global_index"]),
                                "paragraph_id": f"P{paragraph_index:03d}",
                                "source_text": paragraph,
                            }
                        )
            if not matches:
                continue
            selected = [matches[0]]
            for candidate in reversed(matches[1:]):
                if len(selected) >= max_paragraphs_per_concept:
                    break
                selected.append(candidate)
            selected.sort(key=lambda item: (item["global_index"], item["paragraph_id"]))
            for item in selected:
                paragraph_key = (item["global_index"], int(item["paragraph_id"][1:]))
                if paragraph_key in seen_paragraphs:
                    continue
                added_chars = len(item["source_text"])
                if results and used_chars + added_chars > max_chars:
                    return results
                results.append(item)
                seen_paragraphs.add(paragraph_key)
                used_chars += added_chars
        return results

    def commit_translation_batch(
        self,
        run_id: str,
        outcomes: Sequence[TranslationOutcome],
        pipeline: str = "parallel_v4",
        audit_mode: str = "full",
    ) -> None:
        ordered = sorted(outcomes, key=lambda item: item.block.global_index)
        with self.transaction() as connection:
            for outcome in ordered:
                if pipeline == "parallel_v4":
                    previous_vote = connection.execute(
                        "SELECT * FROM comparison_votes WHERE block_id=?",
                        (outcome.block.id,),
                    ).fetchone()
                    if previous_vote is not None:
                        self._archive_comparison_vote(
                            connection, previous_vote, utc_now()
                        )
                connection.execute(
                    "UPDATE translation_versions SET active=0 WHERE block_id=? AND pipeline=? AND active=1",
                    (outcome.block.id, pipeline),
                )
                cursor = connection.execute(
                    """INSERT INTO translation_versions(
                           block_id, pipeline, run_id, knowledge_version, status,
                           draft_translation, final_translation, analysis,
                           semantic_obligations, memory_summary, warnings_json,
                           active, created_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (
                        outcome.block.id, pipeline, run_id, outcome.knowledge_version,
                        outcome.status, outcome.draft_translation, outcome.final_translation,
                        outcome.analysis, outcome.semantic_obligations,
                        outcome.memory_summary,
                        json.dumps(outcome.warnings, ensure_ascii=False), utc_now(),
                    ),
                )
                translation_id = int(cursor.lastrowid)
                for concept_id in sorted(set(outcome.matched_concept_ids)):
                    connection.execute(
                        """INSERT OR IGNORE INTO dependencies(
                               translation_id, dependency_type, dependency_id, knowledge_version
                           ) VALUES(?, 'concept', ?, ?)""",
                        (translation_id, concept_id, outcome.knowledge_version),
                    )
                for claim_id in outcome.claim_dependencies:
                    connection.execute(
                        """INSERT OR IGNORE INTO dependencies(
                               translation_id, dependency_type, dependency_id, knowledge_version
                           ) VALUES(?, 'claim', ?, ?)""",
                        (translation_id, claim_id, outcome.knowledge_version),
                    )
                for audit in outcome.audit_calls:
                    audit_request, audit_raw, audit_parsed = self._audit_fields(
                        audit_mode,
                        dict(audit.get("request") or {}),
                        str(audit.get("raw_response") or ""),
                        audit.get("parsed"),
                    )
                    self.record_audit_call(
                        run_id=run_id,
                        block_id=outcome.block.id,
                        purpose=str(audit.get("purpose") or "translate"),
                        model=str(audit.get("model") or "unknown"),
                        knowledge_version=outcome.knowledge_version,
                        request=audit_request,
                        raw_response=audit_raw,
                        parsed=audit_parsed,
                        accepted=bool(
                            audit.get(
                                "accepted",
                                outcome.status
                                in {
                                    V4BlockStatus.COMPLETED.value,
                                    V4BlockStatus.COMPLETED_WITH_WARNINGS.value,
                                },
                            )
                        ),
                        attempts=int(audit.get("attempts") or 1),
                        elapsed_ms=int(audit.get("elapsed_ms") or 0),
                        error=audit.get("error") or outcome.error,
                        connection=connection,
                    )
                connection.execute(
                    "UPDATE blocks SET status=?, last_error=?, updated_at=? WHERE id=?",
                    (outcome.status, outcome.error, utc_now(), outcome.block.id),
                )
                if outcome.status == V4BlockStatus.INCOMPLETE_REQUIRES_HUMAN.value:
                    connection.execute(
                        """INSERT INTO human_queue(
                               block_id, kind, severity, payload_json, created_at
                           ) VALUES(?, 'context_overflow', 'blocking', ?, ?)""",
                        (
                            outcome.block.id,
                            json.dumps({"error": outcome.error}, ensure_ascii=False),
                            utc_now(),
                        ),
                    )

    def commit_translation_proposals(
        self,
        run_id: str,
        outcomes: Sequence[TranslationOutcome],
        enqueue_review: bool = False,
    ) -> Optional[int]:
        proposals = [
            (outcome, "term", payload)
            for outcome in outcomes
            for payload in outcome.term_proposals
            if payload.get("src")
        ] + [
            (outcome, "relation", payload)
            for outcome in outcomes
            for payload in outcome.relation_proposals
        ]
        if not proposals:
            return None
        proposals.sort(key=lambda item: (item[0].block.global_index, item[1], json.dumps(item[2], sort_keys=True)))
        with self.transaction() as connection:
            material_terms: Dict[str, tuple[str, str]] = {}
            for _, kind, payload in proposals:
                if kind != "term":
                    continue
                source = str(payload.get("src") or "").strip()
                normalized = normalize_english_form(source)
                target = str(payload.get("tgt") or "").strip()
                concept_id = stable_id("concept", normalized)
                existing = connection.execute(
                    "SELECT default_target FROM concepts WHERE id=? AND retired_version IS NULL",
                    (concept_id,),
                ).fetchone()
                if existing is None or (not existing["default_target"] and target):
                    material_terms[concept_id] = (source, target)
            version = (
                self.create_knowledge_version(f"translation proposals {run_id}", connection)
                if material_terms
                else int(connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0])
            )
            has_novel_proposal = False
            changed_concepts: set[str] = set()
            proposed_concepts: set[str] = set()
            current_block_ids = {outcome.block.id for outcome in outcomes}
            seen_proposal_keys: set[tuple[str, str, str]] = set()
            for outcome, kind, payload in proposals:
                payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                if kind == "term":
                    source = str(payload.get("src") or "").strip()
                    normalized = normalize_english_form(source)
                    concept_id = stable_id("concept", normalized)
                    proposed_concepts.add(concept_id)
                    target = str(payload.get("tgt") or "").strip()
                    quote = source if source in outcome.block.source_text else source
                    existing_concept = connection.execute(
                        "SELECT default_target FROM concepts WHERE id=? AND retired_version IS NULL",
                        (concept_id,),
                    ).fetchone()
                    existing_evidence = connection.execute(
                        """SELECT 1 FROM evidence
                           WHERE kind='translation_term'
                             AND LOWER(source_form)=LOWER(?) AND payload_json=? LIMIT 1""",
                        (source, payload_json),
                    ).fetchone()
                    proposal_key = ("term", concept_id, target)
                    changes_decision = (
                        existing_concept is None
                        or (not existing_concept["default_target"] and bool(target))
                        or (
                            bool(target)
                            and existing_concept["default_target"] != target
                        )
                    )
                    novel = (
                        changes_decision
                        and proposal_key not in seen_proposal_keys
                        and existing_evidence is None
                    )
                    seen_proposal_keys.add(proposal_key)
                    has_novel_proposal = has_novel_proposal or novel
                    cursor = connection.execute(
                        """INSERT INTO evidence(
                               block_id, paragraph_id, kind, source_form, evidence_quote,
                               payload_json, confidence, extractor, run_id, created_at
                           ) VALUES(?, 'P000', 'translation_term', ?, ?, ?, 0.5,
                                    'translate_v4', ?, ?)""",
                        (
                            outcome.block.id, source, quote,
                            payload_json, run_id, utc_now(),
                        ),
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO concepts(
                               id, kind, canonical_source, default_target, description,
                               status, scope, locked, created_version, created_at
                           ) VALUES(?, ?, ?, ?, ?, 'provisional', 'book', 0, ?, ?)""",
                        (
                            concept_id, payload.get("type") or "concept", source,
                            payload.get("tgt") or "", payload.get("context") or "",
                            version, utc_now(),
                        ),
                    )
                    if target:
                        connection.execute(
                            """UPDATE concepts
                               SET default_target=CASE
                                   WHEN default_target='' THEN ? ELSE default_target END
                               WHERE id=? AND locked=0""",
                            (target, concept_id),
                        )
                    if concept_id in material_terms:
                        changed_concepts.add(concept_id)
                    lexeme_id = self._ensure_schema8_lexeme(
                        connection,
                        source,
                        normalized_form=normalized,
                        concept_id=concept_id,
                        knowledge_version=version,
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO source_forms(
                               lexeme_id, form, normalized_form, grammar_json
                           ) VALUES(?, ?, ?, '{}')""",
                        (lexeme_id, source, normalized),
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO mentions(
                               block_id, paragraph_id, source_form, normalized_form,
                               discourse_function, lexeme_id, concept_id, evidence_id
                           ) VALUES(?, 'P000', ?, ?, 'unknown', ?, ?, ?)""",
                        (
                            outcome.block.id,
                            source,
                            normalized,
                            lexeme_id,
                            concept_id,
                            int(cursor.lastrowid),
                        ),
                    )
                    translation = connection.execute(
                        """SELECT id FROM translation_versions
                           WHERE block_id=? AND pipeline='parallel_v4' AND active=1""",
                        (outcome.block.id,),
                    ).fetchone()
                    if translation:
                        connection.execute(
                            """INSERT OR IGNORE INTO dependencies(
                                   translation_id, dependency_type, dependency_id, knowledge_version
                               ) VALUES(?, 'concept', ?, ?)""",
                            (int(translation["id"]), concept_id, version),
                        )
                else:
                    quote = str(payload.get("context") or payload.get("sub") or "relation")
                    existing_evidence = connection.execute(
                        """SELECT 1 FROM evidence
                           WHERE kind='translation_relation' AND payload_json=? LIMIT 1""",
                        (payload_json,),
                    ).fetchone()
                    proposal_key = ("relation", payload_json, "")
                    novel = proposal_key not in seen_proposal_keys and existing_evidence is None
                    seen_proposal_keys.add(proposal_key)
                    has_novel_proposal = has_novel_proposal or novel
                    connection.execute(
                        """INSERT INTO evidence(
                               block_id, paragraph_id, kind, evidence_quote, payload_json,
                               confidence, extractor, run_id, created_at
                           ) VALUES(?, 'P000', 'translation_relation', ?, ?, 0.5,
                                    'translate_v4', ?, ?)""",
                        (
                            outcome.block.id,
                            quote, payload_json, run_id, utc_now(),
                        ),
                    )
                if enqueue_review and novel:
                    connection.execute(
                        """INSERT INTO human_queue(
                               block_id, kind, severity, payload_json, created_at
                           ) VALUES(?, 'translation_proposal', 'review', ?, ?)""",
                        (
                            outcome.block.id,
                            json.dumps(
                                {"proposal_kind": kind, "payload": payload},
                                ensure_ascii=False,
                            ),
                            utc_now(),
                        ),
                    )
            if changed_concepts:
                import re

                for concept_id in sorted(changed_concepts):
                    forms = [
                        row["form"]
                        for row in connection.execute(
                            """SELECT DISTINCT sf.form FROM source_forms sf
                               JOIN concept_lexemes cl
                                 ON cl.lexeme_id=sf.lexeme_id
                               WHERE cl.concept_id=?
                                 AND cl.retired_version IS NULL""",
                            (concept_id,),
                        )
                    ]
                    if not forms:
                        continue
                    rows = connection.execute(
                        """SELECT b.id, b.source_text, tv.id translation_id
                           FROM blocks b JOIN translation_versions tv ON tv.block_id=b.id
                           WHERE b.source_edition_id=(SELECT id FROM source_editions WHERE active=1)
                             AND tv.pipeline='parallel_v4' AND tv.active=1"""
                    ).fetchall()
                    for row in rows:
                        if row["id"] in current_block_ids:
                            continue
                        if any(re.search(rf"\b{re.escape(form)}\b", row["source_text"], re.I) for form in forms):
                            connection.execute(
                                "UPDATE translation_versions SET status=? WHERE id=?",
                                (V4BlockStatus.NEEDS_REVALIDATE.value, row["translation_id"]),
                            )
                            connection.execute(
                                "UPDATE blocks SET status=?, updated_at=? WHERE id=?",
                                (V4BlockStatus.NEEDS_REVALIDATE.value, utc_now(), row["id"]),
                            )
            if proposed_concepts:
                import re

                active_blocks = connection.execute(
                    """SELECT id, legacy_id, source_text FROM blocks
                       WHERE source_edition_id=(SELECT id FROM source_editions WHERE active=1)"""
                ).fetchall()
                for concept_id in sorted(proposed_concepts):
                    concept = connection.execute(
                        """SELECT id, canonical_source, default_target, locked
                           FROM concepts WHERE id=? AND retired_version IS NULL""",
                        (concept_id,),
                    ).fetchone()
                    if concept is None or not concept["default_target"]:
                        continue
                    pattern = re.compile(
                        rf"\b{re.escape(concept['canonical_source'])}\b", re.I
                    )
                    affected = [
                        row for row in active_blocks if pattern.search(row["source_text"])
                    ]
                    targets = {
                        row["target"]
                        for row in connection.execute(
                            """SELECT target FROM rendering_rules
                               WHERE concept_id=? AND retired_version IS NULL AND target!=''""",
                            (concept_id,),
                        )
                    }
                    for row in connection.execute(
                        """SELECT payload_json FROM evidence
                           WHERE kind='translation_term' AND source_form IS NOT NULL"""
                    ):
                        candidate = json.loads(row["payload_json"])
                        if normalize_english_form(str(candidate.get("src") or "")) == normalize_english_form(
                            concept["canonical_source"]
                        ) and candidate.get("tgt"):
                            targets.add(str(candidate["tgt"]))
                    high_impact = len(affected) >= 3 or len(targets) > 1 or bool(concept["locked"])
                    if not high_impact:
                        continue
                    evidence_payload = [
                        {
                            "legacy_id": outcome.block.id,
                            "evidence_quote": str(payload.get("src") or ""),
                            "payload": payload,
                        }
                        for outcome, kind, payload in proposals
                        if kind == "term"
                        and normalize_english_form(str(payload.get("src") or ""))
                        == normalize_english_form(concept["canonical_source"])
                    ]
                    task_payload = {
                        "source": concept["canonical_source"],
                        "target": concept["default_target"],
                        "affected_blocks": len(affected),
                        "target_conflict": len(targets) > 1,
                        "evidence": evidence_payload,
                    }
                    task_id = stable_id(
                        "verify",
                        f"concept:{concept_id}:{json.dumps(task_payload, sort_keys=True)}",
                        length=24,
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO verification_tasks(
                               id, subject_type, subject_id, payload_json, status,
                               required_votes, created_at
                           ) VALUES(?, 'concept', ?, ?, 'open', 2, ?)""",
                        (
                            task_id,
                            concept_id,
                            json.dumps(task_payload, ensure_ascii=False, sort_keys=True),
                            utc_now(),
                        ),
                    )
            return version if has_novel_proposal else None

    def active_translations(self, pipeline: str = "parallel_v4") -> Dict[str, Dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM translation_versions
                   WHERE pipeline=? AND active=1 ORDER BY block_id""",
                (pipeline,),
            ).fetchall()
            return {row["block_id"]: dict(row) for row in rows}

    def list_baseline_documents(self) -> List[Dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM baseline_documents
                   WHERE source_edition_id=(SELECT id FROM source_editions WHERE active=1)
                   ORDER BY active DESC, created_at DESC"""
            ).fetchall()
            return [dict(row) for row in rows]

    def baseline_for_block(
        self,
        block_id: str,
        baseline_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        with closing(self.connect()) as connection:
            params: List[Any] = [block_id]
            name_clause = ""
            if baseline_name:
                name_clause = " AND d.name=?"
                params.append(baseline_name)
            document = connection.execute(
                f"""SELECT d.* FROM baseline_documents d
                    JOIN block_baseline_links l ON l.baseline_document_id=d.id
                    WHERE l.block_id=? AND d.active=1{name_clause}
                    ORDER BY d.created_at DESC LIMIT 1""",
                params,
            ).fetchone()
            if document is None:
                return None
            paragraphs = connection.execute(
                """SELECT p.paragraph_index, p.target_text, l.partial_start,
                          l.partial_end, l.alignment_status
                   FROM block_baseline_links l
                   JOIN baseline_paragraphs p
                     ON p.baseline_document_id=l.baseline_document_id
                    AND p.paragraph_index=l.paragraph_index
                   WHERE l.block_id=? AND l.baseline_document_id=?
                   ORDER BY l.ordinal""",
                (block_id, document["id"]),
            ).fetchall()
            return {
                "document": dict(document),
                "paragraphs": [dict(row) for row in paragraphs],
                "text": "\n\n".join(row["target_text"] for row in paragraphs),
                "has_partial_boundary": any(
                    row["partial_start"] or row["partial_end"] for row in paragraphs
                ),
                "has_ambiguous_alignment": any(
                    row["alignment_status"] != "exact" for row in paragraphs
                ),
            }

    def comparison_reference_for_block(
        self,
        block_id: str,
        baseline_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """返回与当前文本块严格对齐的外部基线或串行译文。"""
        baseline = self.baseline_for_block(block_id, baseline_name)
        if (
            baseline
            and not baseline.get("has_partial_boundary")
            and not baseline.get("has_ambiguous_alignment")
            and str(baseline.get("text") or "").strip()
        ):
            return {
                "origin": baseline["document"]["name"],
                "text": str(baseline["text"]).strip(),
            }
        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT final_translation FROM translation_versions
                   WHERE block_id=? AND pipeline='serial_v3' AND active=1
                     AND final_translation!=''
                   ORDER BY id DESC LIMIT 1""",
                (block_id,),
            ).fetchone()
        if row is None:
            return None
        return {"origin": "serial_v3", "text": row["final_translation"].strip()}

    def create_claim(
        self,
        kind: str,
        statement: str,
        reveal_global_index: int,
        subject_form: str = "",
        scope: str = "book",
        confidence: float = 0.5,
        high_impact: bool = False,
        status: str = "proposed",
        locked: bool = False,
    ) -> str:
        if kind not in {
            "translation_constraint",
            "temporal_constraint",
            "identity_hypothesis",
            "reveal_boundary",
            "interpretation_hypothesis",
        }:
            raise ValueError("不支持的claim类型")
        if scope not in {"book", "occurrence"}:
            raise ValueError("claim scope必须是book或occurrence")
        if status not in {"proposed", "disputed", "verified", "rejected"}:
            raise ValueError("不支持的claim状态")
        if status == "verified" and not locked:
            raise ValueError("直接创建verified claim时必须同时人工锁定")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("claim confidence必须位于0到1之间")
        if reveal_global_index < 0:
            raise ValueError("reveal_global_index不能为负数")
        high_impact = high_impact or kind in {
            "temporal_constraint",
            "identity_hypothesis",
            "reveal_boundary",
        }
        claim_id = stable_id(
            "claim",
            f"{kind}:{scope}:{reveal_global_index}:{subject_form}:{statement}",
            length=24,
        )
        with self.transaction() as connection:
            version = self.create_knowledge_version("create claim", connection)
            connection.execute(
                """INSERT OR IGNORE INTO claims(
                       id, kind, statement, subject_form, status, scope, confidence,
                       reveal_global_index, high_impact, locked, created_version, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    claim_id,
                    kind,
                    statement,
                    subject_form,
                    status,
                    scope,
                    confidence,
                    reveal_global_index,
                    int(high_impact),
                    int(locked),
                    version,
                    utc_now(),
                ),
            )
            if (high_impact or kind == "translation_constraint") and status in {
                "proposed",
                "disputed",
            }:
                task_id = stable_id("verify", f"claim:{claim_id}", length=24)
                connection.execute(
                    """INSERT OR IGNORE INTO verification_tasks(
                           id, subject_type, subject_id, payload_json, status,
                           required_votes, created_at
                       ) VALUES(?, 'claim', ?, ?, 'open', 2, ?)""",
                    (
                        task_id,
                        claim_id,
                        json.dumps(
                            {
                                "kind": kind,
                                "statement": statement,
                                "subject_form": subject_form,
                                "reveal_global_index": reveal_global_index,
                                "evidence": [],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        utc_now(),
                    ),
                )
        return claim_id

    def list_claims(self, include_retired: bool = False) -> List[Dict[str, Any]]:
        query = "SELECT * FROM claims"
        if not include_retired:
            query += " WHERE retired_version IS NULL"
        query += " ORDER BY reveal_global_index, created_at"
        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute(query)]

    def claims_for_block(self, block: V4Block) -> List[Dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT DISTINCT c.*
                   FROM claims c
                   LEFT JOIN claim_evidence ce ON ce.claim_id=c.id
                   LEFT JOIN evidence e ON e.id=ce.evidence_id
                   WHERE c.retired_version IS NULL
                     AND c.kind='translation_constraint'
                     AND c.status='verified'
                     AND c.reveal_global_index<=?
                     AND (
                         (c.scope='occurrence' AND e.block_id=?)
                         OR
                         (c.scope!='occurrence' AND (
                             c.subject_form='' OR INSTR(LOWER(?), LOWER(c.subject_form))>0
                         ))
                     )
                   ORDER BY c.locked DESC, c.confidence DESC, c.id""",
                (block.global_index, block.id, block.source_text),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_verification_tasks(self, status: str = "open") -> List[Dict[str, Any]]:
        with closing(self.connect()) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM verification_tasks WHERE status=? ORDER BY created_at, id",
                    (status,),
                )
            ]

    def get_block_by_identifier(self, identifier: str) -> V4Block:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT * FROM blocks
                   WHERE source_edition_id=(SELECT id FROM source_editions WHERE active=1)
                     AND (id=? OR legacy_id=?) LIMIT 1""",
                (identifier, identifier),
            ).fetchone()
            if row is None:
                raise KeyError(f"文本块不存在: {identifier}")
            return self._row_to_block(row)

    def review_blocks(self, limit: int = 500) -> List[Dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT b.id, b.legacy_id, b.chapter_title, b.global_index,
                          b.status, tv.status translation_status, tv.warnings_json,
                          cv.choice comparison_choice
                   FROM blocks b
                   LEFT JOIN translation_versions tv
                     ON tv.block_id=b.id AND tv.pipeline='parallel_v4' AND tv.active=1
                   LEFT JOIN comparison_votes cv ON cv.block_id=b.id
                   WHERE b.source_edition_id=(SELECT id FROM source_editions WHERE active=1)
                   ORDER BY CASE
                       WHEN tv.status='completed_with_warnings' THEN 0
                       WHEN b.status IN ('incomplete_requires_human','failed_retryable',
                                         'failed_terminal','needs_revalidate') THEN 1
                       ELSE 2 END,
                       b.global_index LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_review_block(self, identifier: str) -> Dict[str, Any]:
        block = self.get_block_by_identifier(identifier)
        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT b.*, v4.status v4_status, v4.final_translation v4_translation,
                          v4.draft_translation, v4.warnings_json,
                          serial.final_translation serial_translation
                   FROM blocks b
                   LEFT JOIN translation_versions v4
                     ON v4.block_id=b.id AND v4.pipeline='parallel_v4' AND v4.active=1
                   LEFT JOIN translation_versions serial
                     ON serial.block_id=b.id AND serial.pipeline='serial_v3' AND serial.active=1
                   WHERE b.id=?""",
                (block.id,),
            ).fetchone()
            evidence = [
                dict(item)
                for item in connection.execute(
                    """SELECT paragraph_id, kind, source_form, evidence_quote,
                              payload_json, confidence
                       FROM evidence WHERE block_id=? ORDER BY id""",
                    (block.id,),
                )
            ]
            annotations = [
                dict(item)
                for item in connection.execute(
                    "SELECT * FROM annotations WHERE block_id=? ORDER BY paragraph_index, created_at",
                    (block.id,),
                )
            ]
        baseline = self.baseline_for_block(block.id)
        result = dict(row) if row else {}
        result["evidence"] = evidence
        result["annotations"] = annotations
        result["baseline"] = baseline
        return result

    def add_annotation(
        self,
        block_identifier: str,
        paragraph_index: int,
        body: str,
        status: str = "proposed",
        source: str = "human",
    ) -> str:
        body = body.strip()
        if not body:
            raise ValueError("注释正文不能为空")
        if paragraph_index < 0:
            raise ValueError("paragraph_index不能为负数")
        if status not in {"proposed", "approved", "rejected"}:
            raise ValueError("不支持的注释状态")
        block = self.get_block_by_identifier(block_identifier)
        annotation_id = stable_id(
            "annotation",
            f"{block.id}:{paragraph_index}:{body}:{source}",
            length=24,
        )
        with self.transaction() as connection:
            version = int(connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0])
            connection.execute(
                """INSERT OR IGNORE INTO annotations(
                       id, block_id, paragraph_index, body, status, source,
                       created_version, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    annotation_id,
                    block.id,
                    paragraph_index,
                    body,
                    status,
                    source,
                    version,
                    utc_now(),
                ),
            )
        return annotation_id

    def list_annotations(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        query = """SELECT a.*, b.legacy_id FROM annotations a
                   JOIN blocks b ON b.id=a.block_id"""
        params: List[Any] = []
        if status:
            query += " WHERE a.status=?"
            params.append(status)
        query += " ORDER BY b.global_index, a.paragraph_index, a.created_at"
        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute(query, params)]

    @staticmethod
    def _archive_comparison_vote(
        connection: sqlite3.Connection,
        vote: sqlite3.Row,
        archived_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO comparison_vote_history(
                   original_vote_id, block_id, candidate_a_origin,
                   candidate_b_origin, candidate_a_hash, candidate_b_hash,
                   choice, selected_origin, blinded, note, created_at,
                   updated_at, archived_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vote["id"],
                vote["block_id"],
                vote["candidate_a_origin"],
                vote["candidate_b_origin"],
                vote["candidate_a_hash"],
                vote["candidate_b_hash"],
                vote["choice"],
                vote["selected_origin"],
                vote["blinded"],
                vote["note"],
                vote["created_at"],
                vote["updated_at"],
                archived_at,
            ),
        )
        connection.execute(
            "DELETE FROM comparison_votes WHERE id=?", (vote["id"],)
        )

    def record_comparison_vote(
        self,
        block_identifier: str,
        choice: str,
        candidate_a_origin: str,
        candidate_b_origin: str,
        candidate_a_hash: str = "",
        candidate_b_hash: str = "",
        blinded: bool = True,
        note: str = "",
    ) -> Dict[str, Any]:
        if choice not in {"A", "B", "tie", "neither"}:
            raise ValueError("盲评选择必须是A、B、tie或neither")
        block = self.get_block_by_identifier(block_identifier)
        selected_origin = {
            "A": candidate_a_origin,
            "B": candidate_b_origin,
        }.get(choice)
        now = utc_now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM comparison_votes WHERE block_id=?", (block.id,)
            ).fetchone()
            if existing is not None and (
                existing["candidate_a_hash"] != candidate_a_hash
                or existing["candidate_b_hash"] != candidate_b_hash
            ):
                self._archive_comparison_vote(
                    connection, existing, now
                )
            connection.execute(
                """INSERT INTO comparison_votes(
                       block_id, candidate_a_origin, candidate_b_origin,
                       candidate_a_hash, candidate_b_hash, choice, selected_origin,
                       blinded, note, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(block_id) DO UPDATE SET
                       candidate_a_origin=excluded.candidate_a_origin,
                       candidate_b_origin=excluded.candidate_b_origin,
                       candidate_a_hash=excluded.candidate_a_hash,
                       candidate_b_hash=excluded.candidate_b_hash,
                       choice=excluded.choice,
                       selected_origin=excluded.selected_origin,
                       blinded=excluded.blinded,
                       note=excluded.note,
                       updated_at=excluded.updated_at""",
                (
                    block.id,
                    candidate_a_origin,
                    candidate_b_origin,
                    candidate_a_hash,
                    candidate_b_hash,
                    choice,
                    selected_origin,
                    int(blinded),
                    note.strip(),
                    now,
                    now,
                ),
            )
        return self.comparison_vote_for_block(
            block.id, candidate_a_hash, candidate_b_hash
        ) or {}

    def comparison_vote_for_block(
        self,
        block_identifier: str,
        candidate_a_hash: Optional[str] = None,
        candidate_b_hash: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        block = self.get_block_by_identifier(block_identifier)
        with closing(self.connect()) as connection:
            if candidate_a_hash is None or candidate_b_hash is None:
                row = connection.execute(
                    "SELECT * FROM comparison_votes WHERE block_id=?", (block.id,)
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT * FROM comparison_votes
                       WHERE block_id=? AND candidate_a_hash=? AND candidate_b_hash=?""",
                    (block.id, candidate_a_hash, candidate_b_hash),
                ).fetchone()
            return dict(row) if row else None

    def list_comparison_votes(self) -> List[Dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT v.*, b.legacy_id, b.global_index
                   FROM comparison_votes v JOIN blocks b ON b.id=v.block_id
                   ORDER BY b.global_index"""
            ).fetchall()
            return [dict(row) for row in rows]

    def list_comparison_vote_history(self) -> List[Dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT v.*, b.legacy_id, b.global_index
                   FROM comparison_vote_history v JOIN blocks b ON b.id=v.block_id
                   ORDER BY v.archived_at, b.global_index"""
            ).fetchall()
            return [dict(row) for row in rows]

    def resolve_annotation(self, annotation_id: str, action: str) -> Dict[str, Any]:
        if action not in {"approve", "reject"}:
            raise ValueError("action必须是approve或reject")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM annotations WHERE id=?",
                (annotation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"注释不存在: {annotation_id}")
            status = "approved" if action == "approve" else "rejected"
            connection.execute(
                "UPDATE annotations SET status=?, resolved_at=? WHERE id=?",
                (status, utc_now(), annotation_id),
            )
            return {"id": annotation_id, "status": status}

    def request_repair(self, block_identifier: str, issues: Sequence[str]) -> str:
        block = self.get_block_by_identifier(block_identifier)
        issue_list = [str(issue) for issue in issues if str(issue).strip()]
        task_id = stable_id(
            "repair",
            f"{block.id}:{json.dumps(issue_list, ensure_ascii=False, sort_keys=True)}",
            length=24,
        )
        with self.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO repair_tasks(
                       id, block_id, status, issues_json, requested_at
                   ) VALUES(?, ?, 'open', ?, ?)""",
                (task_id, block.id, json.dumps(issue_list, ensure_ascii=False), utc_now()),
            )
        return task_id

    def list_repair_tasks(self, status: str = "open") -> List[Dict[str, Any]]:
        with closing(self.connect()) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT r.*, b.legacy_id FROM repair_tasks r
                       JOIN blocks b ON b.id=r.block_id
                       WHERE r.status=? ORDER BY r.requested_at""",
                    (status,),
                )
            ]

    def commit_repair_result(
        self,
        run_id: str,
        task_id: str,
        final_translation: str,
        audit: Dict[str, Any],
    ) -> int:
        """Commit one repaired translation as a new active version."""
        with self.transaction() as connection:
            task = connection.execute(
                "SELECT * FROM repair_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(f"修复任务不存在: {task_id}")
            if task["status"] != "open":
                raise ValueError(f"修复任务已处理: {task_id}")
            previous = connection.execute(
                """SELECT * FROM translation_versions
                   WHERE block_id=? AND pipeline='parallel_v4' AND active=1""",
                (task["block_id"],),
            ).fetchone()
            if previous is None:
                raise ValueError("当前文本块没有活动译文版本")
            version = int(connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0])
            connection.execute(
                """UPDATE translation_versions SET active=0
                   WHERE block_id=? AND pipeline='parallel_v4' AND active=1""",
                (task["block_id"],),
            )
            cursor = connection.execute(
                """INSERT INTO translation_versions(
                       block_id, pipeline, run_id, knowledge_version, status,
                       draft_translation, final_translation, analysis,
                       semantic_obligations, memory_summary, warnings_json,
                       active, created_at
                    ) VALUES(?, 'parallel_v4', ?, ?, 'completed', ?, ?, ?, ?, ?, '[]', 1, ?)""",
                (
                    task["block_id"],
                    run_id,
                    version,
                    previous["draft_translation"],
                    final_translation,
                    previous["analysis"],
                    previous["semantic_obligations"],
                    previous["memory_summary"],
                    utc_now(),
                ),
            )
            translation_id = int(cursor.lastrowid)
            connection.execute(
                """INSERT OR IGNORE INTO dependencies(
                       translation_id, dependency_type, dependency_id, knowledge_version
                   ) SELECT ?, dependency_type, dependency_id, ?
                     FROM dependencies WHERE translation_id=?""",
                (translation_id, version, previous["id"]),
            )
            self.record_audit_call(
                run_id=run_id,
                block_id=task["block_id"],
                purpose="repair",
                model=str(audit.get("model") or "unknown"),
                knowledge_version=version,
                request=dict(audit.get("request") or {}),
                raw_response=str(audit.get("raw_response") or ""),
                parsed=audit.get("parsed"),
                accepted=True,
                attempts=int(audit.get("attempts") or 1),
                elapsed_ms=int(audit.get("elapsed_ms") or 0),
                error=None,
                connection=connection,
            )
            connection.execute(
                "UPDATE blocks SET status='completed', last_error=NULL, updated_at=? WHERE id=?",
                (utc_now(), task["block_id"]),
            )
            connection.execute(
                """UPDATE repair_tasks
                   SET status='completed', resolved_at=?, translation_id=? WHERE id=?""",
                (utc_now(), translation_id, task_id),
            )
            return translation_id

    def commit_repair_failure(
        self,
        task_id: str,
        error: str,
        run_id: Optional[str] = None,
        audit: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Keep the current translation active and route a failed repair to humans."""
        with self.transaction() as connection:
            task = connection.execute(
                "SELECT * FROM repair_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(f"修复任务不存在: {task_id}")
            if task["status"] != "open":
                return
            version = int(connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0])
            if audit is not None and run_id:
                self.record_audit_call(
                    run_id=run_id,
                    block_id=task["block_id"],
                    purpose="repair",
                    model=str(audit.get("model") or "unknown"),
                    knowledge_version=version,
                    request=dict(audit.get("request") or {}),
                    raw_response=str(audit.get("raw_response") or ""),
                    parsed=audit.get("parsed"),
                    accepted=False,
                    attempts=int(audit.get("attempts") or 1),
                    elapsed_ms=int(audit.get("elapsed_ms") or 0),
                    error=error,
                    connection=connection,
                )
            connection.execute(
                "UPDATE repair_tasks SET status='needs_human', resolved_at=? WHERE id=?",
                (utc_now(), task_id),
            )
            connection.execute(
                """INSERT INTO human_queue(
                       block_id, kind, severity, payload_json, created_at
                   ) VALUES(?, 'repair_failed', 'high', ?, ?)""",
                (
                    task["block_id"],
                    json.dumps(
                        {
                            "repair_task_id": task_id,
                            "issues": json.loads(task["issues_json"]),
                            "error": error,
                        },
                        ensure_ascii=False,
                    ),
                    utc_now(),
                ),
            )

    def commit_verification_result(
        self,
        run_id: str,
        task: Dict[str, Any],
        votes: Sequence[Dict[str, Any]],
    ) -> str:
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT status FROM verification_tasks WHERE id=?",
                (task["id"],),
            ).fetchone()
            if current is None:
                raise KeyError(f"核验任务不存在: {task['id']}")
            if current["status"] != "open":
                return str(current["status"])
            version = int(connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0])
            for index, vote in enumerate(votes, start=1):
                audit_id = self.record_audit_call(
                    run_id=run_id,
                    block_id=None,
                    purpose="verify",
                    model=str(vote.get("model") or "unknown"),
                    knowledge_version=version,
                    request=dict(vote.get("request") or {}),
                    raw_response=str(vote.get("raw_response") or ""),
                    parsed=vote.get("parsed"),
                    accepted=bool(vote.get("accepted")),
                    attempts=int(vote.get("attempts") or 1),
                    elapsed_ms=int(vote.get("elapsed_ms") or 0),
                    error=vote.get("error"),
                    connection=connection,
                )
                parsed = vote.get("parsed") or {
                    "verdict": "uncertain",
                    "rationale": vote.get("error") or "核验输出无效",
                    "evidence_quotes": [],
                }
                connection.execute(
                    """INSERT INTO verification_votes(
                           task_id, verifier_index, verdict, rationale, evidence_json,
                           audit_call_id, created_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                    (
                        task["id"],
                        index,
                        parsed["verdict"],
                        parsed["rationale"],
                        json.dumps(parsed.get("evidence_quotes") or [], ensure_ascii=False),
                        audit_id,
                        utc_now(),
                    ),
                )
            supported = (
                len(votes) >= int(task.get("required_votes") or 2)
                and all((vote.get("parsed") or {}).get("verdict") == "support" for vote in votes)
            )
            if supported:
                version = self.create_knowledge_version(
                    f"double verification {task['id']}", connection
                )
                if task["subject_type"] == "concept":
                    connection.execute(
                        """UPDATE concepts SET status='verified',
                               verified_target=CASE
                                   WHEN verified_target!='' THEN verified_target
                                   WHEN working_target!='' THEN working_target
                                   ELSE default_target END
                           WHERE id=? AND locked=0""",
                        (task["subject_id"],),
                    )
                    connection.execute(
                        "UPDATE rendering_rules SET status='verified' WHERE concept_id=? AND locked=0",
                        (task["subject_id"],),
                    )
                elif task["subject_type"] == "claim":
                    connection.execute(
                        "UPDATE claims SET status='verified' WHERE id=? AND locked=0",
                        (task["subject_id"],),
                    )
                status = "verified"
            else:
                status = "needs_human"
                payload = {
                    "verification_task_id": task["id"],
                    "subject_type": task["subject_type"],
                    "subject_id": task["subject_id"],
                    "proposal": json.loads(task["payload_json"]),
                    "votes": [vote.get("parsed") for vote in votes],
                }
                connection.execute(
                    """INSERT INTO human_queue(
                           block_id, kind, severity, payload_json, created_at
                       ) VALUES(NULL, 'high_impact_verification', 'high', ?, ?)""",
                    (json.dumps(payload, ensure_ascii=False), utc_now()),
                )
            connection.execute(
                "UPDATE verification_tasks SET status=?, resolved_at=? WHERE id=?",
                (status, utc_now(), task["id"]),
            )
            return status

    def list_human_queue(self, status: str = "open") -> List[Dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT h.*, b.legacy_id
                   FROM human_queue h LEFT JOIN blocks b ON b.id=h.block_id
                   WHERE h.status=? ORDER BY h.id""",
                (status,),
            ).fetchall()
            return [dict(row) for row in rows]

    def resolve_human_item(self, item_id: int, action: str) -> Dict[str, Any]:
        if action not in {"accept", "reject", "retry"}:
            raise ValueError("action 必须是 accept、reject 或 retry")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM human_queue WHERE id=?",
                (item_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"人工队列项不存在: {item_id}")
            if row["status"] != "open":
                raise ValueError(f"人工队列项 {item_id} 已处理: {row['status']}")
            if action == "retry":
                if row["kind"] == "context_overflow" and row["block_id"]:
                    connection.execute(
                        "UPDATE blocks SET status=?, last_error=NULL, updated_at=? WHERE id=?",
                        (V4BlockStatus.READY.value, utc_now(), row["block_id"]),
                    )
                elif row["kind"] == "repair_failed":
                    payload = json.loads(row["payload_json"])
                    task_id = payload.get("repair_task_id")
                    if not task_id:
                        raise ValueError("修复失败队列项缺少repair_task_id")
                    connection.execute(
                        """UPDATE repair_tasks SET status='open', resolved_at=NULL
                           WHERE id=? AND status='needs_human'""",
                        (task_id,),
                    )
                else:
                    raise ValueError("只有 context_overflow 或 repair_failed 队列项可以重试")
                connection.execute(
                    "UPDATE human_queue SET status='retried', resolved_at=? WHERE id=?",
                    (utc_now(), item_id),
                )
                return {
                    "id": item_id,
                    "status": "retried",
                    "knowledge_version": None,
                    "affected_translations": 0,
                }
            if row["kind"] == "high_impact_verification":
                payload = json.loads(row["payload_json"])
                subject_type = payload.get("subject_type")
                subject_id = payload.get("subject_id")
                version = self.create_knowledge_version(
                    f"human {action} high impact item {item_id}", connection
                )
                if subject_type == "concept":
                    if action == "accept":
                        replacement_target = str(
                            (payload.get("proposal") or {}).get("target") or ""
                        ).strip()
                        connection.execute(
                            """UPDATE concepts SET
                                   default_target=CASE WHEN ?!='' THEN ? ELSE default_target END,
                                   working_target=CASE WHEN ?!='' THEN ? ELSE working_target END,
                                   verified_target=CASE WHEN ?!='' THEN ?
                                       WHEN verified_target!='' THEN verified_target
                                       WHEN working_target!='' THEN working_target
                                       ELSE default_target END,
                                   status='verified', locked=1 WHERE id=?""",
                            (
                                replacement_target,
                                replacement_target,
                                replacement_target,
                                replacement_target,
                                replacement_target,
                                replacement_target,
                                subject_id,
                            ),
                        )
                        if replacement_target:
                            connection.execute(
                                """UPDATE rendering_rules SET retired_version=?
                                   WHERE concept_id=? AND retired_version IS NULL AND locked=0""",
                                (version, subject_id),
                            )
                            rule_id = stable_id(
                                "rule",
                                f"{subject_id}:human-default:{replacement_target}",
                            )
                            connection.execute(
                                """INSERT OR IGNORE INTO rendering_rules(
                                       id, concept_id, condition_json, target, priority,
                                       status, scope, locked, created_version, created_at
                                   ) VALUES(?, ?, '{}', ?, 100, 'verified', 'book', 1, ?, ?)""",
                                (
                                    rule_id,
                                    subject_id,
                                    replacement_target,
                                    version,
                                    utc_now(),
                                ),
                            )
                        else:
                            connection.execute(
                                """UPDATE rendering_rules
                                   SET status='verified', locked=1 WHERE concept_id=?""",
                                (subject_id,),
                            )
                    else:
                        connection.execute(
                            """UPDATE concepts SET status='rejected', retired_version=?
                               WHERE id=? AND locked=0""",
                            (version, subject_id),
                        )
                elif subject_type == "claim":
                    if action == "accept":
                        replacement_statement = str(
                            (payload.get("proposal") or {}).get("statement") or ""
                        ).strip()
                        connection.execute(
                            """UPDATE claims SET
                                   statement=CASE WHEN ?!='' THEN ? ELSE statement END,
                                   status='verified', locked=1 WHERE id=?""",
                            (replacement_statement, replacement_statement, subject_id),
                        )
                    else:
                        connection.execute(
                            """UPDATE claims SET status='rejected', retired_version=?
                               WHERE id=? AND locked=0""",
                            (version, subject_id),
                        )
                affected_rows = connection.execute(
                    """SELECT DISTINCT tv.id translation_id, tv.block_id
                       FROM dependencies d
                       JOIN translation_versions tv ON tv.id=d.translation_id
                       WHERE d.dependency_type=? AND d.dependency_id=?
                         AND tv.active=1 AND tv.pipeline='parallel_v4'""",
                    (subject_type, subject_id),
                ).fetchall()
                for affected_row in affected_rows:
                    connection.execute(
                        "UPDATE translation_versions SET status=? WHERE id=?",
                        (
                            V4BlockStatus.NEEDS_REVALIDATE.value,
                            affected_row["translation_id"],
                        ),
                    )
                    connection.execute(
                        "UPDATE blocks SET status=?, updated_at=? WHERE id=?",
                        (
                            V4BlockStatus.NEEDS_REVALIDATE.value,
                            utc_now(),
                            affected_row["block_id"],
                        ),
                    )
                resolved_status = "accepted" if action == "accept" else "rejected"
                connection.execute(
                    "UPDATE human_queue SET status=?, resolved_at=? WHERE id=?",
                    (resolved_status, utc_now(), item_id),
                )
                return {
                    "id": item_id,
                    "status": resolved_status,
                    "knowledge_version": version,
                    "affected_translations": len(affected_rows),
                }
            if row["kind"] != "translation_proposal":
                raise ValueError("该队列项不是可接受或拒绝的知识建议")
            payload = json.loads(row["payload_json"])
            proposal_kind = payload.get("proposal_kind")
            proposal = payload.get("payload") or {}
            version: Optional[int] = None
            affected = 0
            if proposal_kind == "term" and proposal.get("src"):
                import re

                source = str(proposal["src"]).strip()
                target = str(proposal.get("tgt") or "").strip()
                concept_id = stable_id("concept", normalize_english_form(source))
                concept = connection.execute(
                    """SELECT c.*, kv.reason created_reason
                       FROM concepts c JOIN knowledge_versions kv ON kv.id=c.created_version
                       WHERE c.id=? AND c.retired_version IS NULL""",
                    (concept_id,),
                ).fetchone()
                if action == "accept":
                    version = self.create_knowledge_version(
                        f"human accept queue item {item_id}", connection
                    )
                    if concept is None:
                        connection.execute(
                            """INSERT INTO concepts(
                                   id, kind, canonical_source, default_target,
                                   working_target, verified_target, description,
                                   status, scope, locked, created_version, created_at
                               ) VALUES(?, ?, ?, ?, ?, ?, ?, 'verified', 'book', 1, ?, ?)""",
                            (
                                concept_id,
                                proposal.get("type") or "concept",
                                source,
                                target,
                                target,
                                target,
                                proposal.get("context") or "",
                                version,
                                utc_now(),
                            ),
                        )
                        lexeme_id = self._ensure_schema8_lexeme(
                            connection,
                            source,
                            normalized_form=normalize_english_form(source),
                            concept_id=concept_id,
                            knowledge_version=version,
                        )
                        connection.execute(
                            """INSERT INTO source_forms(
                                   lexeme_id, form, normalized_form, grammar_json
                               ) VALUES(?, ?, ?, '{}')""",
                            (lexeme_id, source, normalize_english_form(source)),
                        )
                    else:
                        connection.execute(
                            """UPDATE concepts SET default_target=?, working_target=?,
                                      verified_target=?, status='verified', locked=1
                               WHERE id=?""",
                            (target, target, target, concept_id),
                        )
                    if target:
                        rule_id = stable_id("rule", f"{concept_id}:human-default:{target}")
                        connection.execute(
                            """INSERT OR IGNORE INTO rendering_rules(
                                   id, concept_id, condition_json, target, priority, status,
                                   scope, locked, created_version, created_at
                               ) VALUES(?, ?, '{}', ?, 100, 'verified', 'book', 1, ?, ?)""",
                            (rule_id, concept_id, target, version, utc_now()),
                        )
                elif (
                    concept is not None
                    and str(concept["created_reason"]).startswith("translation proposals")
                    and not concept["locked"]
                ):
                    version = self.create_knowledge_version(
                        f"human reject queue item {item_id}", connection
                    )
                    connection.execute(
                        "UPDATE concepts SET status='rejected', retired_version=? WHERE id=?",
                        (version, concept_id),
                    )
                if version is not None:
                    rows = connection.execute(
                        """SELECT b.id, b.source_text, tv.id translation_id
                           FROM blocks b JOIN translation_versions tv ON tv.block_id=b.id
                           WHERE b.source_edition_id=(SELECT id FROM source_editions WHERE active=1)
                             AND tv.pipeline='parallel_v4' AND tv.active=1
                             AND tv.status IN (?, ?)""",
                        (
                            V4BlockStatus.COMPLETED.value,
                            V4BlockStatus.COMPLETED_WITH_WARNINGS.value,
                        ),
                    ).fetchall()
                    pattern = re.compile(rf"\b{re.escape(source)}\b", re.I)
                    for translation in rows:
                        if not pattern.search(translation["source_text"]):
                            continue
                        connection.execute(
                            "UPDATE translation_versions SET status=? WHERE id=?",
                            (
                                V4BlockStatus.NEEDS_REVALIDATE.value,
                                translation["translation_id"],
                            ),
                        )
                        connection.execute(
                            "UPDATE blocks SET status=?, updated_at=? WHERE id=?",
                            (
                                V4BlockStatus.NEEDS_REVALIDATE.value,
                                utc_now(),
                                translation["id"],
                            ),
                        )
                        affected += 1
            resolved_status = "accepted" if action == "accept" else "rejected"
            connection.execute(
                "UPDATE human_queue SET status=?, resolved_at=? WHERE id=?",
                (resolved_status, utc_now(), item_id),
            )
            return {
                "id": item_id,
                "status": resolved_status,
                "knowledge_version": version,
                "affected_translations": affected,
            }

    def amend_human_item(self, item_id: int, replacement: str) -> Dict[str, Any]:
        replacement = replacement.strip()
        if not replacement:
            raise ValueError("修改内容不能为空")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM human_queue WHERE id=? AND status='open'",
                (item_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"待处理人工队列项不存在: {item_id}")
            payload = json.loads(row["payload_json"])
            if row["kind"] == "translation_proposal":
                proposal = payload.get("payload") or {}
                if payload.get("proposal_kind") != "term":
                    raise ValueError("只有术语建议支持编辑后接受")
                proposal["tgt"] = replacement
                payload["payload"] = proposal
            elif row["kind"] == "high_impact_verification":
                proposal = payload.get("proposal") or {}
                if payload.get("subject_type") == "concept":
                    proposal["target"] = replacement
                elif payload.get("subject_type") == "claim":
                    proposal["statement"] = replacement
                else:
                    raise ValueError("不支持编辑该高影响建议")
                payload["proposal"] = proposal
            else:
                raise ValueError("该队列项不支持编辑")
            connection.execute(
                "UPDATE human_queue SET payload_json=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), item_id),
            )
            return {"id": item_id, "payload": payload}

    def export_rows(self, pipeline: str = "parallel_v4") -> List[Dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT b.*, tv.status translation_status, tv.draft_translation,
                          tv.final_translation, tv.analysis, tv.memory_summary,
                          tv.warnings_json
                   FROM blocks b
                   LEFT JOIN translation_versions tv
                     ON tv.block_id=b.id AND tv.pipeline=? AND tv.active=1
                   WHERE b.source_edition_id=(SELECT id FROM source_editions WHERE active=1)
                   ORDER BY b.global_index""",
                (pipeline,),
            ).fetchall()
            return [dict(row) for row in rows]

    def status_summary(self) -> Dict[str, int]:
        with closing(self.connect()) as connection:
            return {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    """SELECT status, COUNT(*) count FROM blocks
                       WHERE source_edition_id=(SELECT id FROM source_editions WHERE active=1)
                       GROUP BY status ORDER BY status"""
                )
            }
