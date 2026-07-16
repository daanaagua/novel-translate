"""Strict fused semantic and narrative pre-mapping protocol."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Collection, Mapping, Sequence

from .models import V4Block
from .narrative_models import (
    DiscourseState,
    NarrativeMemoryCandidate,
    NarrativePremapResult,
    NarrativeSubject,
    SemanticRelation,
)


MAX_RELATIONS_PER_BLOCK = 32
MAX_MEMORY_CANDIDATES_PER_BLOCK = 32


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _span_is_grounded(span: str, source_text: str) -> bool:
    return bool(span.strip()) and _normalized(span) in _normalized(source_text)


def _strip_code_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.I)
        text = re.sub(r"\s*```$", "", text, count=1)
    return text.strip()


def _validate_relations(
    raw: Any,
    source_text: str,
    allowed_memory_ids: set[str],
) -> tuple[list[SemanticRelation], list[str]]:
    if not isinstance(raw, list):
        return [], ["semantic_relations: expected an array"]
    relations: list[SemanticRelation] = []
    warnings: list[str] = []
    for index, item in enumerate(raw[:MAX_RELATIONS_PER_BLOCK]):
        try:
            if not isinstance(item, Mapping):
                raise ValueError("entry must be an object")
            spans = tuple(str(value).strip() for value in item.get("source_spans") or ())
            if not all(_span_is_grounded(span, source_text) for span in spans):
                raise ValueError("source_spans are not grounded in current source")
            related = tuple(
                str(value).strip()
                for value in item.get("related_memory_ids") or ()
            )
            if any(value not in allowed_memory_ids for value in related):
                raise ValueError("related_memory_ids include an unavailable id")
            relations.append(
                SemanticRelation(
                    relation_type=str(item.get("relation_type") or ""),
                    inference_strength=str(
                        item.get("inference_strength") or ""
                    ),
                    source_spans=spans,
                    related_memory_ids=related,
                    translation_constraint=str(
                        item.get("translation_constraint") or ""
                    ),
                )
            )
        except (TypeError, ValueError) as exc:
            warnings.append(f"semantic_relations[{index}]: {exc}")
    if len(raw) > MAX_RELATIONS_PER_BLOCK:
        warnings.append(
            f"semantic_relations: truncated after {MAX_RELATIONS_PER_BLOCK} entries"
        )
    return relations, warnings


def _validate_subjects(
    raw: Any, allowed_subject_ids: set[str]
) -> tuple[NarrativeSubject, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("subjects must be an array")
    subjects = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("subjects entries must be objects")
        subject_id = str(item.get("subject_id") or "").strip()
        if subject_id not in allowed_subject_ids:
            raise ValueError("subjects include an unavailable id")
        subjects.append(
            NarrativeSubject(
                subject_type=str(item.get("subject_type") or ""),
                subject_id=subject_id,
                role=str(item.get("role") or "subject"),
            )
        )
    return tuple(subjects)


def _validate_memory_candidates(
    raw: Any,
    source_text: str,
    allowed_subject_ids: set[str],
    allowed_memory_ids: set[str],
) -> tuple[list[NarrativeMemoryCandidate], list[str]]:
    if not isinstance(raw, list):
        return [], ["memory_candidates: expected an array"]
    memories: list[NarrativeMemoryCandidate] = []
    warnings: list[str] = []
    for index, item in enumerate(raw[:MAX_MEMORY_CANDIDATES_PER_BLOCK]):
        try:
            if not isinstance(item, Mapping):
                raise ValueError("entry must be an object")
            evidence = tuple(
                str(value).strip()
                for value in item.get("evidence_spans") or ()
            )
            if not evidence or not all(
                _span_is_grounded(span, source_text) for span in evidence
            ):
                raise ValueError(
                    "evidence_spans are not grounded in current source"
                )
            related = tuple(
                str(value).strip()
                for value in item.get("related_memory_ids") or ()
            )
            if any(value not in allowed_memory_ids for value in related):
                raise ValueError("related_memory_ids include an unavailable id")
            memories.append(
                NarrativeMemoryCandidate(
                    candidate_id=str(item.get("candidate_id") or ""),
                    memory_type=str(item.get("memory_type") or ""),
                    statement=str(item.get("statement") or ""),
                    truth_status=str(item.get("truth_status") or ""),
                    visibility=str(item.get("visibility") or ""),
                    confidence=float(item.get("confidence")),
                    evidence_spans=evidence,
                    subjects=_validate_subjects(
                        item.get("subjects"), allowed_subject_ids
                    ),
                    related_memory_ids=related,
                    state_operation=str(
                        item.get("state_operation") or "append"
                    ),
                    high_impact=bool(item.get("high_impact")),
                )
            )
        except (TypeError, ValueError) as exc:
            warnings.append(f"memory_candidates[{index}]: {exc}")
    if len(raw) > MAX_MEMORY_CANDIDATES_PER_BLOCK:
        warnings.append(
            "memory_candidates: truncated after "
            f"{MAX_MEMORY_CANDIDATES_PER_BLOCK} entries"
        )
    return memories, warnings


def _validate_discourse_delta(
    raw: Any, allowed_subject_ids: set[str]
) -> tuple[DiscourseState, list[str]]:
    if not isinstance(raw, Mapping):
        return DiscourseState(premap_uncertain=True), [
            "discourse_delta: expected an object"
        ]
    warnings: list[str] = []
    try:
        for field_name in (
            "viewpoint_holder",
            "scene_location",
        ):
            value = str(raw.get(field_name) or "")
            if value and value not in allowed_subject_ids:
                raise ValueError(f"{field_name} includes an unavailable id")
        for field_name in (
            "active_speakers",
            "addressed_parties",
        ):
            values = tuple(str(value) for value in raw.get(field_name) or ())
            if any(value not in allowed_subject_ids for value in values):
                raise ValueError(f"{field_name} includes an unavailable id")
        return DiscourseState.from_mapping(raw), warnings
    except (TypeError, ValueError) as exc:
        warnings.append(f"discourse_delta: {exc}")
        return DiscourseState(premap_uncertain=True), warnings


def validate_premap_payload(
    payload: Any,
    source_text: str,
    allowed_subject_ids: Collection[str] = (),
    allowed_memory_ids: Collection[str] = (),
) -> NarrativePremapResult:
    """Validate response sections independently and retain every grounded section."""

    if not isinstance(payload, Mapping):
        raise ValueError("premap response must be a JSON object")
    subjects = {str(value) for value in allowed_subject_ids}
    memories = {str(value) for value in allowed_memory_ids}
    relations, relation_warnings = _validate_relations(
        payload.get("semantic_relations"), source_text, memories
    )
    candidates, memory_warnings = _validate_memory_candidates(
        payload.get("memory_candidates"), source_text, subjects, memories
    )
    discourse, discourse_warnings = _validate_discourse_delta(
        payload.get("discourse_delta"), subjects
    )
    warnings = relation_warnings + memory_warnings + discourse_warnings
    return NarrativePremapResult(
        semantic_relations=tuple(relations),
        memory_candidates=tuple(candidates),
        discourse_delta=discourse,
        validation_warnings=tuple(warnings),
        degraded=bool(warnings),
    )


@dataclass(frozen=True)
class PremapperConfig:
    temperature: float = 0.0
    max_tokens: int = 6_144
    max_attempts: int = 2

    def __post_init__(self) -> None:
        if self.max_tokens < 256:
            raise ValueError("max_tokens must be at least 256")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")


class NarrativePremapper:
    SYSTEM_PROMPT = """你是文学翻译系统的融合预映射器。你不翻译原文，也不猜测后文。
