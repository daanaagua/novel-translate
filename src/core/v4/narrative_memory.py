"""Append-only narrative memory, premap cache, snapshots, and retrieval."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

from .database import V4Database, stable_id, utc_now
from .models import V4Block
from .narrative_models import (
    DiscourseDelta,
    DiscourseState,
    NarrativeMemoryCandidate,
    NarrativeMemoryRecord,
    NarrativePremapResult,
    NarrativeRetrieval,
    NarrativeSnapshot,
    NarrativeSubject,
    SemanticRelation,
    render_narrative_memory,
)


MAX_VISIBLE_MEMORIES = 512
MAX_CACHE_JSON_BYTES = 256 * 1024


class NarrativeContextOverflow(RuntimeError):
    pass


@dataclass(frozen=True)
class MemoryMergeResult:
    memory_version: int
    memory_ids: tuple[str, ...]
    change_ids: tuple[int, ...] = ()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalize_statement(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _semantic_fingerprint(
    candidate: NarrativeMemoryCandidate,
    block: V4Block,
    evidence_offsets: Sequence[tuple[int, int]],
) -> str:
    payload = {
        "memory_type": candidate.memory_type,
        "statement": _normalize_statement(candidate.statement),
        "subjects": sorted(
            (
                subject.subject_type,
                subject.subject_id,
                subject.role,
            )
            for subject in candidate.subjects
        ),
        "source_block_id": block.id,
        "evidence_offsets": sorted(evidence_offsets),
        "visibility": candidate.visibility,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class NarrativeMemoryStore:
    def __init__(self, database: V4Database):
        if not isinstance(database, V4Database):
            raise TypeError("database must be a V4Database")
        self.database = database

    @contextmanager
    def _write(
        self, connection: sqlite3.Connection | None
    ) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            if not connection.in_transaction:
                raise ValueError("external connection requires an active transaction")
            yield connection
            return
        with self.database.transaction() as owned:
            yield owned

    def current_memory_version(
        self, connection: sqlite3.Connection | None = None
    ) -> int:
        if connection is not None:
            row = connection.execute(
                "SELECT MAX(id) FROM memory_versions"
            ).fetchone()
            return int(row[0])
        with closing(self.database.connect()) as owned:
            row = owned.execute("SELECT MAX(id) FROM memory_versions").fetchone()
        return int(row[0])

    def _create_memory_version(
        self,
        connection: sqlite3.Connection,
        reason: str,
        source_global_index: int,
    ) -> int:
        parent = self.current_memory_version(connection)
        cursor = connection.execute(
            """INSERT INTO memory_versions(
                   parent_id, reason, source_global_index, created_at)
               VALUES(?, ?, ?, ?)""",
            (parent, reason[:512], int(source_global_index), utc_now()),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def premap_cache_key(
        *,
        block: V4Block,
        structure_hash: str,
        prompt_hash: str,
        model_id: str,
        parameters_hash: str,
        prior_snapshot_hash: str,
        provisional_subject_hash: str,
        protocol_version: str = "narrative-premap-v1",
    ) -> str:
        payload = {
            "protocol_version": protocol_version,
            "source_hash": block.source_hash,
            "structure_hash": structure_hash,
            "prompt_hash": prompt_hash,
            "model_id": model_id,
            "parameters_hash": parameters_hash,
            "prior_snapshot_hash": prior_snapshot_hash,
            "provisional_subject_hash": provisional_subject_hash,
        }
        return "premap-cache_" + hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()

    def save_premap_result(
        self,
        *,
        cache_key: str,
        block: V4Block,
        result: NarrativePremapResult,
        model_id: str,
        prompt_hash: str,
        prior_snapshot_hash: str,
        request_hash: str,
        response_hash: str,
        audit_call_id: int | None = None,
    ) -> str:
        if not isinstance(result, NarrativePremapResult):
            raise TypeError("result must be NarrativePremapResult")
        payload = result.to_dict()
        encoded = _canonical_json(payload).encode("utf-8")
        if len(encoded) > MAX_CACHE_JSON_BYTES:
            raise ValueError("premap result exceeds cache size limit")
        result_id = stable_id("premap", cache_key, 32)
        status = (
            "degraded"
            if result.degraded and not (
                result.semantic_relations or result.memory_candidates
            )
            else "accepted_with_warnings"
            if result.validation_warnings
            else "accepted"
        )
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM premap_results WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            if existing is not None:
                return str(existing["id"])
            connection.execute(
                """INSERT INTO premap_results(
                       id, block_id, cache_key, status, semantic_json,
                       memory_candidates_json, discourse_delta_json,
                       validation_json, model_id, prompt_hash,
                       prior_snapshot_hash, request_hash, response_hash,
                       audit_call_id, snapshot_id, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    result_id,
                    block.id,
                    cache_key,
                    status,
                    _canonical_json(
                        [value.to_dict() for value in result.semantic_relations]
                    ),
                    _canonical_json(
                        [value.to_dict() for value in result.memory_candidates]
                    ),
                    _canonical_json(result.discourse_delta.to_dict()),
                    _canonical_json(
                        {
                            "warnings": list(result.validation_warnings),
                            "degraded": result.degraded,
                        }
                    ),
                    model_id[:256],
                    prompt_hash[:256],
                    prior_snapshot_hash[:256],
                    request_hash[:256],
                    response_hash[:256],
                    audit_call_id,
                    utc_now(),
                ),
            )
        return result_id

    def load_premap_result(
        self, cache_key: str
    ) -> NarrativePremapResult | None:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM premap_results WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        relations = []
        for item in json.loads(row["semantic_json"] or "[]"):
            relations.append(
                SemanticRelation(
                    relation_type=str(item["relation_type"]),
                    inference_strength=str(item["inference_strength"]),
                    source_spans=tuple(item.get("source_spans") or ()),
                    related_memory_ids=tuple(
                        item.get("related_memory_ids") or ()
                    ),
                    translation_constraint=str(
                        item["translation_constraint"]
                    ),
                )
            )
        candidates = []
        for item in json.loads(row["memory_candidates_json"] or "[]"):
            candidates.append(
                NarrativeMemoryCandidate(
                    candidate_id=str(item["candidate_id"]),
                    memory_type=str(item["memory_type"]),
                    statement=str(item["statement"]),
                    truth_status=str(item["truth_status"]),
                    visibility=str(item["visibility"]),
                    confidence=float(item["confidence"]),
                    evidence_spans=tuple(item.get("evidence_spans") or ()),
                    subjects=tuple(
                        NarrativeSubject(
                            subject_type=str(subject["subject_type"]),
                            subject_id=str(subject["subject_id"]),
                            role=str(subject.get("role") or "subject"),
                        )
                        for subject in item.get("subjects") or ()
                    ),
                    related_memory_ids=tuple(
                        item.get("related_memory_ids") or ()
                    ),
                    state_operation=str(
                        item.get("state_operation") or "append"
                    ),
                    high_impact=bool(item.get("high_impact")),
                )
            )
        validation = json.loads(row["validation_json"] or "{}")
        return NarrativePremapResult(
            semantic_relations=tuple(relations),
            memory_candidates=tuple(candidates),
            discourse_delta=DiscourseDelta.from_mapping(
                json.loads(row["discourse_delta_json"] or "{}")
            ),
            validation_warnings=tuple(validation.get("warnings") or ()),
            degraded=bool(validation.get("degraded")),
        )

    def link_premap_snapshot(
        self, cache_key: str, snapshot_id: str
    ) -> None:
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM narrative_snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(f"narrative snapshot does not exist: {snapshot_id}")
            updated = connection.execute(
                """UPDATE premap_results SET snapshot_id=?
                   WHERE cache_key=?""",
                (snapshot_id, cache_key),
            )
            if updated.rowcount != 1:
                raise KeyError(f"premap cache does not exist: {cache_key}")

    def load_snapshot(self, snapshot_id: str) -> NarrativeSnapshot | None:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM narrative_snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return NarrativeSnapshot(
            id=str(row["id"]),
            block_id=str(row["block_id"]),
            global_index=int(row["global_index"]),
            knowledge_version=int(row["knowledge_version"]),
            memory_version=int(row["memory_version"]),
            previous_snapshot_id=str(row["previous_snapshot_id"] or ""),
            discourse_state=DiscourseState.from_mapping(
                json.loads(row["discourse_state_json"] or "{}")
            ),
            visible_memory_ids=tuple(
                json.loads(row["visible_memory_ids_json"] or "[]")
            ),
            snapshot_hash=str(row["snapshot_hash"]),
        )

    def snapshot_for_cache(
        self, cache_key: str
    ) -> NarrativeSnapshot | None:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                """SELECT snapshot_id FROM premap_results
                   WHERE cache_key=?""",
                (cache_key,),
            ).fetchone()
        if row is None or not row["snapshot_id"]:
            return None
        return self.load_snapshot(str(row["snapshot_id"]))

    def snapshot_payload(
        self, snapshot: NarrativeSnapshot, *, max_memories: int = 64
    ) -> dict[str, Any]:
        selected_ids = snapshot.visible_memory_ids[:max_memories]
        if not selected_ids:
            memories: list[dict[str, Any]] = []
        else:
            placeholders = ",".join("?" for _ in selected_ids)
            with closing(self.database.connect()) as connection:
                rows = connection.execute(
                    f"""SELECT id, memory_type, statement, truth_status,
                               visibility, confidence, status
                        FROM narrative_memories
                        WHERE id IN ({placeholders})""",
                    selected_ids,
                ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            memories = [
                {
                    "id": memory_id,
                    "memory_type": str(by_id[memory_id]["memory_type"]),
                    "statement": str(by_id[memory_id]["statement"]),
                    "truth_status": str(by_id[memory_id]["truth_status"]),
                    "visibility": str(by_id[memory_id]["visibility"]),
                    "confidence": float(by_id[memory_id]["confidence"]),
                    "status": str(by_id[memory_id]["status"]),
                }
                for memory_id in selected_ids
                if memory_id in by_id
            ]
        return {
            "id": snapshot.id,
            "snapshot_hash": snapshot.snapshot_hash,
            "global_index": snapshot.global_index,
            "memory_version": snapshot.memory_version,
            "visible_memories": memories,
        }

    @staticmethod
    def _evidence_offsets(
        block: V4Block, candidate: NarrativeMemoryCandidate
    ) -> tuple[tuple[int, int, str], ...]:
        found: list[tuple[int, int, str]] = []
        cursor = 0
        for quote in candidate.evidence_spans:
            start = block.source_text.find(quote, cursor)
            if start < 0:
                start = block.source_text.find(quote)
            if start < 0:
                raise ValueError(
                    f"memory evidence is not grounded in {block.id}: {quote!r}"
                )
            end = start + len(quote)
            found.append((start, end, quote))
            cursor = end
        return tuple(found)

    @staticmethod
    def _link_relation(candidate: NarrativeMemoryCandidate) -> str:
        if candidate.memory_type == "contradiction":
            return "contradicts"
        if candidate.state_operation == "supersede":
            return "supersedes"
        if candidate.state_operation == "close_question":
            return "answers"
        if candidate.state_operation == "relate":
            return "elaborates"
        return "supports"

    @staticmethod
    def _impact(candidate: NarrativeMemoryCandidate) -> int:
        if candidate.high_impact or candidate.memory_type in {
            "timeline_anchor",
            "narrator_state",
        }:
            return 3
        if candidate.memory_type in {
            "relationship_state",
            "location_state",
            "character_state",
            "contradiction",
        }:
            return 2
        if candidate.visibility == "render_only":
            return 1
        return 0

    def merge_candidates(
        self,
        block: V4Block,
        candidates: Sequence[NarrativeMemoryCandidate],
        *,
        source: str = "premap",
        connection: sqlite3.Connection | None = None,
    ) -> MemoryMergeResult:
        prepared_by_id: dict[
            str,
            tuple[
                NarrativeMemoryCandidate,
                tuple[tuple[int, int, str], ...],
                str,
                str,
            ],
        ] = {}
        for candidate in candidates:
            if not isinstance(candidate, NarrativeMemoryCandidate):
                raise TypeError(
                    "memory candidates must contain NarrativeMemoryCandidate values"
                )
            evidence = self._evidence_offsets(block, candidate)
            fingerprint = _semantic_fingerprint(
                candidate,
                block,
                [(start, end) for start, end, _ in evidence],
            )
            memory_id = stable_id("memory", fingerprint, 32)
            prepared_by_id.setdefault(
                memory_id,
                (candidate, evidence, fingerprint, memory_id),
            )
        prepared = list(prepared_by_id.values())
        if not prepared:
            return MemoryMergeResult(self.current_memory_version(), ())

        with self._write(connection) as active:
            existing_ids = {
                str(row["id"])
                for row in active.execute(
                    f"""SELECT id FROM narrative_memories
                        WHERE id IN ({','.join('?' for _ in prepared)})""",
                    tuple(value[3] for value in prepared),
                )
            }
            new_rows = [value for value in prepared if value[3] not in existing_ids]
            missing_links: list[tuple[str, str, str, float]] = []
            for candidate, _evidence, _fingerprint, memory_id in prepared:
                relation = self._link_relation(candidate)
                for related_id in candidate.related_memory_ids:
                    target = active.execute(
                        "SELECT 1 FROM narrative_memories WHERE id=?",
                        (related_id,),
                    ).fetchone()
                    if target is None:
                        continue
                    exists = active.execute(
                        """SELECT 1 FROM narrative_memory_links
                           WHERE from_memory_id=? AND to_memory_id=?
                             AND relation=?""",
                        (memory_id, related_id, relation),
                    ).fetchone()
                    if exists is None:
                        missing_links.append(
                            (memory_id, related_id, relation, candidate.confidence)
                        )
            if not new_rows and not missing_links:
                return MemoryMergeResult(
                    self.current_memory_version(active),
                    tuple(value[3] for value in prepared),
                )

            version = self._create_memory_version(
                active,
                f"{source} narrative memory for {block.id}",
                block.global_index,
            )
            change_ids: list[int] = []
            for candidate, evidence, fingerprint, memory_id in new_rows:
                active.execute(
                    """INSERT INTO narrative_memories(
                           id, memory_type, statement, truth_status, visibility,
                           confidence, reveal_global_index, source_block_id,
                           source_hash, status, high_impact,
                           semantic_fingerprint, created_memory_version,
                           retired_memory_version, created_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'provisional', ?, ?,
                              ?, NULL, ?)""",
                    (
                        memory_id,
                        candidate.memory_type,
                        candidate.statement,
                        candidate.truth_status,
                        candidate.visibility,
                        candidate.confidence,
                        block.global_index,
                        block.id,
                        block.source_hash,
                        int(candidate.high_impact),
                        fingerprint,
                        version,
                        utc_now(),
                    ),
                )
                for start, end, quote in evidence:
                    active.execute(
                        """INSERT OR IGNORE INTO narrative_memory_evidence(
                               memory_id, block_id, start_offset, end_offset,
                               quote, source_hash)
                           VALUES(?, ?, ?, ?, ?, ?)""",
                        (
                            memory_id,
                            block.id,
                            start,
                            end,
                            quote,
                            block.source_hash,
                        ),
                    )
                for subject in candidate.subjects:
                    active.execute(
                        """INSERT OR IGNORE INTO narrative_memory_subjects(
                               memory_id, subject_type, subject_id, role)
                           VALUES(?, ?, ?, ?)""",
                        (
                            memory_id,
                            subject.subject_type,
                            subject.subject_id,
                            subject.role,
                        ),
                    )
                cursor = active.execute(
                    """INSERT INTO memory_changes(
                           memory_version, subject_type, subject_id, change_kind,
                           old_fingerprint, new_fingerprint, impact_level,
                           payload_json, created_at)
                       VALUES(?, 'narrative_memory', ?, ?, '', ?, ?, ?, ?)""",
                    (
                        version,
                        memory_id,
                        candidate.memory_type,
                        fingerprint,
                        self._impact(candidate),
                        _canonical_json(
                            {
                                "source_block_id": block.id,
                                "visibility": candidate.visibility,
                                "high_impact": candidate.high_impact,
                            }
                        ),
                        utc_now(),
                    ),
                )
                change_ids.append(int(cursor.lastrowid))
            for from_id, to_id, relation, confidence in missing_links:
                active.execute(
                    """INSERT OR IGNORE INTO narrative_memory_links(
                           from_memory_id, to_memory_id, relation, confidence,
                           created_memory_version)
                       VALUES(?, ?, ?, ?, ?)""",
                    (from_id, to_id, relation, confidence, version),
                )
                if relation in {"answers", "supersedes"}:
                    prior = active.execute(
                        """SELECT semantic_fingerprint FROM narrative_memories
                           WHERE id=? AND retired_memory_version IS NULL""",
                        (to_id,),
                    ).fetchone()
                    if prior is not None:
                        active.execute(
                            """UPDATE narrative_memories
                               SET status='superseded',
                                   retired_memory_version=?
                               WHERE id=? AND retired_memory_version IS NULL""",
                            (version, to_id),
                        )
                        cursor = active.execute(
                            """INSERT INTO memory_changes(
                                   memory_version, subject_type, subject_id,
                                   change_kind, old_fingerprint,
                                   new_fingerprint, impact_level,
                                   payload_json, created_at)
                               VALUES(?, 'narrative_memory', ?, ?, ?, '', 2,
                                      ?, ?)""",
                            (
                                version,
                                to_id,
                                relation,
                                str(prior["semantic_fingerprint"]),
                                _canonical_json({"replacement": from_id}),
                                utc_now(),
                            ),
                        )
                        change_ids.append(int(cursor.lastrowid))
            return MemoryMergeResult(
                version,
                tuple(value[3] for value in prepared),
                tuple(change_ids),
            )

    def links_for_memory(self, memory_id: str) -> list[dict[str, str]]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """SELECT to_memory_id, relation
                   FROM narrative_memory_links
                   WHERE from_memory_id=?
                   ORDER BY relation, to_memory_id""",
                (memory_id,),
            ).fetchall()
        return [
            {
                "to_memory_id": str(row["to_memory_id"]),
                "relation": str(row["relation"]),
            }
            for row in rows
        ]

    def build_snapshot(
        self,
        block: V4Block,
        *,
        knowledge_version: int,
        discourse_state: DiscourseState,
        memory_version: int | None = None,
    ) -> NarrativeSnapshot:
        version = memory_version or self.current_memory_version()
        priority_subject_ids = {
            value
            for value in (
                *discourse_state.active_speakers,
                *discourse_state.addressed_parties,
                *discourse_state.unresolved_references,
                discourse_state.viewpoint_holder,
                discourse_state.scene_location,
            )
            if value
        }
        with self.database.transaction() as connection:
            if priority_subject_ids:
                subject_placeholders = ",".join(
                    "?" for _ in priority_subject_ids
                )
                priority_sql = f"""EXISTS(
                    SELECT 1 FROM narrative_memory_subjects nms
                    WHERE nms.memory_id=nm.id
                      AND nms.subject_id IN ({subject_placeholders})
                )"""
                priority_args = tuple(sorted(priority_subject_ids))
            else:
                priority_sql = "0"
                priority_args = ()
            rows = connection.execute(
                f"""SELECT nm.id, {priority_sql} priority_match
                    FROM narrative_memories nm
                    JOIN blocks source_block
                      ON source_block.id=nm.source_block_id
                     AND source_block.source_hash=nm.source_hash
                    WHERE nm.reveal_global_index<=?
                      AND nm.visibility!='system_private'
                      AND nm.status!='rejected'
                      AND nm.created_memory_version<=?
                      AND (nm.retired_memory_version IS NULL
                           OR nm.retired_memory_version>?)
                    ORDER BY priority_match DESC, nm.high_impact DESC,
                             nm.reveal_global_index DESC, nm.id
                    LIMIT ?""",
                priority_args
                + (
                    block.global_index,
                    version,
                    version,
                    MAX_VISIBLE_MEMORIES,
                ),
            ).fetchall()
            visible_ids = tuple(str(row["id"]) for row in rows)
            previous = connection.execute(
                """SELECT id FROM narrative_snapshots
                   WHERE global_index<?
                     AND knowledge_version<=?
                     AND memory_version<=?
                   ORDER BY global_index DESC, memory_version DESC, id DESC
                   LIMIT 1""",
                (block.global_index, knowledge_version, version),
            ).fetchone()
            previous_id = str(previous["id"]) if previous is not None else ""
            state_json = _canonical_json(discourse_state.to_dict())
            snapshot_hash = hashlib.sha256(
                _canonical_json(
                    {
                        "block_id": block.id,
                        "global_index": block.global_index,
                        "knowledge_version": int(knowledge_version),
                        "memory_version": int(version),
                        "previous_snapshot_id": previous_id,
                        "discourse_state": discourse_state.to_dict(),
                        "visible_memory_ids": visible_ids,
                    }
                ).encode("utf-8")
            ).hexdigest()
            snapshot_id = stable_id("snapshot", snapshot_hash, 32)
            connection.execute(
                """INSERT OR IGNORE INTO narrative_snapshots(
                       id, block_id, global_index, knowledge_version,
                       memory_version, previous_snapshot_id,
                       discourse_state_json, visible_memory_ids_json,
                       snapshot_hash, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    block.id,
                    block.global_index,
                    int(knowledge_version),
                    int(version),
                    previous_id or None,
                    state_json,
                    _canonical_json(visible_ids),
                    snapshot_hash,
                    utc_now(),
                ),
            )
        return NarrativeSnapshot(
            id=snapshot_id,
            block_id=block.id,
            global_index=block.global_index,
            knowledge_version=int(knowledge_version),
            memory_version=int(version),
            previous_snapshot_id=previous_id,
            discourse_state=discourse_state,
            visible_memory_ids=visible_ids,
            snapshot_hash=snapshot_hash,
        )

    @staticmethod
    def _record_from_row(
        row: sqlite3.Row, subjects: Sequence[NarrativeSubject]
    ) -> NarrativeMemoryRecord:
        return NarrativeMemoryRecord(
            id=str(row["id"]),
            memory_type=str(row["memory_type"]),
            statement=str(row["statement"]),
            truth_status=str(row["truth_status"]),
            visibility=str(row["visibility"]),
            confidence=float(row["confidence"]),
            reveal_global_index=int(row["reveal_global_index"]),
            source_block_id=str(row["source_block_id"]),
            status=str(row["status"]),
            high_impact=bool(row["high_impact"]),
            semantic_fingerprint=str(row["semantic_fingerprint"]),
            subjects=tuple(subjects),
        )

    def retrieve_for_block(
        self,
        block: V4Block,
        snapshot: NarrativeSnapshot,
        *,
        matched_subject_ids: Sequence[str],
        max_chars: int,
    ) -> NarrativeRetrieval:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        if not snapshot.visible_memory_ids:
            return NarrativeRetrieval((), (), 0)
        placeholders = ",".join("?" for _ in snapshot.visible_memory_ids)
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                f"""SELECT * FROM narrative_memories
                    WHERE id IN ({placeholders})""",
                snapshot.visible_memory_ids,
            ).fetchall()
            subject_rows = connection.execute(
                f"""SELECT * FROM narrative_memory_subjects
                    WHERE memory_id IN ({placeholders})
                    ORDER BY memory_id, subject_type, subject_id, role""",
                snapshot.visible_memory_ids,
            ).fetchall()
        subjects_by_memory: dict[str, list[NarrativeSubject]] = {}
        for row in subject_rows:
            subjects_by_memory.setdefault(str(row["memory_id"]), []).append(
                NarrativeSubject(
                    subject_type=str(row["subject_type"]),
                    subject_id=str(row["subject_id"]),
                    role=str(row["role"]),
                )
            )
        matched = {str(value) for value in matched_subject_ids}
        discourse = set(snapshot.discourse_state.active_speakers)
        discourse.update(snapshot.discourse_state.addressed_parties)
        discourse.update(snapshot.discourse_state.unresolved_references)
        if snapshot.discourse_state.viewpoint_holder:
            discourse.add(snapshot.discourse_state.viewpoint_holder)
        if snapshot.discourse_state.scene_location:
            discourse.add(snapshot.discourse_state.scene_location)
        ranked = []
        for row in rows:
            memory_subjects = subjects_by_memory.get(str(row["id"]), [])
            subject_ids = {value.subject_id for value in memory_subjects}
            direct = bool(subject_ids & matched)
            participant = bool(subject_ids & discourse)
            distance = max(
                0, block.global_index - int(row["reveal_global_index"])
            )
            score = (
                100 * int(direct)
                + 80 * int(participant)
                + 50 * int(distance <= 3)
                + 30 * int(str(row["memory_type"]) == "open_question")
                + 25 * int(bool(row["high_impact"]))
                + int(float(row["confidence"]) * 20)
                - min(distance, 50)
            )
            record = self._record_from_row(row, memory_subjects)
            required = bool(
                (record.high_impact and (direct or participant))
                or (
                    record.visibility == "render_only"
                    and (direct or participant)
                )
                or (
                    record.memory_type == "explicit_fact"
                    and record.status == "verified"
                    and direct
                )
            )
            rendered_size = len(
                render_narrative_memory(record, required=required)
            ) + 1
            ranked.append(
                (
                    required,
                    score,
                    record.reveal_global_index,
                    record.id,
                    rendered_size,
                    record,
                )
            )
        ranked.sort(key=lambda item: (-int(item[0]), -item[1], -item[2], item[3]))
        required_rows = [item for item in ranked if item[0]]
        required_chars = sum(item[4] for item in required_rows)
        if required_chars > max_chars:
            raise NarrativeContextOverflow(
                f"{block.id} required narrative context {required_chars} "
                f"exceeds budget {max_chars}"
            )
        selected = list(required_rows)
        used = required_chars
        for item in ranked:
            if item[0]:
                continue
            if used + item[4] > max_chars:
                continue
            selected.append(item)
            used += item[4]
        return NarrativeRetrieval(
            memories=tuple(item[5] for item in selected),
            required_memory_ids=tuple(item[5].id for item in required_rows),
            rendered_chars=used,
        )
