"""Frozen coreference contracts and conservative deterministic resolution.

This module prepares and validates future model requests, but the deterministic
path is entirely local and persists its decisions without calling a model.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import hashlib
import json
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, ValidationError

from .database import V4Database, normalize_english_form, stable_id, utc_now
from .models import (
    CoreferenceCase,
    CoreferenceConceptAnchor,
    CoreferenceMention,
    CoreferenceResponse,
    CoreferenceTypeObservation,
)


COREFERENCE_PROTOCOL_VERSION = "coreference-v1"
MAX_PROTOCOL_CASES = 999
MAX_CASE_MENTIONS = 8
MAX_CONTEXT_CHARS = 400
MAX_LEXEME_TYPE_OBSERVATIONS = 8
MAX_MENTION_TYPE_OBSERVATIONS = 4
MAX_CONCEPT_ANCHORS = 8
MAX_FREE_TEXT_CHARS = 200
MAX_OBSERVATION_SOURCE_CHARS = 160
MAX_KIND_CHARS = 80
# The capped request contains at most 20,320 free-text characters.  JSON may
# escape each as six bytes; 192 KiB leaves a fixed envelope for keys and IDs.
MAX_CASE_PAYLOAD_BYTES = 192 * 1024


class CoreferenceProtocolError(ValueError):
    """A model response does not conform to its frozen request cases."""


class _ProtectedBindingConflict(RuntimeError):
    """A deterministic same case contains a protected, unbindable member."""


def _pure_validation_data(value: Any) -> Any:
    """Remove Pydantic instances so nested objects cannot skip revalidation."""

    if isinstance(value, BaseModel):
        return _pure_validation_data(value.model_dump(warnings=False))
    if isinstance(value, Mapping):
        return {
            key: _pure_validation_data(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_pure_validation_data(item) for item in value]
    return value


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _observation_payload(
    observation: CoreferenceTypeObservation,
) -> dict[str, Any]:
    return {
        "adjudication_id": observation.adjudication_id,
        "concept_id": observation.concept_id,
        "confidence": observation.confidence,
        "evidence_id": observation.evidence_id,
        "kind": _bounded_text(observation.kind, MAX_KIND_CHARS),
        "mention_id": observation.mention_id,
        "observation_id": observation.observation_id,
        "source": _bounded_text(
            observation.source, MAX_OBSERVATION_SOURCE_CHARS
        ),
    }


def _anchor_payload(anchor: CoreferenceConceptAnchor) -> dict[str, Any]:
    return {
        "anchor_mention_id": anchor.anchor_mention_id,
        "canonical_source": _bounded_text(
            anchor.canonical_source, MAX_FREE_TEXT_CHARS
        ),
        "concept_id": anchor.concept_id,
        "evidence_id": anchor.evidence_id,
        "kind": _bounded_text(anchor.kind, MAX_KIND_CHARS),
        "role": _bounded_text(anchor.role, MAX_KIND_CHARS),
        "status": _bounded_text(anchor.status, MAX_KIND_CHARS),
    }


def _mention_payload(mention: CoreferenceMention) -> dict[str, Any]:
    return {
        "block_id": mention.block_id,
        "block_kind": _bounded_text(mention.block_kind, MAX_KIND_CHARS),
        "concept_anchor_ids": list(mention.concept_anchor_ids),
        "context": _bounded_text(mention.context, MAX_CONTEXT_CHARS),
        "context_source": _bounded_text(
            mention.context_source, MAX_KIND_CHARS
        ),
        "discourse_function": _bounded_text(
            mention.discourse_function, MAX_KIND_CHARS
        ),
        "evidence_id": mention.evidence_id,
        "mention_id": mention.mention_id,
        "paragraph_id": mention.paragraph_id,
        "request_id": mention.request_id,
        "source_edition_hash": mention.source_edition_hash,
        "source_form": _bounded_text(
            mention.source_form, MAX_FREE_TEXT_CHARS
        ),
        "source_hash": mention.source_hash,
        "type_observations": [
            _observation_payload(item) for item in mention.type_observations
        ],
    }


def payload_dict(case: CoreferenceCase) -> dict[str, Any]:
    """Return the run-independent request object for one frozen case."""

    return {
        "case": {
            "case_id": case.case_id,
            "concept_anchors": [
                _anchor_payload(item) for item in case.concept_anchors
            ],
            "knowledge_version": case.knowledge_version,
            "lexeme": {
                "canonical_form": _bounded_text(
                    case.canonical_form, MAX_FREE_TEXT_CHARS
                ),
                "id": case.lexeme_id,
                "language": _bounded_text(case.language, MAX_KIND_CHARS),
                "normalized_form": _bounded_text(
                    case.normalized_form, MAX_FREE_TEXT_CHARS
                ),
            },
            "mention_set_id": case.mention_set_id,
            "mentions": [_mention_payload(item) for item in case.mentions],
            "type_observations": [
                _observation_payload(item) for item in case.type_observations
            ],
        },
        "protocol_version": COREFERENCE_PROTOCOL_VERSION,
    }


def payload_bytes(case: CoreferenceCase) -> bytes:
    """Serialize a frozen case as canonical UTF-8 JSON."""

    frozen = _canonical_json_bytes(payload_dict(case))
    if len(frozen) > MAX_CASE_PAYLOAD_BYTES:
        raise CoreferenceProtocolError(
            "coreference payload exceeds the fixed request budget"
        )
    return frozen


def payload_hash(case: CoreferenceCase) -> str:
    return hashlib.sha256(payload_bytes(case)).hexdigest()


def _validated_model_names(model_names: Sequence[str]) -> tuple[str, str]:
    if isinstance(model_names, (str, bytes)):
        raise ValueError("coreference requests require two distinct model names")
    raw_names = tuple(model_names)
    if len(raw_names) != 2 or any(
        not isinstance(name, str) for name in raw_names
    ):
        raise ValueError("coreference requests require two distinct model names")
    names = tuple(name.strip() for name in raw_names)
    if any(not name for name in names) or len(set(names)) != 2:
        raise ValueError("coreference requests require two distinct model names")
    return (names[0], names[1])


def cache_key(case: CoreferenceCase, model_names: Sequence[str]) -> str:
    """Build an order-insensitive key for a two-model request set."""

    names = _validated_model_names(model_names)
    key_material = _canonical_json_bytes(
        {
            "models": sorted(names),
            "payload_hash": payload_hash(case),
            "protocol_version": COREFERENCE_PROTOCOL_VERSION,
        }
    )
    return f"coreference-cache_{hashlib.sha256(key_material).hexdigest()}"


def _paragraph_order(value: str) -> tuple[int, int | str]:
    matched = re.fullmatch(r"P(\d+)", value)
    if matched:
        return (0, int(matched.group(1)))
    return (1, value)


def _mention_order(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(row["global_index"]),
        _paragraph_order(str(row["paragraph_id"])),
        int(row["mention_id"]),
    )


def _paragraph_context(source_text: str, paragraph_id: str) -> str:
    paragraphs = re.split(r"\n\s*\n", source_text)
    matched = re.fullmatch(r"P(\d+)", paragraph_id)
    if matched:
        index = int(matched.group(1)) - 1
        if 0 <= index < len(paragraphs):
            return paragraphs[index]
    return source_text


def _bounded_context(row: Mapping[str, Any]) -> tuple[str, str]:
    quote = str(row["evidence_quote"] or "").strip()
    if quote:
        value = quote
        source = "evidence_quote"
    else:
        value = _paragraph_context(
            str(row["source_text"] or ""), str(row["paragraph_id"])
        ).strip()
        source = "paragraph"
    if len(value) > MAX_CONTEXT_CHARS:
        value = value[: MAX_CONTEXT_CHARS - 1] + "…"
    return value, source


def _select_observations(
    observations: Iterable[CoreferenceTypeObservation],
    limit: int,
) -> tuple[CoreferenceTypeObservation, ...]:
    """Keep deterministic early representatives without duplicate-kind bias."""

    ordered = sorted(observations, key=lambda item: item.observation_id)
    if not ordered or limit <= 0:
        return ()
    selected: dict[int, CoreferenceTypeObservation] = {}

    def add(observation: CoreferenceTypeObservation) -> None:
        if len(selected) < limit:
            selected.setdefault(observation.observation_id, observation)

    add(ordered[0])
    add(ordered[-1])
    seen_kinds = {item.kind for item in selected.values()}
    for observation in ordered:
        if observation.kind in seen_kinds:
            continue
        add(observation)
        seen_kinds.add(observation.kind)
        if len(selected) >= limit:
            break
    for observation in ordered:
        add(observation)
        if len(selected) >= limit:
            break
    return tuple(
        sorted(selected.values(), key=lambda item: item.observation_id)
    )


def _select_mentions(
    rows: Sequence[Mapping[str, Any]],
    observations: Sequence[CoreferenceTypeObservation],
) -> list[Mapping[str, Any]]:
    ordered = sorted(rows, key=_mention_order)
    if len(ordered) <= MAX_CASE_MENTIONS:
        return ordered

    by_id = {int(row["mention_id"]): row for row in ordered}
    mention_kinds: dict[int, set[str]] = defaultdict(set)
    for observation in observations:
        if observation.mention_id in by_id:
            mention_kinds[int(observation.mention_id)].add(observation.kind)

    selected: dict[int, Mapping[str, Any]] = {}

    def add(row: Mapping[str, Any]) -> None:
        if len(selected) < MAX_CASE_MENTIONS:
            selected.setdefault(int(row["mention_id"]), row)

    # The two temporal anchors are always retained.
    add(ordered[0])
    add(ordered[-1])

    # Retain a stable representative for every observed, mention-bound kind.
    for kind in sorted({item.kind for item in observations}):
        representative = next(
            (
                row
                for row in ordered
                if kind in mention_kinds.get(int(row["mention_id"]), set())
            ),
            None,
        )
        if representative is not None:
            add(representative)

    order_index = {
        int(row["mention_id"]): index for index, row in enumerate(ordered)
    }
    # The persisted schema has no co-occurring-entity set.  Block, paragraph,
    # block kind, observed type, and temporal distance are the stable proxy.
    while len(selected) < MAX_CASE_MENTIONS:
        remaining = [
            row for row in ordered if int(row["mention_id"]) not in selected
        ]
        if not remaining:
            break
        used_blocks = {str(row["block_id"]) for row in selected.values()}
        used_paragraphs = {
            (str(row["block_id"]), str(row["paragraph_id"]))
            for row in selected.values()
        }
        used_block_kinds = {str(row["block_kind"]) for row in selected.values()}
        used_types = {
            kind
            for row in selected.values()
            for kind in mention_kinds.get(int(row["mention_id"]), set())
        }
        selected_indexes = [
            order_index[int(row["mention_id"])] for row in selected.values()
        ]

        def diversity_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
            mention_id = int(row["mention_id"])
            index = order_index[mention_id]
            temporal_distance = min(abs(index - item) for item in selected_indexes)
            new_types = len(mention_kinds.get(mention_id, set()) - used_types)
            return (
                -int(str(row["block_id"]) not in used_blocks),
                -int(
                    (str(row["block_id"]), str(row["paragraph_id"]))
                    not in used_paragraphs
                ),
                -int(str(row["block_kind"]) not in used_block_kinds),
                -new_types,
                -temporal_distance,
                _mention_order(row),
            )

        add(min(remaining, key=diversity_key))

    return sorted(selected.values(), key=_mention_order)


def _decoded_mapping(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _anchor_member_ids(value: Any) -> set[int]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    found: set[int] = set()

    def collect(item: Any) -> None:
        if isinstance(item, bool):
            return
        if isinstance(item, int):
            found.add(item)
        elif isinstance(item, str) and item.isdigit():
            found.add(int(item))
        elif isinstance(item, Mapping):
            for key in ("mention_id", "id", "members", "mention_ids"):
                if key in item:
                    collect(item[key])
        elif isinstance(item, (list, tuple)):
            for nested in item:
                collect(nested)

    collect(decoded)
    return found


_TITLE_OBSERVATION_KINDS = {
    "book_title",
    "chapter_title",
    "title",
    "work",
    "work_title",
}
_TITLE_DIRECTORY_BLOCK_KINDS = {
    "chapter_title",
    "contents",
    "front_matter",
    "heading",
    "index",
    "title",
    "toc",
}


class CoreferenceCoordinator:
    """Freeze same-lexeme cases and coordinate their local protocol."""

    def __init__(self, database: V4Database):
        self.database = database

    def freeze_cases(self, max_cases: int | None = None) -> tuple[CoreferenceCase, ...]:
        if max_cases is not None:
            if isinstance(max_cases, bool) or not isinstance(max_cases, int):
                raise ValueError("max_cases must be an integer or None")
            if max_cases < 0:
                raise ValueError("max_cases cannot be negative")
            if max_cases == 0:
                return ()

        with closing(self.database.connect()) as connection:
            connection.execute("BEGIN")
            version_row = connection.execute(
                "SELECT MAX(id) AS knowledge_version FROM knowledge_versions"
            ).fetchone()
            knowledge_version = int(version_row["knowledge_version"])

            candidate_sql = """SELECT l.id, l.language, l.normalized_form,
                                      l.canonical_form
                               FROM lexemes l
                               WHERE l.retired_version IS NULL
                                 AND EXISTS(
                                     SELECT 1
                                     FROM mentions active_m
                                     JOIN blocks active_b
                                       ON active_b.id=active_m.block_id
                                     JOIN source_editions active_se
                                       ON active_se.id=active_b.source_edition_id
                                      AND active_se.active=1
                                     WHERE active_m.lexeme_id=l.id)
                                 AND (
                                     (SELECT COUNT(*)
                                      FROM mentions counted_m
                                      JOIN blocks counted_b
                                        ON counted_b.id=counted_m.block_id
                                      JOIN source_editions counted_se
                                        ON counted_se.id=counted_b.source_edition_id
                                       AND counted_se.active=1
                                      WHERE counted_m.lexeme_id=l.id) >= 2
                                     OR
                                     (SELECT COUNT(DISTINCT observed.kind)
                                      FROM concept_type_observations observed
                                      WHERE observed.lexeme_id=l.id
                                        AND observed.retired_version IS NULL) >= 2
                                 )
                               ORDER BY l.normalized_form, l.id"""
            candidate_parameters: tuple[Any, ...] = ()
            if max_cases is not None:
                candidate_sql += " LIMIT ?"
                candidate_parameters = (min(max_cases, MAX_PROTOCOL_CASES),)
            candidate_rows = connection.execute(
                candidate_sql, candidate_parameters
            ).fetchall()
            if max_cases is None and len(candidate_rows) > MAX_PROTOCOL_CASES:
                candidate_rows = candidate_rows[:MAX_PROTOCOL_CASES]
            selected_lexeme_ids = tuple(
                str(row["id"]) for row in candidate_rows
            )
            if not selected_lexeme_ids:
                connection.rollback()
                return ()
            placeholders = ",".join("?" for _ in selected_lexeme_ids)

            mention_rows = connection.execute(
                f"""SELECT m.id AS mention_id, m.evidence_id, m.block_id,
                          m.paragraph_id, m.source_form, m.discourse_function,
                          m.lexeme_id, m.concept_id,
                          e.evidence_quote,
                          b.block_type AS block_kind, b.source_hash,
                          b.source_text, b.global_index,
                          se.normalized_sha256 AS source_edition_hash,
                          l.language, l.normalized_form, l.canonical_form
                   FROM mentions m
                   JOIN evidence e ON e.id=m.evidence_id
                   JOIN blocks b ON b.id=m.block_id
                   JOIN source_editions se
                     ON se.id=b.source_edition_id AND se.active=1
                   JOIN lexemes l ON l.id=m.lexeme_id
                   WHERE l.retired_version IS NULL
                     AND m.lexeme_id IN ({placeholders})
                   ORDER BY l.normalized_form, l.id, b.global_index,
                            m.paragraph_id, m.id""",
                selected_lexeme_ids,
            ).fetchall()
            observation_rows = connection.execute(
                f"""SELECT o.id AS observation_id, o.lexeme_id, o.mention_id,
                          observed_concept.id AS concept_id, o.evidence_id,
                          o.kind, o.confidence,
                          o.source, o.adjudication_id
                   FROM concept_type_observations o
                   JOIN lexemes l ON l.id=o.lexeme_id
                   LEFT JOIN concepts observed_concept
                     ON observed_concept.id=o.concept_id
                    AND observed_concept.retired_version IS NULL
                   WHERE o.retired_version IS NULL
                     AND l.retired_version IS NULL
                     AND o.lexeme_id IN ({placeholders})
                   ORDER BY o.lexeme_id, COALESCE(o.mention_id, -1),
                            o.kind, o.id""",
                selected_lexeme_ids,
            ).fetchall()
            linked_anchor_rows = connection.execute(
                f"""SELECT cl.lexeme_id, c.id AS concept_id, c.kind,
                          c.canonical_source, c.status, cl.role,
                          c.anchor_mention_id, cl.evidence_id
                   FROM concept_lexemes cl
                   JOIN concepts c ON c.id=cl.concept_id
                   WHERE cl.retired_version IS NULL
                     AND c.retired_version IS NULL
                     AND cl.lexeme_id IN ({placeholders})
                   ORDER BY cl.lexeme_id, c.id,
                            CASE cl.role
                                WHEN 'primary' THEN 0
                                WHEN 'title' THEN 1
                                WHEN 'alias' THEN 2
                                ELSE 3
                            END,
                            COALESCE(cl.evidence_id, -1)""",
                selected_lexeme_ids,
            ).fetchall()
            direct_anchor_rows = connection.execute(
                f"""SELECT DISTINCT m.lexeme_id, c.id AS concept_id, c.kind,
                          c.canonical_source, c.status, 'mention' AS role,
                          c.anchor_mention_id, NULL AS evidence_id
                   FROM mentions m
                   JOIN blocks b ON b.id=m.block_id
                   JOIN source_editions se
                     ON se.id=b.source_edition_id AND se.active=1
                   JOIN concepts c ON c.id=m.concept_id
                   WHERE c.retired_version IS NULL
                     AND m.lexeme_id IN ({placeholders})
                   ORDER BY m.lexeme_id, c.id""",
                selected_lexeme_ids,
            ).fetchall()
            connection.rollback()

        rows_by_lexeme: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in mention_rows:
            rows_by_lexeme[str(row["lexeme_id"])].append(dict(row))

        observations_by_lexeme: dict[
            str, list[CoreferenceTypeObservation]
        ] = defaultdict(list)
        for row in observation_rows:
            observations_by_lexeme[str(row["lexeme_id"])].append(
                CoreferenceTypeObservation(
                    observation_id=int(row["observation_id"]),
                    kind=str(row["kind"]),
                    confidence=float(row["confidence"]),
                    source=str(row["source"]),
                    mention_id=(
                        int(row["mention_id"])
                        if row["mention_id"] is not None
                        else None
                    ),
                    concept_id=(
                        str(row["concept_id"])
                        if row["concept_id"] is not None
                        else None
                    ),
                    evidence_id=(
                        int(row["evidence_id"])
                        if row["evidence_id"] is not None
                        else None
                    ),
                    adjudication_id=(
                        str(row["adjudication_id"])
                        if row["adjudication_id"] is not None
                        else None
                    ),
                )
            )

        anchors_by_lexeme: dict[str, list[CoreferenceConceptAnchor]] = defaultdict(list)
        seen_anchors: set[tuple[Any, ...]] = set()
        for row in (*linked_anchor_rows, *direct_anchor_rows):
            key = (
                str(row["lexeme_id"]),
                str(row["concept_id"]),
            )
            if key in seen_anchors:
                continue
            seen_anchors.add(key)
            anchors_by_lexeme[str(row["lexeme_id"])].append(
                CoreferenceConceptAnchor(
                    concept_id=str(row["concept_id"]),
                    kind=str(row["kind"]),
                    canonical_source=str(row["canonical_source"]),
                    status=str(row["status"]),
                    role=str(row["role"]),
                    anchor_mention_id=(
                        int(row["anchor_mention_id"])
                        if row["anchor_mention_id"] is not None
                        else None
                    ),
                    evidence_id=(
                        int(row["evidence_id"])
                        if row["evidence_id"] is not None
                        else None
                    ),
                )
            )

        pending = [
            (lexeme_id, rows_by_lexeme[lexeme_id])
            for lexeme_id in selected_lexeme_ids
        ]

        cases: list[CoreferenceCase] = []
        for case_index, (lexeme_id, rows) in enumerate(pending, start=1):
            observations = tuple(observations_by_lexeme[lexeme_id])
            selected = _select_mentions(rows, observations)
            selected_ids = {int(row["mention_id"]) for row in selected}
            selected_concept_ids = {
                str(row["concept_id"])
                for row in selected
                if row["concept_id"] is not None
            }
            payload_observations = _select_observations(
                (item for item in observations if item.mention_id is None),
                MAX_LEXEME_TYPE_OBSERVATIONS,
            )
            anchor_role_order = {
                "primary": 0,
                "title": 1,
                "alias": 2,
                "uncertain": 3,
                "mention": 4,
            }

            def binding_priority(item: CoreferenceConceptAnchor) -> int:
                if item.concept_id in selected_concept_ids:
                    return 0
                if item.anchor_mention_id in selected_ids:
                    return 1
                return 2

            prioritized_anchors = sorted(
                anchors_by_lexeme[lexeme_id],
                key=lambda item: (
                    binding_priority(item),
                    anchor_role_order.get(item.role, 5),
                    item.concept_id,
                    item.evidence_id if item.evidence_id is not None else -1,
                ),
            )
            anchors = tuple(
                sorted(
                    prioritized_anchors[:MAX_CONCEPT_ANCHORS],
                    key=lambda item: (
                        item.concept_id,
                        item.role,
                        item.evidence_id if item.evidence_id is not None else -1,
                    ),
                )
            )
            available_anchor_ids = {item.concept_id for item in anchors}
            built_mentions: list[CoreferenceMention] = []
            for mention_index, row in enumerate(selected, start=1):
                mention_id = int(row["mention_id"])
                mention_observations = _select_observations(
                    (
                        item
                        for item in observations
                        if item.mention_id == mention_id
                    ),
                    MAX_MENTION_TYPE_OBSERVATIONS,
                )
                anchor_ids = {
                    item.concept_id
                    for item in anchors
                    if item.anchor_mention_id == mention_id
                }
                if (
                    row["concept_id"] is not None
                    and str(row["concept_id"]) in available_anchor_ids
                ):
                    anchor_ids.add(str(row["concept_id"]))
                context, context_source = _bounded_context(row)
                built_mentions.append(
                    CoreferenceMention(
                        mention_id=mention_id,
                        request_id=f"M{mention_index:03d}",
                        evidence_id=int(row["evidence_id"]),
                        block_id=str(row["block_id"]),
                        paragraph_id=str(row["paragraph_id"]),
                        block_kind=str(row["block_kind"]),
                        source_hash=str(row["source_hash"]),
                        source_edition_hash=str(row["source_edition_hash"]),
                        source_form=str(row["source_form"]),
                        discourse_function=str(row["discourse_function"]),
                        context=context,
                        context_source=context_source,
                        type_observations=mention_observations,
                        concept_anchor_ids=tuple(sorted(anchor_ids)),
                    )
                )
            real_ids = sorted(str(value) for value in selected_ids)
            first = rows[0]
            cases.append(
                CoreferenceCase(
                    case_id=f"R{case_index:03d}",
                    mention_set_id=stable_id(
                        "mention-set", ":".join(real_ids)
                    ),
                    knowledge_version=knowledge_version,
                    lexeme_id=lexeme_id,
                    language=str(first["language"]),
                    normalized_form=str(first["normalized_form"]),
                    canonical_form=str(first["canonical_form"]),
                    mentions=tuple(built_mentions),
                    type_observations=payload_observations,
                    concept_anchors=anchors,
                )
            )
        return tuple(cases)

    @staticmethod
    def _redirect_target(
        connection: sqlite3.Connection,
        concept_id: str,
    ) -> tuple[str | None, bool]:
        return V4Database._active_canonical_concept(connection, concept_id)

    @staticmethod
    def _validate_case_anchors(
        case: CoreferenceCase,
        rows: Sequence[Mapping[str, Any]],
        connection: sqlite3.Connection,
    ) -> None:
        payload_anchor_ids = tuple(
            anchor.concept_id for anchor in case.concept_anchors
        )
        if len(set(payload_anchor_ids)) != len(payload_anchor_ids):
            raise CoreferenceProtocolError(
                "coreference case contains duplicate concept anchors"
            )

        persisted: dict[str, sqlite3.Row] = {}
        for anchor in case.concept_anchors:
            row = connection.execute(
                """SELECT c.kind, c.canonical_source, c.status,
                          c.primary_lexeme_id, c.anchor_mention_id,
                          c.retired_version
                   FROM concepts c WHERE c.id=?""",
                (anchor.concept_id,),
            ).fetchone()
            if row is None or row["retired_version"] is not None:
                raise CoreferenceProtocolError(
                    f"concept anchor is not active: {anchor.concept_id}"
                )
            best_link = connection.execute(
                """SELECT role, evidence_id FROM concept_lexemes
                   WHERE concept_id=? AND lexeme_id=?
                         AND retired_version IS NULL
                   ORDER BY CASE role
                                WHEN 'primary' THEN 0
                                WHEN 'title' THEN 1
                                WHEN 'alias' THEN 2
                                ELSE 3
                            END,
                            COALESCE(evidence_id, -1)
                   LIMIT 1""",
                (anchor.concept_id, case.lexeme_id),
            ).fetchone()
            direct_binding = connection.execute(
                """SELECT 1 FROM mentions m
                   JOIN blocks b ON b.id=m.block_id
                   JOIN source_editions se
                     ON se.id=b.source_edition_id AND se.active=1
                   WHERE m.lexeme_id=? AND m.concept_id=?
                   LIMIT 1""",
                (case.lexeme_id, anchor.concept_id),
            ).fetchone()
            if (
                str(row["primary_lexeme_id"] or "") != case.lexeme_id
                and best_link is None
            ):
                raise CoreferenceProtocolError(
                    f"concept anchor belongs to another lexeme: "
                    f"{anchor.concept_id}"
                )
            expected_role = (
                str(best_link["role"])
                if best_link is not None
                else "mention"
            )
            expected_evidence_id = (
                best_link["evidence_id"]
                if best_link is not None
                else None
            )
            if best_link is None and direct_binding is None:
                raise CoreferenceProtocolError(
                    f"concept anchor is not legal for the current lexeme: "
                    f"{anchor.concept_id}"
                )
            if (
                str(row["kind"]) != anchor.kind
                or str(row["canonical_source"]) != anchor.canonical_source
                or str(row["status"]) != anchor.status
                or expected_role != anchor.role
                or expected_evidence_id != anchor.evidence_id
            ):
                raise CoreferenceProtocolError(
                    f"concept anchor metadata does not match persisted data: "
                    f"{anchor.concept_id}"
                )
            if row["anchor_mention_id"] != anchor.anchor_mention_id:
                raise CoreferenceProtocolError(
                    f"concept anchor mention does not match persisted data: "
                    f"{anchor.concept_id}"
                )
            persisted[anchor.concept_id] = row

        row_by_mention = {
            int(row["mention_id"]): row
            for row in rows
        }
        available = set(payload_anchor_ids)
        for mention in case.mentions:
            if len(set(mention.concept_anchor_ids)) != len(
                mention.concept_anchor_ids
            ):
                raise CoreferenceProtocolError(
                    f"mention {mention.mention_id} contains duplicate anchors"
                )
            for anchor_id in mention.concept_anchor_ids:
                if anchor_id not in available:
                    raise CoreferenceProtocolError(
                        f"mention anchor is not present in case payload: "
                        f"{anchor_id}"
                    )
                mention_row = row_by_mention[mention.mention_id]
                persisted_anchor = persisted[anchor_id]
                if (
                    str(mention_row["concept_id"] or "") != anchor_id
                    and persisted_anchor["anchor_mention_id"]
                    != mention.mention_id
                ):
                    raise CoreferenceProtocolError(
                        f"mention anchor is inconsistent with persisted data: "
                        f"{anchor_id}"
                    )

    @staticmethod
    def _candidate_span(
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> tuple[str, str, int, int, str] | None:
        start = payload.get("start_offset")
        end = payload.get("end_offset")
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end <= len(str(row["source_text"]))
            and normalize_english_form(str(row["source_text"])[start:end])
            == str(row["normalized_form"])
        ):
            return (
                str(row["source_edition_hash"]),
                str(row["block_id"]),
                start,
                end,
                str(row["normalized_form"]),
            )

        candidate_spans = {
            (
                str(row["source_edition_hash"]),
                str(item["block_id"]),
                int(item["start_offset"]),
                int(item["end_offset"]),
                str(row["normalized_form"]),
            )
            for item in connection.execute(
                """SELECT lc.block_id, lc.start_offset, lc.end_offset
                   FROM candidate_resolutions cr
                   JOIN lexical_candidates lc ON lc.id=cr.candidate_id
                   WHERE cr.evidence_id=? AND cr.lexeme_id=?""",
                (row["evidence_id"], row["lexeme_id"]),
            ).fetchall()
        }
        if len(candidate_spans) == 1:
            return next(iter(candidate_spans))

        occurrence_spans = {
            (
                str(row["source_edition_hash"]),
                str(item["block_id"]),
                int(item["start_offset"]),
                int(item["end_offset"]),
                str(row["normalized_form"]),
            )
            for item in connection.execute(
                """SELECT block_id, start_offset, end_offset
                   FROM form_occurrences
                   WHERE lexeme_id=? AND block_id=? AND source_form=?
                         AND source_hash=?""",
                (
                    row["lexeme_id"],
                    row["block_id"],
                    row["source_form"],
                    row["source_hash"],
                ),
            ).fetchall()
        }
        if len(occurrence_spans) == 1:
            return next(iter(occurrence_spans))
        return None

    def _deterministic_evaluation(
        self,
        case: CoreferenceCase,
        connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        mention_ids = tuple(mention.mention_id for mention in case.mentions)
        if len(mention_ids) < 2 or len(set(mention_ids)) != len(mention_ids):
            return {"relation": None, "rule": None, "target": None}
        expected_mention_set_id = stable_id(
            "mention-set", ":".join(sorted(str(value) for value in mention_ids))
        )
        if case.mention_set_id != expected_mention_set_id:
            raise ValueError("coreference case mention_set_id is not canonical")

        placeholders = ",".join("?" for _ in mention_ids)
        rows = connection.execute(
            f"""SELECT m.id AS mention_id, m.evidence_id, m.block_id,
                       m.paragraph_id, m.source_form, m.normalized_form,
                       m.lexeme_id, m.concept_id, e.kind AS evidence_kind,
                       e.evidence_quote, e.payload_json,
                       b.block_type, b.source_text, b.source_hash,
                       se.normalized_sha256 AS source_edition_hash
                FROM mentions m
                JOIN evidence e ON e.id=m.evidence_id
                JOIN blocks b ON b.id=m.block_id
                JOIN source_editions se
                  ON se.id=b.source_edition_id AND se.active=1
                JOIN lexemes l
                  ON l.id=m.lexeme_id AND l.retired_version IS NULL
                WHERE m.id IN ({placeholders})
                ORDER BY m.id""",
            mention_ids,
        ).fetchall()
        if {int(row["mention_id"]) for row in rows} != set(mention_ids):
            raise ValueError(
                "deterministic coreference requires active persisted mentions"
            )
        if any(str(row["lexeme_id"]) != case.lexeme_id for row in rows):
            raise ValueError(
                "deterministic coreference only accepts one lexeme per mention set"
            )
        case_by_id = {mention.mention_id: mention for mention in case.mentions}
        payload_by_id = {
            int(row["mention_id"]): _decoded_mapping(row["payload_json"])
            for row in rows
        }
        self._validate_case_anchors(case, rows, connection)

        raw_anchor_ids = {
            str(row["concept_id"])
            for row in rows
            if row["concept_id"] is not None
        }
        raw_anchor_ids.update(
            anchor_id
            for mention in case.mentions
            for anchor_id in mention.concept_anchor_ids
        )
        raw_anchor_ids.update(
            anchor.concept_id for anchor in case.concept_anchors
        )
        canonical_by_anchor: dict[str, str | None] = {}
        redirected_any = False
        for anchor_id in sorted(raw_anchor_ids):
            canonical, redirected = self._redirect_target(connection, anchor_id)
            canonical_by_anchor[anchor_id] = canonical
            redirected_any = redirected_any or redirected
        canonical_anchor_ids = {
            value for value in canonical_by_anchor.values() if value is not None
        }

        protected_rows = connection.execute(
            """SELECT DISTINCT c.id, c.kind, c.locked, c.status
               FROM concepts c
               LEFT JOIN concept_lexemes cl
                 ON cl.concept_id=c.id AND cl.retired_version IS NULL
               WHERE c.retired_version IS NULL
                 AND (c.id IN (
                        SELECT m.concept_id FROM mentions m
                        WHERE m.id IN ("""
            + placeholders
            + """))
                      OR (cl.lexeme_id=? AND (c.locked=1 OR c.status='verified')))
               ORDER BY c.id""",
            (*mention_ids, case.lexeme_id),
        ).fetchall()
        protected_concept_ids = {
            str(row["id"])
            for row in protected_rows
            if bool(row["locked"]) or str(row["status"]) == "verified"
        }

        locked_relations: list[str] = []
        for decision in connection.execute(
            """SELECT relation, left_anchor_id, right_anchor_id,
                      anchor_members_json
               FROM coreference_decisions
               WHERE lexeme_id=? AND retired_version IS NULL
                 AND relation IN ('same', 'different', 'non_entity')
                 AND (locked=1 OR decision_source='human')
               ORDER BY CASE decision_source WHEN 'human' THEN 0 ELSE 1 END,
                        id""",
            (case.lexeme_id,),
        ).fetchall():
            members = _anchor_member_ids(decision["anchor_members_json"])
            decision_anchors = {
                str(decision["left_anchor_id"]),
                str(decision["right_anchor_id"]),
            }
            if (
                str(decision["relation"]) in {"different", "non_entity"}
                and bool(set(mention_ids) & members)
            ) or set(mention_ids) <= members or (
                len(raw_anchor_ids & decision_anchors) >= 2
            ) or (
                case.mention_set_id in decision_anchors
            ):
                locked_relations.append(str(decision["relation"]))
        if locked_relations:
            unique_locked = set(locked_relations)
            if len(unique_locked) == 1:
                return {
                    "relation": locked_relations[0],
                    "rule": "locked_decision",
                    "target": (
                        next(iter(canonical_anchor_ids))
                        if locked_relations[0] == "same"
                        and len(canonical_anchor_ids) == 1
                        else None
                    ),
                }
            return {"relation": None, "rule": None, "target": None}

        duplicate_subset = False
        duplicate_targets: set[str] = set()
        duplicate_has_invalid_target = False
        for decision in connection.execute(
            """SELECT left_anchor_type, left_anchor_id,
                      right_anchor_type, right_anchor_id,
                      anchor_members_json
               FROM coreference_decisions
               WHERE lexeme_id=? AND retired_version IS NULL
                     AND relation='same'
               ORDER BY id""",
            (case.lexeme_id,),
        ).fetchall():
            previous = _anchor_member_ids(decision["anchor_members_json"])
            if not set(mention_ids) <= previous:
                continue
            duplicate_subset = True
            for side in ("left", "right"):
                if decision[f"{side}_anchor_type"] != "concept":
                    continue
                canonical, _ = self._redirect_target(
                    connection, str(decision[f"{side}_anchor_id"])
                )
                if canonical is None:
                    duplicate_has_invalid_target = True
                else:
                    duplicate_targets.add(canonical)

        if any(value is None for value in canonical_by_anchor.values()):
            return {"relation": None, "rule": None, "target": None}
        if len(canonical_anchor_ids) > 1:
            if (
                duplicate_subset
                and not duplicate_has_invalid_target
                and len(duplicate_targets) == 1
            ):
                return {
                    "relation": "same",
                    "rule": "same_anchor_or_duplicate_subset",
                    "target": next(iter(duplicate_targets)),
                }
            return {"relation": None, "rule": None, "target": None}

        replay_candidates: set[tuple[str, str, str | None]] = set()
        current_evidence_ids = {
            int(row["evidence_id"]) for row in rows
        }
        for decision in connection.execute(
            """SELECT left_anchor_type, left_anchor_id,
                      right_anchor_type, right_anchor_id, relation,
                      votes_json, evidence_ids_json, anchor_members_json
               FROM coreference_decisions
               WHERE lexeme_id=? AND retired_version IS NULL
                     AND decision_source='deterministic'
                     AND relation IN ('same', 'different', 'non_entity')
               ORDER BY id""",
            (case.lexeme_id,),
        ).fetchall():
            if _anchor_member_ids(decision["anchor_members_json"]) != set(
                mention_ids
            ) or _anchor_member_ids(decision["evidence_ids_json"]) != (
                current_evidence_ids
            ):
                continue
            try:
                votes = json.loads(str(decision["votes_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(votes, list):
                continue
            replay_rule = next(
                (
                    str(vote["rule"])
                    for vote in votes
                    if isinstance(vote, Mapping)
                    and isinstance(vote.get("rule"), str)
                    and str(vote["rule"]).strip()
                ),
                None,
            )
            if replay_rule is None:
                continue
            replay_targets: set[str] = set()
            for side in ("left", "right"):
                if decision[f"{side}_anchor_type"] != "concept":
                    continue
                canonical, _ = self._redirect_target(
                    connection, str(decision[f"{side}_anchor_id"])
                )
                if canonical is not None:
                    replay_targets.add(canonical)
            if len(replay_targets) > 1:
                continue
            replay_candidates.add(
                (
                    str(decision["relation"]),
                    replay_rule,
                    next(iter(replay_targets), None),
                )
            )
        if len(replay_candidates) == 1:
            replay_relation, replay_rule, replay_target = next(
                iter(replay_candidates)
            )
            return {
                "relation": replay_relation,
                "rule": replay_rule,
                "target": replay_target,
            }
        if len(replay_candidates) > 1:
            return {"relation": None, "rule": None, "target": None}

        spans = [
            self._candidate_span(
                connection,
                row,
                payload_by_id[int(row["mention_id"])],
            )
            for row in rows
        ]
        if all(span is not None for span in spans) and len(set(spans)) == 1:
            return {
                "relation": "same",
                "rule": "same_span",
                "target": self._preferred_target(
                    protected_concept_ids, canonical_anchor_ids
                ),
            }

        evidence_hashes = []
        for row in rows:
            payload = payload_by_id[int(row["mention_id"])]
            explicit_hash = next(
                (
                    str(payload[key]).strip()
                    for key in ("evidence_hash", "candidate_hash", "payload_hash")
                    if key in payload and str(payload[key]).strip()
                ),
                "",
            )
            evidence_hashes.append(explicit_hash)
        if all(evidence_hashes) and len(set(evidence_hashes)) == 1:
            return {
                "relation": "same",
                "rule": "same_evidence_hash_retry",
                "target": self._preferred_target(
                    protected_concept_ids, canonical_anchor_ids
                ),
            }

        if len(protected_concept_ids) == 1:
            return {
                "relation": "same",
                "rule": "unique_human_locked_concept",
                "target": next(iter(protected_concept_ids)),
            }

        title_kinds: dict[int, set[str]] = defaultdict(set)
        for observation in connection.execute(
            f"""SELECT mention_id, kind FROM concept_type_observations
                WHERE mention_id IN ({placeholders})
                  AND retired_version IS NULL""",
            mention_ids,
        ).fetchall():
            title_kinds[int(observation["mention_id"])].add(
                str(observation["kind"]).strip().casefold()
            )
        fingerprints: list[str] = []
        title_eligible = True
        for row in rows:
            mention_id = int(row["mention_id"])
            eligible = bool(title_kinds[mention_id] & _TITLE_OBSERVATION_KINDS)
            title_eligible = title_eligible and eligible
            payload = payload_by_id[mention_id]
            fingerprint = payload.get("title_fingerprint")
            fingerprints.append(
                fingerprint
                if isinstance(fingerprint, str) and fingerprint.strip()
                else ""
            )
        directory_count = sum(
            str(row["block_type"]).strip().casefold()
            in _TITLE_DIRECTORY_BLOCK_KINDS
            for row in rows
        )
        if (
            title_eligible
            and all(fingerprints)
            and len(set(fingerprints)) == 1
            and len(rows) == 2
            and directory_count == 1
        ):
            return {
                "relation": "same",
                "rule": "exact_title_fingerprint",
                "target": self._preferred_target(
                    protected_concept_ids, canonical_anchor_ids
                ),
            }

        per_mention_anchors: list[set[str]] = []
        for row in rows:
            mention = case_by_id[int(row["mention_id"])]
            anchors = set(mention.concept_anchor_ids)
            if row["concept_id"] is not None:
                anchors.add(str(row["concept_id"]))
            per_mention_anchors.append(anchors)
        common_anchors = (
            set.intersection(*per_mention_anchors)
            if per_mention_anchors and all(per_mention_anchors)
            else set()
        )
        if duplicate_subset and (
            duplicate_has_invalid_target or len(duplicate_targets) > 1
        ):
            return {"relation": None, "rule": None, "target": None}
        if common_anchors or duplicate_subset:
            common_canonical = {
                self._redirect_target(connection, value)[0]
                for value in common_anchors
            } - {None}
            return {
                "relation": "same",
                "rule": "same_anchor_or_duplicate_subset",
                "target": self._preferred_target(
                    protected_concept_ids,
                    duplicate_targets
                    or common_canonical
                    or canonical_anchor_ids,
                ),
            }

        if redirected_any and len(canonical_anchor_ids) == 1:
            return {
                "relation": "same",
                "rule": "redirect",
                "target": next(iter(canonical_anchor_ids)),
            }
        return {"relation": None, "rule": None, "target": None}

    @staticmethod
    def _preferred_target(
        protected_concept_ids: set[str],
        canonical_anchor_ids: set[str],
    ) -> str | None:
        if len(protected_concept_ids) == 1:
            return next(iter(protected_concept_ids))
        if canonical_anchor_ids:
            return min(canonical_anchor_ids)
        return None

    def _deterministic_relation(
        self,
        case: CoreferenceCase,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[str | None, str | None]:
        if connection is None:
            with closing(self.database.connect()) as owned_connection:
                evaluation = self._deterministic_evaluation(
                    case, owned_connection
                )
        else:
            evaluation = self._deterministic_evaluation(case, connection)
        return evaluation["relation"], evaluation["rule"]

    def resolve_deterministic(
        self,
        case: CoreferenceCase,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[str | None, str | None]:
        """Resolve and atomically persist one conservative, model-free case."""

        if connection is not None:
            self.database._require_active_transaction(connection)
            self.database._audit_transaction_for(connection)
        if connection is None:
            with self.database.transaction() as owned_connection:
                return self.resolve_deterministic(
                    case, connection=owned_connection
                )

        with self.database._method_savepoint(
            connection, "deterministic_resolution"
        ):
            return self._resolve_deterministic_in_transaction(
                case, connection
            )

    def _resolve_deterministic_in_transaction(
        self,
        case: CoreferenceCase,
        connection: sqlite3.Connection,
    ) -> tuple[str | None, str | None]:

        evaluation = self._deterministic_evaluation(case, connection)
        relation = evaluation["relation"]
        rule = evaluation["rule"]
        if relation is None or rule is None:
            return (None, None)
        if rule == "locked_decision":
            return (relation, rule)

        mention_ids = tuple(mention.mention_id for mention in case.mentions)
        evidence_ids = tuple(mention.evidence_id for mention in case.mentions)
        target_concept_id = evaluation["target"]
        if relation == "same":
            try:
                with self.database._method_savepoint(
                    connection=connection,
                    prefix="deterministic_same",
                ):
                    if target_concept_id is None:
                        anchor_mention_id = case.mentions[0].mention_id
                        target_concept_id = (
                            self.database.ensure_concept_for_anchor(
                                case.lexeme_id,
                                anchor_mention_id,
                                connection=connection,
                            )
                        )
                    placeholders = ",".join("?" for _ in mention_ids)
                    expected_changes = sum(
                        row["concept_id"] != target_concept_id
                        for row in connection.execute(
                            f"""SELECT concept_id FROM mentions
                                WHERE id IN ({placeholders})""",
                            mention_ids,
                        ).fetchall()
                    )
                    changed = self.database.bind_mentions(
                        target_concept_id,
                        mention_ids,
                        connection=connection,
                    )
                    if changed != expected_changes:
                        raise _ProtectedBindingConflict(
                            "deterministic same cannot bind every member"
                        )
            except _ProtectedBindingConflict:
                return (None, None)

        decision_payload = {
            "decision_source": "deterministic",
            "evidence_ids": sorted(evidence_ids),
            "left_anchor_id": target_concept_id or case.mention_set_id,
            "left_anchor_type": (
                "concept" if target_concept_id is not None else "mention_set"
            ),
            "lexeme_id": case.lexeme_id,
            "mention_ids": sorted(mention_ids),
            "mention_set_id": case.mention_set_id,
            "relation": relation,
            "right_anchor_id": case.mention_set_id,
            "right_anchor_type": "mention_set",
            "rule": rule,
        }
        frozen_payload = _canonical_json_bytes(decision_payload)
        decision_payload_hash = hashlib.sha256(frozen_payload).hexdigest()
        existing = connection.execute(
            """SELECT id, relation, decision_source
               FROM coreference_decisions
               WHERE payload_hash=? AND retired_version IS NULL""",
            (decision_payload_hash,),
        ).fetchone()
        if existing is None:
            decision_id = stable_id(
                "coref", decision_payload_hash, length=24
            )
            collision = connection.execute(
                "SELECT payload_hash FROM coreference_decisions WHERE id=?",
                (decision_id,),
            ).fetchone()
            if collision is not None:
                raise RuntimeError(
                    f"coreference decision id collision: {decision_id}"
                )
            version = int(
                connection.execute(
                    "SELECT MAX(id) FROM knowledge_versions"
                ).fetchone()[0]
            )
            connection.execute(
                """INSERT INTO coreference_decisions(
                       id, lexeme_id, left_anchor_type, left_anchor_id,
                       right_anchor_type, right_anchor_id, relation,
                       decision_source, confidence, locked, votes_json,
                       evidence_ids_json, anchor_members_json, payload_hash,
                       created_version, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, 'deterministic', 1.0, 0,
                          ?, ?, ?, ?, ?, ?)""",
                (
                    decision_id,
                    case.lexeme_id,
                    decision_payload["left_anchor_type"],
                    decision_payload["left_anchor_id"],
                    decision_payload["right_anchor_type"],
                    decision_payload["right_anchor_id"],
                    relation,
                    json.dumps(
                        [{"source": "deterministic", "rule": rule}],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(sorted(evidence_ids)),
                    json.dumps(sorted(mention_ids)),
                    decision_payload_hash,
                    version,
                    utc_now(),
                ),
            )
            self.database.record_audit_call(
                run_id=None,
                block_id=None,
                purpose="deterministic_coreference",
                model="none",
                knowledge_version=version,
                request={
                    "actor_type": "deterministic",
                    "payload_hash": decision_payload_hash,
                },
                raw_response="",
                parsed={
                    "actor_type": "deterministic",
                    "concept_id": target_concept_id,
                    "relation": relation,
                    "rule": rule,
                },
                accepted=True,
                attempts=1,
                elapsed_ms=0,
                error=None,
                connection=connection,
                archive_payload=False,
            )
        elif (
            str(existing["relation"]) != relation
            or str(existing["decision_source"]) != "deterministic"
        ):
            raise RuntimeError(
                "active coreference payload collides with protected decision"
            )
        return (relation, rule)

    @staticmethod
    def payload_bytes(case: CoreferenceCase) -> bytes:
        return payload_bytes(case)

    @staticmethod
    def payload_hash(case: CoreferenceCase) -> str:
        return payload_hash(case)

    @staticmethod
    def cache_key(case: CoreferenceCase, model_names: Sequence[str]) -> str:
        return cache_key(case, model_names)

    @staticmethod
    def model_payloads(
        case: CoreferenceCase, model_names: Sequence[str]
    ) -> tuple[bytes, bytes]:
        _validated_model_names(model_names)
        frozen = payload_bytes(case)
        return (frozen, frozen)

    @staticmethod
    def parse_response(
        raw_response: CoreferenceResponse | Mapping[str, Any] | str | bytes,
        cases: Iterable[CoreferenceCase],
    ) -> CoreferenceResponse:
        frozen_cases = tuple(cases)
        by_id = {case.case_id: case for case in frozen_cases}
        if len(by_id) != len(frozen_cases):
            raise CoreferenceProtocolError("frozen cases contain duplicate case ids")
        try:
            if isinstance(raw_response, (str, bytes)):
                response = CoreferenceResponse.model_validate_json(raw_response)
            else:
                response = CoreferenceResponse.model_validate(
                    _pure_validation_data(raw_response)
                )
        except (ValidationError, ValueError, TypeError) as exc:
            raise CoreferenceProtocolError(
                f"invalid coreference response: {exc}"
            ) from exc

        seen: set[str] = set()
        for vote in response.votes:
            if vote.case_id in seen:
                raise CoreferenceProtocolError(
                    f"duplicate vote for coreference case {vote.case_id}"
                )
            seen.add(vote.case_id)
            case = by_id.get(vote.case_id)
            if case is None:
                raise CoreferenceProtocolError(
                    f"unknown coreference case: {vote.case_id}"
                )
            if len(set(vote.mention_ids)) != len(vote.mention_ids):
                raise CoreferenceProtocolError(
                    f"duplicate mention id in case {vote.case_id}"
                )
            expected = {mention.request_id for mention in case.mentions}
            received = set(vote.mention_ids)
            if received != expected:
                outside = sorted(received - expected)
                missing = sorted(expected - received)
                detail = []
                if outside:
                    detail.append(f"outside case: {outside}")
                if missing:
                    detail.append(f"missing: {missing}")
                raise CoreferenceProtocolError(
                    f"mention ids do not match {vote.case_id} ({'; '.join(detail)})"
                )
        missing_cases = sorted(set(by_id) - seen)
        if missing_cases:
            raise CoreferenceProtocolError(
                f"response omitted coreference cases: {missing_cases}"
            )
        return response

    validate_response = parse_response
