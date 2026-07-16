"""Plan precise schema-8 revalidation work from persisted dependency indexes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

from .database import V4Database, utc_now


_COMPLETED_STATUSES = ("completed", "completed_with_warnings")
_MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_PAYLOAD_IDS = 64
_MAX_SUBJECT_ID_CHARS = 256
_MAX_RESULT_SUBJECTS = 128
_MAX_RESULT_REASONS = 256


@dataclass
class _Candidate:
    translation_id: int
    block_id: str
    from_version: int
    source_hash: str
    changes: dict[int, dict[str, Any]] = field(default_factory=dict)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _change_set_hash(change_ids: Sequence[int], to_version: int) -> str:
    canonical = _canonical_json(
        {
            "change_ids": sorted(set(int(value) for value in change_ids)),
            "to_knowledge_version": int(to_version),
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _task_id(translation_id: int, change_hash: str) -> str:
    digest = hashlib.sha256(
        f"{int(translation_id)}:{change_hash}".encode("utf-8")
    ).hexdigest()
    return f"revalidate_{digest[:24]}"


def _result_change_ids(raw: Any) -> set[int]:
    try:
        payload = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict) or not isinstance(payload.get("change_ids"), list):
        return set()
    values: set[int] = set()
    for value in payload["change_ids"]:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        values.add(value)
    return values


class RevalidationPlanner:
    """Create or merge pending tasks without executing validation work."""

    def __init__(self, database: V4Database):
        if not isinstance(database, V4Database):
            raise TypeError("database must be a V4Database")
        self.database = database

    @staticmethod
    def _normalize_change_ids(change_ids: Sequence[int]) -> list[int]:
        if isinstance(change_ids, (str, bytes)) or not isinstance(change_ids, Sequence):
            raise TypeError("change_ids must be a sequence of positive integers")
        normalized: set[int] = set()
        for value in change_ids:
            if isinstance(value, bool):
                raise ValueError("change_ids must contain positive integers")
            if isinstance(value, int):
                change_id = value
            elif isinstance(value, str) and value.strip().isdigit():
                change_id = int(value.strip())
            else:
                raise ValueError("change_ids must contain positive integers")
            if change_id <= 0:
                raise ValueError("change_ids must contain positive integers")
            normalized.add(change_id)
        return sorted(normalized)

    @staticmethod
    def _safe_payload_subjects(row: sqlite3.Row) -> list[tuple[str, str]]:
        """Read only explicitly authorized, bounded subject fields."""

        raw = str(row["payload_json"] or "{}")
        if len(raw.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict):
            return []
        kind = str(row["change_kind"] or "").casefold()
        allowed: dict[str, tuple[str, ...]] = {}
        list_fields: dict[str, tuple[str, ...]] = {}
        if any(token in kind for token in ("merge", "redirect", "subject_link")):
            allowed["concept"] = (
                "canonical_concept_id",
                "retired_concept_id",
                "old_concept_id",
                "new_concept_id",
            )
            list_fields["concept"] = ("merged_concept_ids", "concept_ids")
        if "rule" in kind:
            allowed["rule"] = ("rule_id", "old_rule_id", "new_rule_id")
            list_fields["rule"] = ("rule_ids",)
        if "claim" in kind:
            allowed["claim"] = ("claim_id",)
            list_fields["claim"] = ("claim_ids",)
        if any(token in kind for token in ("source_form", "alias", "lexeme")):
            allowed["lexeme"] = ("lexeme_id", "old_lexeme_id", "new_lexeme_id")
            list_fields["lexeme"] = ("lexeme_ids",)
        subjects: set[tuple[str, str]] = set()
        for subject_type, keys in allowed.items():
            for key in keys:
                value = payload.get(key)
                if isinstance(value, str):
                    subject_id = value.strip()
                    if 0 < len(subject_id) <= _MAX_SUBJECT_ID_CHARS:
                        subjects.add((subject_type, subject_id))
        for subject_type, keys in list_fields.items():
            for key in keys:
                values = payload.get(key)
                if not isinstance(values, list) or len(values) > _MAX_PAYLOAD_IDS:
                    continue
                for value in values:
                    if isinstance(value, str):
                        subject_id = value.strip()
                        if 0 < len(subject_id) <= _MAX_SUBJECT_ID_CHARS:
                            subjects.add((subject_type, subject_id))
        return sorted(subjects)

    @staticmethod
    def _dependency_snapshot_is_current(row: sqlite3.Row) -> bool:
        source_hash = str(row["source_hash"] or "")
        source_text = str(row["source_text"] or "")
        if (
            len(source_hash) == 64
            and all(character in "0123456789abcdef" for character in source_hash)
            and hashlib.sha256(source_text.encode("utf-8")).hexdigest() != source_hash
        ):
            return False
        raw_spans = str(row["source_spans_json"] or "[]")
        if raw_spans in {"", "[]"}:
            return True
        try:
            spans = json.loads(raw_spans)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(spans, list) or len(spans) > 128:
            return False
        matched_form = str(row["matched_form"] or "")
        for span in spans:
            if (
                not isinstance(span, list)
                or len(span) != 2
                or isinstance(span[0], bool)
                or isinstance(span[1], bool)
                or not isinstance(span[0], int)
                or not isinstance(span[1], int)
            ):
                return False
            start, end = span
            if not 0 <= start < end <= len(source_text):
                return False
            if matched_form and source_text[start:end] != matched_form:
                return False
        return True

    @staticmethod
    def _empty_result(requested: int = 0, effective: int = 0) -> dict[str, Any]:
        return {
            "requested": requested,
            "effective": effective,
            "planned": 0,
            "created": 0,
            "merged": 0,
            "unchanged": 0,
            "retired": 0,
        }

    def plan(self, change_ids: Sequence[int]) -> dict[str, Any]:
        normalized = self._normalize_change_ids(change_ids)
        if not normalized:
            return self._empty_result()
        with self.database.transaction() as connection:
            with self.database._method_savepoint(connection, "revalidation_plan"):
                return self._plan_in_transaction(connection, normalized)

    def _plan_in_transaction(
        self, connection: sqlite3.Connection, change_ids: list[int]
    ) -> dict[str, Any]:
        connection.execute(
            "CREATE TEMP TABLE revalidation_input(id INTEGER PRIMARY KEY)"
        )
        connection.executemany(
            "INSERT INTO revalidation_input(id) VALUES(?)",
            [(change_id,) for change_id in change_ids],
        )
        rows = connection.execute(
            """SELECT kc.* FROM knowledge_changes kc
               JOIN revalidation_input input ON input.id=kc.id
               ORDER BY kc.id"""
        ).fetchall()
        found = {int(row["id"]) for row in rows}
        missing = sorted(set(change_ids) - found)
        if missing:
            raise KeyError(f"unknown knowledge change: {missing[0]}")

        effective_rows = [
            row
            for row in rows
            if int(row["impact_level"]) > 0
            and str(row["old_fingerprint"] or "")
            != str(row["new_fingerprint"] or "")
        ]
        retired_payload = _canonical_json({"reason": "translation_inactive"})
        retired = connection.execute(
            """UPDATE revalidation_tasks AS task
               SET status='resolved_noop', result_json=?, resolved_at=?
               WHERE task.status='pending'
                 AND NOT EXISTS(
                     SELECT 1 FROM translation_versions tv
                     WHERE tv.id=task.translation_id AND tv.active=1
                 )""",
            (retired_payload, utc_now()),
        ).rowcount
        if not effective_rows:
            result = self._empty_result(len(change_ids), 0)
            result["retired"] = int(retired)
            return result

        connection.execute(
            """CREATE TEMP TABLE revalidation_subjects(
                   change_id INTEGER NOT NULL,
                   knowledge_version INTEGER NOT NULL,
                   impact_level INTEGER NOT NULL,
                   change_kind TEXT NOT NULL,
                   subject_type TEXT NOT NULL,
                   subject_id TEXT NOT NULL,
                   new_fingerprint TEXT NOT NULL,
                   PRIMARY KEY(change_id, subject_type, subject_id)
               )"""
        )
        subject_rows: list[tuple[Any, ...]] = []
        for row in effective_rows:
            subjects = {
                (str(row["subject_type"]).strip(), str(row["subject_id"]).strip())
            }
            subjects.update(self._safe_payload_subjects(row))
            for subject_type, subject_id in sorted(subjects):
                if (
                    not subject_type
                    or not subject_id
                    or len(subject_type) > 64
                    or len(subject_id) > _MAX_SUBJECT_ID_CHARS
                ):
                    continue
                subject_rows.append(
                    (
                        int(row["id"]),
                        int(row["knowledge_version"]),
                        int(row["impact_level"]),
                        str(row["change_kind"]),
                        subject_type,
                        subject_id,
                        str(row["new_fingerprint"] or ""),
                    )
                )
        connection.executemany(
            """INSERT OR IGNORE INTO revalidation_subjects(
                   change_id, knowledge_version, impact_level, change_kind,
                   subject_type, subject_id, new_fingerprint)
               VALUES(?, ?, ?, ?, ?, ?, ?)""",
            subject_rows,
        )

        candidates: dict[int, _Candidate] = {}
        dependency_rows = connection.execute(
            """SELECT DISTINCT
                      tv.id translation_id, tv.block_id,
                      tv.knowledge_version from_version,
                      b.source_hash, b.source_text,
                      d.matched_form, d.source_spans_json,
                      subject.change_id, subject.knowledge_version,
                      subject.impact_level, subject.change_kind,
                      subject.subject_type, subject.subject_id
               FROM revalidation_subjects subject
               JOIN dependencies d
                 ON d.dependency_type=subject.subject_type
                AND d.dependency_id=subject.subject_id
               JOIN translation_versions tv ON tv.id=d.translation_id
               JOIN knowledge_versions translated_version
                 ON translated_version.id=tv.knowledge_version
               JOIN blocks b ON b.id=tv.block_id
               JOIN source_editions edition
                 ON edition.id=b.source_edition_id AND edition.active=1
               WHERE tv.active=1 AND tv.pipeline='parallel_v4'
                 AND tv.status IN ('completed','completed_with_warnings')
                 AND b.status IN ('completed','completed_with_warnings')
                 AND d.dependency_fingerprint<>subject.new_fingerprint
               ORDER BY tv.id, subject.change_id, subject.subject_type, subject.subject_id"""
        ).fetchall()
        for row in dependency_rows:
            if not self._dependency_snapshot_is_current(row):
                continue
            self._add_candidate(candidates, row, "dependency")

        occurrence_rows = connection.execute(
            """SELECT DISTINCT
                      tv.id translation_id, tv.block_id,
                      tv.knowledge_version from_version,
                      b.source_hash,
                      subject.change_id, subject.knowledge_version,
                      subject.impact_level, subject.change_kind,
                      subject.subject_type, subject.subject_id
               FROM revalidation_subjects subject
               JOIN form_occurrences occurrence
                 ON occurrence.lexeme_id=subject.subject_id
                AND subject.subject_type='lexeme'
               JOIN blocks b ON b.id=occurrence.block_id
               JOIN source_editions edition
                 ON edition.id=b.source_edition_id AND edition.active=1
               JOIN translation_versions tv ON tv.block_id=b.id
               JOIN knowledge_versions translated_version
                 ON translated_version.id=tv.knowledge_version
               WHERE occurrence.source_hash=b.source_hash
                 AND occurrence.start_offset>=0
                 AND occurrence.end_offset<=length(b.source_text)
                 AND substr(
                       b.source_text,
                       occurrence.start_offset+1,
                       occurrence.end_offset-occurrence.start_offset
                     )=occurrence.source_form
                 AND tv.active=1 AND tv.pipeline='parallel_v4'
                 AND tv.status IN ('completed','completed_with_warnings')
                 AND b.status IN ('completed','completed_with_warnings')
                 AND NOT EXISTS(
                     SELECT 1 FROM dependencies existing
                     WHERE existing.translation_id=tv.id
                       AND existing.dependency_type='lexeme'
                       AND existing.dependency_id=subject.subject_id
                 )
               UNION
               SELECT DISTINCT
                      tv.id translation_id, tv.block_id,
                      tv.knowledge_version from_version,
                      b.source_hash,
                      subject.change_id, subject.knowledge_version,
                      subject.impact_level, subject.change_kind,
                      subject.subject_type, subject.subject_id
               FROM revalidation_subjects subject
               JOIN concept_lexemes link
                 ON link.concept_id=subject.subject_id
                AND link.retired_version IS NULL
                AND subject.subject_type='concept'
               JOIN form_occurrences occurrence ON occurrence.lexeme_id=link.lexeme_id
               JOIN blocks b ON b.id=occurrence.block_id
               JOIN source_editions edition
                 ON edition.id=b.source_edition_id AND edition.active=1
               JOIN translation_versions tv ON tv.block_id=b.id
               JOIN knowledge_versions translated_version
                 ON translated_version.id=tv.knowledge_version
               WHERE occurrence.source_hash=b.source_hash
                 AND occurrence.start_offset>=0
                 AND occurrence.end_offset<=length(b.source_text)
                 AND substr(
                       b.source_text,
                       occurrence.start_offset+1,
                       occurrence.end_offset-occurrence.start_offset
                     )=occurrence.source_form
                 AND tv.active=1 AND tv.pipeline='parallel_v4'
                 AND tv.status IN ('completed','completed_with_warnings')
                 AND b.status IN ('completed','completed_with_warnings')
                 AND NOT EXISTS(
                     SELECT 1 FROM dependencies existing
                     WHERE existing.translation_id=tv.id
                       AND existing.dependency_type='concept'
                       AND existing.dependency_id=subject.subject_id
                 )
               ORDER BY translation_id, change_id, subject_type, subject_id"""
        ).fetchall()
        for row in occurrence_rows:
            self._add_candidate(candidates, row, "occurrence")

        counters = {"created": 0, "merged": 0, "unchanged": 0}
        for candidate in candidates.values():
            outcome = self._upsert_candidate(connection, candidate)
            if outcome is not None:
                counters[outcome] += 1
        return {
            "requested": len(change_ids),
            "effective": len(effective_rows),
            "planned": sum(counters.values()),
            **counters,
            "retired": int(retired),
        }

    @staticmethod
    def _add_candidate(
        candidates: dict[int, _Candidate], row: sqlite3.Row, via: str
    ) -> None:
        translation_id = int(row["translation_id"])
        candidate = candidates.setdefault(
            translation_id,
            _Candidate(
                translation_id=translation_id,
                block_id=str(row["block_id"]),
                from_version=int(row["from_version"]),
                source_hash=str(row["source_hash"]),
            ),
        )
        change_id = int(row["change_id"])
        detail = candidate.changes.setdefault(
            change_id,
            {
                "knowledge_version": int(row["knowledge_version"]),
                "impact_level": int(row["impact_level"]),
                "change_kind": str(row["change_kind"]),
                "subjects": set(),
                "via": set(),
            },
        )
        detail["subjects"].add(
            (str(row["subject_type"]), str(row["subject_id"]))
        )
        detail["via"].add(via)

    @staticmethod
    def _change_rows(
        connection: sqlite3.Connection, change_ids: set[int]
    ) -> dict[int, sqlite3.Row]:
        rows: dict[int, sqlite3.Row] = {}
        ordered = sorted(change_ids)
        for start in range(0, len(ordered), 500):
            chunk = ordered[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in connection.execute(
                f"""SELECT id, knowledge_version, impact_level, change_kind,
                            subject_type, subject_id
                     FROM knowledge_changes WHERE id IN ({placeholders})""",
                chunk,
            ).fetchall():
                rows[int(row["id"])] = row
        return rows

    def _upsert_candidate(
        self, connection: sqlite3.Connection, candidate: _Candidate
    ) -> str | None:
        tasks = connection.execute(
            """SELECT * FROM revalidation_tasks
               WHERE translation_id=? AND status IN ('pending','validating')
               ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at, id""",
            (candidate.translation_id,),
        ).fetchall()
        pending = [row for row in tasks if row["status"] == "pending"]
        validating_ids: set[int] = set()
        for row in tasks:
            if row["status"] == "validating":
                validating_ids.update(_result_change_ids(row["result_json"]))
        candidate_ids = set(candidate.changes) - validating_ids
        if not candidate_ids:
            return None

        existing_ids: set[int] = set()
        current: sqlite3.Row | None = pending[0] if pending else None
        if current is not None:
            existing_ids.update(_result_change_ids(current["result_json"]))
            for duplicate in pending[1:]:
                existing_ids.update(_result_change_ids(duplicate["result_json"]))
                connection.execute(
                    """UPDATE revalidation_tasks
                       SET status='resolved_noop', result_json=?, resolved_at=?
                       WHERE id=?""",
                    (
                        _canonical_json({"reason": "duplicate_pending_task"}),
                        utc_now(),
                        duplicate["id"],
                    ),
                )
        merged_ids = existing_ids | candidate_ids
        if current is not None and merged_ids == existing_ids:
            return "unchanged"

        change_rows = self._change_rows(connection, merged_ids)
        if set(change_rows) != merged_ids:
            raise ValueError("pending revalidation task references unknown changes")
        to_version = max(int(row["knowledge_version"]) for row in change_rows.values())
        impact = max(int(row["impact_level"]) for row in change_rows.values())
        change_hash = _change_set_hash(sorted(merged_ids), to_version)
        conflict = connection.execute(
            """SELECT id FROM revalidation_tasks
               WHERE translation_id=? AND change_set_hash=?
                 AND (? IS NULL OR id<>?)""",
            (
                candidate.translation_id,
                change_hash,
                current["id"] if current is not None else None,
                current["id"] if current is not None else None,
            ),
        ).fetchone()
        if conflict is not None:
            return "unchanged" if current is not None else None

        result_json = self._task_result(candidate, merged_ids, change_rows)
        if current is None:
            connection.execute(
                """INSERT INTO revalidation_tasks(
                       id, translation_id, block_id, from_knowledge_version,
                       to_knowledge_version, change_set_hash, impact_level,
                       status, action, attempts, result_json, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', '', 0, ?, ?)""",
                (
                    _task_id(candidate.translation_id, change_hash),
                    candidate.translation_id,
                    candidate.block_id,
                    candidate.from_version,
                    to_version,
                    change_hash,
                    impact,
                    result_json,
                    utc_now(),
                ),
            )
            return "created"
        connection.execute(
            """UPDATE revalidation_tasks
               SET to_knowledge_version=?, change_set_hash=?, impact_level=?,
                   result_json=?
               WHERE id=? AND status='pending'""",
            (to_version, change_hash, impact, result_json, current["id"]),
        )
        return "merged"

    @staticmethod
    def _task_result(
        candidate: _Candidate,
        change_ids: set[int],
        change_rows: dict[int, sqlite3.Row],
    ) -> str:
        subjects: set[tuple[str, str]] = set()
        reasons: list[dict[str, Any]] = []
        for change_id in sorted(change_ids):
            detail = candidate.changes.get(change_id)
            if detail is None:
                row = change_rows[change_id]
                change_subjects = {
                    (str(row["subject_type"]), str(row["subject_id"]))
                }
                vias = ["dependency"]
                change_kind = str(row["change_kind"])
            else:
                change_subjects = set(detail["subjects"])
                vias = sorted(detail["via"])
                change_kind = str(detail["change_kind"])
            subjects.update(change_subjects)
            reasons.append(
                {
                    "change_id": change_id,
                    "change_kind": change_kind,
                    "via": vias,
                }
            )
        ordered_subjects = sorted(subjects)
        payload = {
            "change_ids": sorted(change_ids),
            "subjects": [
                {"subject_type": subject_type, "subject_id": subject_id}
                for subject_type, subject_id in ordered_subjects[:_MAX_RESULT_SUBJECTS]
            ],
            "omitted_subjects": max(0, len(ordered_subjects) - _MAX_RESULT_SUBJECTS),
            "reasons": reasons[:_MAX_RESULT_REASONS],
            "omitted_reasons": max(0, len(reasons) - _MAX_RESULT_REASONS),
            "source_hash": candidate.source_hash,
        }
        return _canonical_json(payload)
