"""Build required, non-spoiling context packets for one translation block."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .database import V4Database
from .matcher import FrozenRenderIndex
from .models import ContextPacket, V4Block


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
        parts = [
            "<translation_constraints>",
            "只使用当前位置可知的信息；不得根据后文推断身份、性别或谜底。",
            "概念含义用于理解世界机制，不得把定义逐字扩写进正文；核心译名可按中文句法添加方位、所属、量词等成分。",
            self._render_concepts(concepts),
            "可用的保守翻译约束：",
            *(f"- {claim['statement']}" for claim in claims),
            "</translation_constraints>",
            "<prior_concept_evidence>",
            "以下是当前概念在当前位置之前的原文用例，只用于恢复已明示的机制和用法，不构成后文解释：",
            self._render_prior_concept_evidence(prior_evidence),
            "</prior_concept_evidence>",
        ]
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
        return ContextPacket(
            block_id=block.id,
            knowledge_version=version,
            rendered=rendered,
            required_chars=required_chars,
            matched_concept_ids=[concept["id"] for concept in concepts],
            matched_claim_ids=[claim["id"] for claim in claims],
        )