仅依据当前英文原文、当前位置可见的前文记忆和话语状态，返回严格 JSON：
{
  "semantic_relations": [],
  "memory_candidates": [],
  "discourse_delta": {}
}
所有证据引文必须逐字取自 current_source。所有 subject_id 和 memory_id 必须来自请求。
原文的歧义、矛盾和未决身份必须保留，不得改写为唯一答案。"""

    def __init__(
        self, llm: Any, config: PremapperConfig | None = None
    ) -> None:
        self.llm = llm
        self.config = config or PremapperConfig()
        self.last_succeeded = False

    @staticmethod
    def _visible_memory_ids(snapshot: Mapping[str, Any]) -> set[str]:
        rows = snapshot.get("visible_memories")
        if rows is None:
            rows = snapshot.get("memories")
        ids: set[str] = set()
        for row in rows or ():
            if isinstance(row, Mapping):
                value = str(row.get("id") or "").strip()
            else:
                value = str(row).strip()
            if value:
                ids.add(value)
        return ids

    def map(
        self,
        *,
        block: V4Block,
        structure: Mapping[str, Any],
        prior_snapshot: Mapping[str, Any],
        discourse_state: DiscourseState,
        provisional_subjects: Sequence[Mapping[str, Any]],
    ) -> NarrativePremapResult:
        allowed_subject_ids = {
            str(value.get("id") or "").strip()
            for value in provisional_subjects
            if isinstance(value, Mapping) and str(value.get("id") or "").strip()
        }
        allowed_memory_ids = self._visible_memory_ids(prior_snapshot)
        request = {
            "structure": dict(structure),
            "prior_snapshot": dict(prior_snapshot),
            "discourse_state": discourse_state.to_dict(),
            "provisional_subjects": [
                dict(value)
                for value in provisional_subjects
                if isinstance(value, Mapping)
            ],
            "current_source": block.source_text,
        }
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    request,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ]
        last_error = ""
        for attempt in range(self.config.max_attempts):
            attempt_messages = list(messages)
            if attempt and last_error:
                attempt_messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上一响应无效："
                            f"{last_error}。仅重新输出符合协议的 JSON。"
                        ),
                    }
                )
            try:
                raw = self.llm.chat(
                    messages=attempt_messages,
                    purpose="narrative_premap",
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    json_mode=True,
                    stream=False,
                )
                payload = json.loads(_strip_code_fence(str(raw)))
                result = validate_premap_payload(
                    payload,
                    block.source_text,
                    allowed_subject_ids=allowed_subject_ids,
                    allowed_memory_ids=allowed_memory_ids,
                )
                self.last_succeeded = True
                return result
            except Exception as exc:
                last_error = str(exc)
        self.last_succeeded = False
        degraded_state = replace(discourse_state, premap_uncertain=True)
        return NarrativePremapResult(
            semantic_relations=(),
            memory_candidates=(),
            discourse_delta=degraded_state,
            validation_warnings=(
                f"premap_response: {last_error or 'unknown protocol failure'}",
            ),
            degraded=True,
        )
