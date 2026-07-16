"""Build required, non-spoiling context packets for one translation block."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .database import V4Database
from .matcher import FrozenRenderIndex
from .models import ContextPacket, V4Block
from .narrative_models import (
    NarrativeRetrieval,
    NarrativeSnapshot,
    SemanticRelation,
    render_narrative_memory,
)


class ContextOverflow(RuntimeError):
    def __init__(self, block_id: str, required_chars: int, budget_chars: int):
        self.block_id = block_id
        self.required_chars = required_chars
        self.budget_chars = budget_chars
        super().__init__(
            f"{block_id} 必需上下文 {required_chars} 字符超过预算 {budget_chars}；需要人工处理"
        )


class ContextBuilder:
    def __init__(self, database: V4Database, max_context_chars: int = 24000):
        self.database = database
        self.max_context_chars = max_context_chars

    @staticmethod
    def _render_concepts(concepts: List[dict]) -> str:
        if not concepts:
            return "（当前块没有命中的已知概念）"
        rendered = []
        for concept in concepts:
            effective_target = str(concept.get("default_target") or "").strip()
            strength = concept.get("target_strength") or "unset"
            if effective_target:
                strength_label = "已核验" if strength == "verified" else "工作译名"
                target_line = f"{effective_target}（{strength_label}）"
            else:
                target_line = "（未定）"
            lines = [
                f"- {concept['source']}",
                f"  核心译名: {target_line}",
                f"  concept_id: {concept['id']}",
            ]
            if concept.get("description"):
                lines.append(f"  概念含义: {concept['description']}")
            for rule in concept.get("rules", []):
                condition = rule.get("condition") or {}
                target = str(rule.get("target") or "").strip()
                if not target:
                    continue
                lines.append(
                    f"  语境规则: {json.dumps(condition, ensure_ascii=False)} → {target}"
                )
            rendered.append("\n".join(lines))
        return "\n".join(rendered)

    @staticmethod
    def _render_prior_concept_evidence(evidence: List[dict]) -> str:
        if not evidence:
            return "（没有命中的前文概念证据）"
        return "\n\n".join(
            (
                f"- [{item['legacy_id']} {item['paragraph_id']}] "
                f"与 {item['concept_source']} 相关的前文原文：\n"
                f"  {item['source_text']}"
            )
            for item in evidence
        )

    @staticmethod
    def _render_semantic_relations(
        relations: Sequence[SemanticRelation],
    ) -> str:
        if not relations:
            return "（当前块没有额外的跨句语义义务）"
        return "\n".join(
            (
                f"- {relation.relation_type} / "
                f"{relation.inference_strength}: "
                f"{relation.translation_constraint} "
                f"[原文片段: {' | '.join(relation.source_spans)}]"
            )
            for relation in relations
        )

    @staticmethod
    def _render_narrative_memory(retrieval: NarrativeRetrieval) -> str:
        if not retrieval.memories:
            return "（当前位置没有命中的叙事记忆）"
        required = set(retrieval.required_memory_ids)
        return "\n".join(
            render_narrative_memory(
                memory, required=memory.id in required
            )
            for memory in retrieval.memories
        )

    def build(
        self,
        block: V4Block,
        previous_source: str = "",
        previous_translation: str = "",
        local_summary: str = "",
        knowledge_version: Optional[int] = None,
        concept_snapshot: Optional[Sequence[Dict[str, Any]]] = None,
        frozen_claims: Optional[Sequence[Mapping[str, Any]]] = None,
        frozen_prior_concept_evidence: Optional[
            Sequence[Mapping[str, Any]]
        ] = None,
        narrative_snapshot: Optional[NarrativeSnapshot] = None,
        narrative_retrieval: Optional[NarrativeRetrieval] = None,
        semantic_relations: Sequence[SemanticRelation] = (),
        source_structure: Optional[Mapping[str, Any]] = None,
        style_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> ContextPacket:
        version = (
            knowledge_version
            if knowledge_version is not None
            else self.database.current_knowledge_version()
        )
        concepts = self.database.concepts_for_text(
            block.source_text, concept_snapshot=concept_snapshot
        )
        if isinstance(concept_snapshot, FrozenRenderIndex) and (
            frozen_claims is None or frozen_prior_concept_evidence is None
        ):
            raise RuntimeError(
                "frozen claims and prior evidence are required with a frozen render index"
            )
        claims = (
            [dict(claim) for claim in frozen_claims]
            if frozen_claims is not None
            else self.database.claims_for_block(block)
        )
        prior_evidence = (
            [dict(item) for item in frozen_prior_concept_evidence]
            if frozen_prior_concept_evidence is not None
            else self.database.prior_concept_source_evidence(block, concepts)
        )
        if (narrative_snapshot is None) != (narrative_retrieval is None):
            raise ValueError(
                "narrative snapshot and retrieval must be supplied together"
            )
        parts = [
            "<translation_constraints>",
            "只使用当前位置可知的信息；不得根据后文推断身份、性别或谜底。",
            "概念含义用于理解世界机制，不得把定义逐字扩写进正文；核心译名可按中文句法添加方位、所属、量词等成分。",
            self._render_concepts(concepts),
            "可用的保守翻译约束：",
            *(f"- {claim['statement']}" for claim in claims),
            "</translation_constraints>",
            "<source_structure>",
            json.dumps(
                dict(source_structure or {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "</source_structure>",
            "<semantic_relations>",
            self._render_semantic_relations(semantic_relations),
            "</semantic_relations>",
        ]
        if narrative_snapshot is not None and narrative_retrieval is not None:
            parts.extend(
                [
                    "<narrative_memory>",
                    "只使用当前位置已经开放的信息；保守提示不得被扩写成原文没有明说的事实。",
                    self._render_narrative_memory(narrative_retrieval),
                    "</narrative_memory>",
                    "<discourse_state>",
                    json.dumps(
                        narrative_snapshot.discourse_state.to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "</discourse_state>",
                ]
            )
        if style_snapshot:
            parts.extend(
                [
                    "<style_state>",
                    json.dumps(
                        dict(style_snapshot.get("state") or {}),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "</style_state>",
                ]
            )
        parts.extend(
            [
            "<prior_concept_evidence>",
            "以下是当前概念在当前位置之前的原文用例，只用于恢复已明示的机制和用法，不构成后文解释：",
            self._render_prior_concept_evidence(prior_evidence),
            "</prior_concept_evidence>",
            ]
        )
        if local_summary:
            parts.extend(["<island_summary>", local_summary, "</island_summary>"])
        if previous_source or previous_translation:
            parts.extend(
                [
                    "<island_previous>",
                    f"<source_tail>{previous_source[-800:]}</source_tail>",
                    f"<translation_tail>{previous_translation[-1200:]}</translation_tail>",
                    "</island_previous>",
                ]
            )
        rendered = "\n".join(parts)
        required_chars = len(block.source_text) + len(rendered)
        if required_chars > self.max_context_chars:
            raise ContextOverflow(block.id, required_chars, self.max_context_chars)
        matched_rule_ids = sorted(
            {
                str(rule.get("id") or "")
                for concept in concepts
                for rule in concept.get("rules", [])
                if str(rule.get("id") or "")
            }
        )
        matched_lexeme_ids = sorted(
            {
                str(concept.get("primary_lexeme_id") or "")
                for concept in concepts
                if str(concept.get("primary_lexeme_id") or "")
            }
        )
        discourse_state_hash = (
            hashlib.sha256(
                json.dumps(
                    narrative_snapshot.discourse_state.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if narrative_snapshot is not None
            else ""
        )
        context_hash = hashlib.sha256(
            json.dumps(
                {
                    "block_id": block.id,
                    "source_hash": block.source_hash,
                    "knowledge_version": version,
                    "memory_version": (
                        narrative_snapshot.memory_version
                        if narrative_snapshot is not None
                        else 1
                    ),
                    "rendered": rendered,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return ContextPacket(
            block_id=block.id,
            knowledge_version=version,
            rendered=rendered,
            required_chars=required_chars,
            memory_version=(
                narrative_snapshot.memory_version
                if narrative_snapshot is not None
                else 1
            ),
            snapshot_id=(
                narrative_snapshot.id if narrative_snapshot is not None else ""
            ),
            matched_lexeme_ids=matched_lexeme_ids,
            matched_concept_ids=[concept["id"] for concept in concepts],
            matched_rule_ids=matched_rule_ids,
            matched_claim_ids=[claim["id"] for claim in claims],
            matched_memory_ids=(
                [memory.id for memory in narrative_retrieval.memories]
                if narrative_retrieval is not None
                else []
            ),
            style_snapshot_id=str(
                (style_snapshot or {}).get("id") or ""
            ),
            discourse_state_hash=discourse_state_hash,
            context_hash=context_hash,
        )
