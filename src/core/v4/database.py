"""SQLite storage and the single-writer commit surface for parallel_v4."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from .models import ScanOutcome, TranslationOutcome, V4Block, V4BlockStatus


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, value: str, length: int = 16) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def normalize_english_form(value: str) -> str:
    normalized = " ".join(value.strip().casefold().split())
    if normalized.endswith("'s") or normalized.endswith("’s"):
        normalized = normalized[:-2]
    elif normalized.endswith("'") or normalized.endswith("’"):
        normalized = normalized[:-1]
    return normalized


class V4Database:
    """All writes happen through this object on the coordinator thread."""

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root)
        self.root = self.project_root / "artifacts" / "parallel_v4"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "book.db"
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
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with closing(self.connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
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

                CREATE TABLE IF NOT EXISTS concepts (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    canonical_source TEXT NOT NULL,
                    default_target TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'provisional',
                    scope TEXT NOT NULL DEFAULT 'book',
                    locked INTEGER NOT NULL DEFAULT 0,
                    created_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
                    retired_version INTEGER REFERENCES knowledge_versions(id),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS source_forms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    concept_id TEXT NOT NULL REFERENCES concepts(id),
                    form TEXT NOT NULL,
                    normalized_form TEXT NOT NULL,
                    grammar_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(concept_id, normalized_form)
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
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            if connection.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0] == 0:
                connection.execute(
                    "INSERT INTO knowledge_versions(parent_id, reason, created_at) VALUES(NULL, ?, ?)",
                    ("initialize parallel_v4", utc_now()),
                )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(blocks)").fetchall()
            }
            if "legacy_id" not in columns:
                connection.execute("ALTER TABLE blocks ADD COLUMN legacy_id TEXT NOT NULL DEFAULT ''")
                connection.execute("UPDATE blocks SET legacy_id=id WHERE legacy_id=''")
            connection.commit()

    def current_knowledge_version(self) -> int:
        with closing(self.connect()) as connection:
            return int(connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0])

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
            status=row["status"],
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

    def start_run(self, run_id: str, stage: str, config: Dict[str, Any]) -> None:
        with self.transaction() as connection:
            version = int(connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0])
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

    def record_audit_call(
        self,
        run_id: str,
        block_id: str,
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
    ) -> None:
        owns_connection = connection is None
        if owns_connection:
            connection = self.connect()
        assert connection is not None
        connection.execute(
            """INSERT INTO audit_calls(
                   run_id, block_id, purpose, model, knowledge_version, request_json,
                   raw_response, parsed_json, accepted, attempts, elapsed_ms, error, created_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, block_id, purpose, model, knowledge_version,
                json.dumps(request, ensure_ascii=False), raw_response,
                json.dumps(parsed, ensure_ascii=False) if parsed is not None else None,
                int(accepted), attempts, elapsed_ms, error, utc_now(),
            ),
        )
        if owns_connection:
            connection.commit()
            connection.close()

    @staticmethod
    def _audit_fields(
        mode: str,
        request: Dict[str, Any],
        raw_response: str,
        parsed: Optional[Dict[str, Any]],
    ) -> tuple[Dict[str, Any], str, Optional[Dict[str, Any]]]:
        if mode not in {"full", "response", "minimal"}:
            raise ValueError("audit_mode 必须是 full、response 或 minimal")
        if mode == "full":
            return request, raw_response, parsed
        summary = {
            "redacted": True,
            "message_count": len(request.get("messages") or []),
            "schema_version": request.get("schema_version"),
        }
        if mode == "response":
            return summary, raw_response, parsed
        return summary, "", None

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
                parsed = outcome.response.model_dump(mode="json") if outcome.response else None
                calls = outcome.audit_calls or [
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
                           ) VALUES(?, ?, 'mention', ?, ?, ?, ?, 'scan_v4', ?, ?)""",
                        (
                            outcome.block.id, mention.paragraph_id, mention.source_form,
                            mention.evidence_quote, json.dumps(payload, ensure_ascii=False),
                            mention.confidence, run_id, utc_now(),
                        ),
                    )
                    evidence_id = int(cursor.lastrowid)
                    connection.execute(
                        """INSERT OR IGNORE INTO mentions(
                               block_id, paragraph_id, source_form, normalized_form,
                               discourse_function, evidence_id
                           ) VALUES(?, ?, ?, ?, ?, ?)""",
                        (
                            outcome.block.id, mention.paragraph_id, mention.source_form,
                            normalize_english_form(mention.source_form),
                            mention.discourse_function, evidence_id,
                        ),
                    )
                for ambiguity in outcome.response.ambiguities:
                    connection.execute(
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
                connection.execute(
                    "UPDATE blocks SET status=?, last_error=NULL, updated_at=? WHERE id=?",
                    (V4BlockStatus.SCANNED.value, utc_now(), outcome.block.id),
                )
            return version

    def reconcile_exact_forms(self, reason: str = "reconcile exact English forms") -> int:
        """Conservative reconciliation: never infer aliases or identities."""
        with self.transaction() as connection:
            rows = connection.execute(
                """SELECT m.id mention_id, m.source_form, m.normalized_form,
                          m.discourse_function, e.payload_json, e.confidence
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
                        concept_id, payload.get("category", "concept"), row["source_form"],
                        payload.get("suggested_target", ""), payload.get("description", ""),
                        version, utc_now(),
                    ),
                )
                connection.execute(
                    """INSERT OR IGNORE INTO source_forms(
                           concept_id, form, normalized_form, grammar_json
                       ) VALUES(?, ?, ?, '{}')""",
                    (concept_id, row["source_form"], row["normalized_form"]),
                )
                connection.execute(
                    "UPDATE mentions SET concept_id=? WHERE id=?",
                    (concept_id, row["mention_id"]),
                )
                target = payload.get("suggested_target", "").strip()
                if target:
                    condition = {"discourse_function": row["discourse_function"]}
                    rule_id = stable_id(
                        "rule",
                        f"{concept_id}:{json.dumps(condition, sort_keys=True)}:{target}",
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO rendering_rules(
                               id, concept_id, condition_json, target, priority, status,
                               scope, locked, created_version, created_at
                           ) VALUES(?, ?, ?, ?, 0, 'provisional', 'book', 0, ?, ?)""",
                        (rule_id, concept_id, json.dumps(condition), target, version, utc_now()),
                    )
            connection.execute(
                "UPDATE blocks SET status=?, updated_at=? WHERE status=?",
                (V4BlockStatus.READY.value, utc_now(), V4BlockStatus.SCANNED.value),
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
            connection.execute(
                """INSERT OR IGNORE INTO source_forms(
                       concept_id, form, normalized_form, grammar_json
                   ) VALUES(?, ?, ?, '{}')""",
                (concept_id, source, normalized),
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

    def concepts_for_text(self, text: str) -> List[Dict[str, Any]]:
        import re

        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT c.id, c.kind, c.canonical_source, c.default_target, c.description,
                          c.status, c.locked, sf.form, sf.normalized_form
                   FROM concepts c JOIN source_forms sf ON sf.concept_id=c.id
                   WHERE c.retired_version IS NULL"""
            ).fetchall()
            matched: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                pattern = re.compile(rf"\b{re.escape(row['form'])}\b", re.IGNORECASE)
                if pattern.search(text):
                    item = matched.setdefault(
                        row["id"],
                        {
                            "id": row["id"], "kind": row["kind"],
                            "source": row["canonical_source"],
                            "default_target": row["default_target"],
                            "description": row["description"], "status": row["status"],
                            "locked": bool(row["locked"]), "forms": [], "rules": [],
                        },
                    )
                    item["forms"].append(row["form"])
            if not matched:
                return []
            placeholders = ",".join("?" for _ in matched)
            rule_rows = connection.execute(
                f"""SELECT id, concept_id, condition_json, target, priority, status, locked
                    FROM rendering_rules
                    WHERE retired_version IS NULL AND concept_id IN ({placeholders})
                    ORDER BY priority DESC, id""",
                list(matched),
            ).fetchall()
            for row in rule_rows:
                matched[row["concept_id"]]["rules"].append(
                    {
                        "id": row["id"], "condition": json.loads(row["condition_json"]),
                        "target": row["target"], "priority": row["priority"],
                        "status": row["status"], "locked": bool(row["locked"]),
                    }
                )
            return list(matched.values())

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
                connection.execute(
                    "UPDATE translation_versions SET active=0 WHERE block_id=? AND pipeline=? AND active=1",
                    (outcome.block.id, pipeline),
                )
                cursor = connection.execute(
                    """INSERT INTO translation_versions(
                           block_id, pipeline, run_id, knowledge_version, status,
                           draft_translation, final_translation, analysis, memory_summary,
                           warnings_json, active, created_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (
                        outcome.block.id, pipeline, run_id, outcome.knowledge_version,
                        outcome.status, outcome.draft_translation, outcome.final_translation,
                        outcome.analysis, outcome.memory_summary,
                        json.dumps(outcome.warnings, ensure_ascii=False), utc_now(),
                    ),
                )
                translation_id = int(cursor.lastrowid)
                concepts = self.concepts_for_text(outcome.block.source_text)
                for concept in concepts:
                    connection.execute(
                        """INSERT OR IGNORE INTO dependencies(
                               translation_id, dependency_type, dependency_id, knowledge_version
                           ) VALUES(?, 'concept', ?, ?)""",
                        (translation_id, concept["id"], outcome.knowledge_version),
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
            current_block_ids = {outcome.block.id for outcome in outcomes}
            seen_proposal_keys: set[tuple[str, str, str]] = set()
            for outcome, kind, payload in proposals:
                payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                if kind == "term":
                    source = str(payload.get("src") or "").strip()
                    normalized = normalize_english_form(source)
                    concept_id = stable_id("concept", normalized)
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
                    connection.execute(
                        """INSERT OR IGNORE INTO source_forms(
                               concept_id, form, normalized_form, grammar_json
                           ) VALUES(?, ?, ?, '{}')""",
                        (concept_id, source, normalized),
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO mentions(
                               block_id, paragraph_id, source_form, normalized_form,
                               discourse_function, concept_id, evidence_id
                           ) VALUES(?, 'P000', ?, ?, 'unknown', ?, ?)""",
                        (outcome.block.id, source, normalized, concept_id, int(cursor.lastrowid)),
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
                            "SELECT form FROM source_forms WHERE concept_id=?",
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
            return version if has_novel_proposal else None

    def active_translations(self, pipeline: str = "parallel_v4") -> Dict[str, Dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM translation_versions
                   WHERE pipeline=? AND active=1 ORDER BY block_id""",
                (pipeline,),
            ).fetchall()
            return {row["block_id"]: dict(row) for row in rows}

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
                if row["kind"] != "context_overflow" or not row["block_id"]:
                    raise ValueError("只有 context_overflow 队列项可以重试")
                connection.execute(
                    "UPDATE blocks SET status=?, last_error=NULL, updated_at=? WHERE id=?",
                    (V4BlockStatus.READY.value, utc_now(), row["block_id"]),
                )
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
                                   id, kind, canonical_source, default_target, description,
                                   status, scope, locked, created_version, created_at
                               ) VALUES(?, ?, ?, ?, ?, 'verified', 'book', 1, ?, ?)""",
                            (
                                concept_id,
                                proposal.get("type") or "concept",
                                source,
                                target,
                                proposal.get("context") or "",
                                version,
                                utc_now(),
                            ),
                        )
                        connection.execute(
                            """INSERT INTO source_forms(
                                   concept_id, form, normalized_form, grammar_json
                               ) VALUES(?, ?, ?, '{}')""",
                            (concept_id, source, normalize_english_form(source)),
                        )
                    else:
                        connection.execute(
                            """UPDATE concepts SET default_target=?, status='verified', locked=1
                               WHERE id=?""",
                            (target, concept_id),
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
