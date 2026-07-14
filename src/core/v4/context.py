"""Build required, non-spoiling context packets for one translation block."""

from __future__ import annotations

import json
from typing import List, Optional

from .database import V4Database
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
            lines = [
                f"- {concept['source']}",
                "  核心译名: "
                + (
                    "（高影响项待核验，不提供候选译法）"
                    if concept.get("verification_pending")
                    else concept["default_target"] or "（未定）"
                ),
                f"  concept_id: {concept['id']}",
            ]
            if concept.get("description"):
                lines.append(f"  概念含义: {concept['description']}")
            for rule in concept.get("rules", []):
                condition = rule.get("condition") or {}
                lines.append(
                    f"  语境规则: {json.dumps(condition, ensure_ascii=False)} → {rule['target']}"
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
    ) -> ContextPacket:
        version = knowledge_version or self.database.current_knowledge_version()
        concepts = self.database.concepts_for_text(block.source_text)
        claims = self.database.claims_for_block(block)
        prior_evidence = self.database.prior_concept_source_evidence(block, concepts)
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
