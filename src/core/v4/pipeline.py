"""Barriered parallel translation coordinator for the parallel_v4 shadow pipeline."""

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from uuid import uuid4

from ...agents.glossary_manager import GlossaryManager
from ..schemas import (
    ChunkStatus,
    Glossary,
    GlossaryItem,
    GlossaryRule,
    TermCategory,
    TermStatus,
    TextChunk,
)
from ..translator import TranslationConfig, TranslationEngine
from .context import ContextBuilder, ContextOverflow
from .database import FrozenRenderBundle, KnowledgeSnapshotError, V4Database
from .matcher import FrozenRenderIndex
from .models import (
    ClaimDependencySnapshot,
    Island,
    RenderingMatchSnapshot,
    TranslationOutcome,
    V4Block,
    V4BlockStatus,
)
from .semantic_mapper import SemanticMapper, SemanticMapperConfig


@dataclass
class V4PipelineConfig:
    island_size: int = 3
    initial_workers: int = 2
    max_workers: int = 4
    max_context_chars: int = 24000
    max_attempts: int = 2
    max_blocks: Optional[int] = None
    include_block_ids: tuple[str, ...] = ()
    decision_mode: str = "unattended"
    enable_polish: bool = True
    enable_semantic_mapper: bool = False
    semantic_temperature: float = 0.0
    semantic_max_tokens: int = 4096
    semantic_max_attempts: int = 2
    draft_temperature: float = 0.1
    draft_max_tokens: int = 6144
    polish_temperature: float = 0.2
    polish_max_tokens: int = 6144
    style_reference: Optional[str] = None
    use_baseline_reference: bool = False
    audit_mode: str = "full"
    force: bool = False

    def __post_init__(self) -> None:
        if self.decision_mode not in {"interactive", "unattended"}:
            raise ValueError("decision_mode 必须是 interactive 或 unattended")
        if self.audit_mode not in {"full", "response", "minimal"}:
            raise ValueError("audit_mode 必须是 full、response 或 minimal")
        self.island_size = max(1, self.island_size)
        self.initial_workers = max(1, self.initial_workers)
        self.max_workers = max(self.initial_workers, self.max_workers)
        self.max_attempts = max(1, self.max_attempts)
        self.semantic_max_attempts = max(1, self.semantic_max_attempts)
        self.include_block_ids = tuple(dict.fromkeys(self.include_block_ids))


class AuditedLLM:
    """Capture model traffic in memory; workers never write the database."""

    def __init__(self, delegate: Any):
        self.delegate = delegate
        self.calls: List[Dict[str, Any]] = []

    def get_model(self, purpose: str) -> str:
        getter = getattr(self.delegate, "get_model", None)
        return getter(purpose) if getter else "unknown"

    def chat(self, *, messages: List[Dict[str, str]], purpose: str = "draft", **kwargs: Any):
        started = time.perf_counter()
        request = {
            "messages": messages,
            "temperature": kwargs.get("temperature"),
            "max_tokens": kwargs.get("max_tokens"),
            "json_mode": bool(kwargs.get("json_mode", False)),
            "stream": bool(kwargs.get("stream", False)),
        }
        try:
            response = self.delegate.chat(messages=messages, purpose=purpose, **kwargs)
        except Exception as exc:
            self.calls.append(
                {
                    "purpose": purpose,
                    "model": self.get_model(purpose),
                    "request": request,
                    "raw_response": "",
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "error": str(exc),
                }
            )
            raise
        if not kwargs.get("stream", False):
            self.calls.append(
                {
                    "purpose": purpose,
                    "model": self.get_model(purpose),
                    "request": request,
                    "raw_response": str(response),
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                }
            )
            return response

        def captured_stream():
            parts: List[Dict[str, str]] = []
            stream_error: Optional[str] = None
            try:
                for item in response:
                    if isinstance(item, tuple):
                        kind, content = item
                    else:
                        kind, content = "content", str(item)
                    parts.append({"type": str(kind), "content": str(content)})
                    yield item
            except Exception as exc:
                stream_error = str(exc)
                raise
            finally:
                call = {
                    "purpose": purpose,
                    "model": self.get_model(purpose),
                    "request": request,
                    "raw_response": json.dumps(parts, ensure_ascii=False),
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                }
                if stream_error:
                    call["error"] = stream_error
                self.calls.append(call)

        return captured_stream()


class V4TranslationPipeline:
    def __init__(
        self,
        database: V4Database,
        llm_factory: Callable[[], Any],
        prompts: Optional[Dict[str, Any]] = None,
        config: Optional[V4PipelineConfig] = None,
    ):
        self.database = database
        self.llm_factory = llm_factory
        self.prompts = prompts or {}
        self.config = config or V4PipelineConfig()
        self.context_builder = ContextBuilder(database, self.config.max_context_chars)

    @staticmethod
    def _category(value: str) -> Optional[TermCategory]:
        try:
            return TermCategory(value)
        except ValueError:
            return TermCategory.CONCEPT

    def _glossary_for(
        self,
        blocks: Sequence[V4Block],
        concept_snapshot: Optional[Sequence[Dict[str, Any]]] = None,
        rendering_contexts_by_block: Optional[
            Mapping[str, Sequence[Mapping[str, Any]]]
        ] = None,
    ) -> GlossaryManager:
        if isinstance(concept_snapshot, FrozenRenderIndex):
            if rendering_contexts_by_block is None:
                active_bundle = getattr(self, "_active_render_bundle", None)
                if (
                    isinstance(active_bundle, FrozenRenderBundle)
                    and active_bundle.index.signature == concept_snapshot.signature
                ):
                    rendering_contexts_by_block = active_bundle.contexts_by_block
                else:
                    rendering_contexts_by_block = self.database.freeze_render_bundle(
                        [block.id for block in blocks]
                    ).contexts_by_block
            items: List[GlossaryItem] = []
            item_priorities: Dict[str, int] = {}
            pattern_sources: Dict[str, str] = {}
            for block in blocks:
                occurrence_contexts = list(
                    rendering_contexts_by_block.get(block.id, [])
                )
                matched_items = concept_snapshot.matched_renderings(
                    block.source_text,
                    block_id=block.id,
                    occurrence_contexts=occurrence_contexts,
                )
                grouped: Dict[tuple[str, str], List[Any]] = {}
                for matched in matched_items:
                    if str(matched.rendered_target or "").strip():
                        key = (matched.lexeme_id, matched.matched_form.casefold())
                        grouped.setdefault(key, []).append(matched)
                for group in grouped.values():
                    by_target: Dict[str, List[Any]] = {}
                    for matched in group:
                        target = str(matched.rendered_target or "").strip()
                        by_target.setdefault(target, []).append(matched)
                    multiple_targets = len(by_target) > 1
                    for target, target_matches in by_target.items():
                        matched = target_matches[0]
                        offsets = sorted(
                            {
                                (value.start_offset, value.end_offset)
                                for value in target_matches
                            }
                        )
                        shown_offsets = offsets[:8]
                        offset_label = "、".join(
                            f"{start}:{end}" for start, end in shown_offsets
                        )
                        if len(offsets) > len(shown_offsets):
                            offset_label += "、等"
                        matched_form = str(matched.matched_form or "").strip()
                        source = matched_form
                        location_note = ""
                        if multiple_targets:
                            source = f"{matched_form}（原文字符{offset_label}）"
                            location_note = (
                                f"仅适用于原文字符位置 {offset_label}；"
                                "不得用于同块其他同形词。"
                            )
                            pattern_sources[source] = matched_form
                        lexeme = concept_snapshot.get_lexeme(matched.lexeme_id) or {}
                        concept = next(
                            (
                                value
                                for value in lexeme.get("concepts", []) or []
                                if str(value.get("id") or "") == matched.concept_id
                            ),
                            None,
                        )
                        if concept is None:
                            concept = next(iter(lexeme.get("concepts", []) or []), {})
                        winning_rules = {
                            str(rule.get("id") or ""): rule
                            for rule in list(lexeme.get("rules", []) or [])
                            + list(concept.get("rules", []) or [])
                        }
                        rule_verified = any(
                            bool(winning_rules.get(rule_id, {}).get("locked"))
                            or str(winning_rules.get(rule_id, {}).get("status") or "")
                            == "verified"
                            for rule_id in matched.applied_rule_ids
                        )
                        winning_priority = max(
                            (
                                int(
                                    winning_rules.get(rule_id, {}).get("priority")
                                    or 0
                                )
                                for rule_id in matched.applied_rule_ids
                            ),
                            default=0,
                        )
                        verified = rule_verified or target in {
                            str(lexeme.get("verified_target") or "").strip(),
                            str(concept.get("verified_target") or "").strip(),
                        }
                        status = (
                            TermStatus.VERIFIED if verified else TermStatus.WORKING
                        )
                        base_description = str(
                            concept.get("description") or ""
                        ).strip()
                        description = " ".join(
                            value
                            for value in (base_description, location_note)
                            if value
                        ) or None
                        item_id = (
                            f"{matched.concept_id or matched.lexeme_id}:"
                            f"{block.id}:{offset_label}"
                        )
                        item = GlossaryItem(
                                id=item_id,
                                src=source or str(lexeme.get("source") or ""),
                                default_target=target,
                                category=self._category(
                                    str(concept.get("kind") or "concept")
                                ),
                                status=status,
                                description=description,
                                rules=[],
                        )
                        items.append(item)
                        item_priorities[item_id] = winning_priority
            ranked_items = sorted(
                enumerate(items),
                key=lambda value: (
                    -item_priorities.get(str(value[1].id), 0),
                    value[0],
                    str(value[1].id),
                ),
            )
            item_limit = 128
            character_limit = max(0, min(16 * 1024, self.config.max_context_chars))
            selected_items: List[GlossaryItem] = []
            omitted_items: List[GlossaryItem] = []
            visible_characters = 0
            for _ordinal, item in ranked_items:
                rendered = TranslationEngine._render_glossary_term(item)
                added = len(rendered) + (1 if selected_items else 0)
                if (
                    len(selected_items) >= item_limit
                    or visible_characters + added > character_limit
                ):
                    omitted_items.append(item)
                    continue
                selected_items.append(item)
                visible_characters += added
            omitted_digest = hashlib.sha256(
                "\n".join(str(item.id) for item in omitted_items).encode("utf-8")
            ).hexdigest()
            manager = GlossaryManager(str(self.database.root / "readonly_glossary"))
            manager.glossary = Glossary(items=selected_items)
            manager._build_patterns()
            selected_sources = {str(item.src) for item in selected_items}
            for annotated_source, matched_form in pattern_sources.items():
                if annotated_source not in selected_sources:
                    continue
                manager._term_patterns[annotated_source] = re.compile(
                    rf"(?<!\w){re.escape(matched_form)}(?!\w)", re.IGNORECASE
                )
            manager.render_limit_metadata = {
                "total": len(items),
                "included": len(selected_items),
                "omitted": len(omitted_items),
                "visible_characters": visible_characters,
                "character_limit": character_limit,
                "item_limit": item_limit,
                "omitted_digest": omitted_digest,
                "reason": "bounded_translation_glossary",
            }
            manager.render_warnings = (
                [
                    {
                        "kind": "render_constraints_truncated",
                        "total": len(items),
                        "kept": len(selected_items),
                        "omitted": len(omitted_items),
                        "digest": omitted_digest,
                        "reason": "bounded_translation_glossary",
                    }
                ]
                if omitted_items
                else []
            )
            return manager

        source = "\n".join(block.source_text for block in blocks)
        concepts = self.database.concepts_for_text(
            source, concept_snapshot=concept_snapshot
        )
        items: List[GlossaryItem] = []
        for concept in concepts:
            src = str(concept.get("source") or "").strip()
            target = str(concept.get("default_target") or "").strip()
            if not src or not target:
                continue
            rules = [
                GlossaryRule(
                    condition=json.dumps(rule.get("condition") or {}, ensure_ascii=False),
                    target=rule.get("target") or "",
                )
                for rule in concept.get("rules", [])
                if rule.get("target")
            ]
            verified = concept.get("target_strength") == "verified" or bool(
                concept.get("locked")
            )
            status = (
                TermStatus.VERIFIED
                if verified
                else TermStatus.WORKING
                if concept.get("target_strength") == "working"
                else TermStatus.PENDING
            )
            items.append(
                GlossaryItem(
                    id=concept["id"],
                    src=src,
                    default_target=target,
                    category=self._category(concept.get("kind") or "concept"),
                    status=status,
                    description=concept.get("description") or None,
                    rules=rules,
                )
            )
        manager = GlossaryManager(str(self.database.root / "readonly_glossary"))
        manager.glossary = Glossary(items=items)
        manager._build_patterns()
        return manager

    @staticmethod
    def _make_islands(blocks: Sequence[V4Block], island_size: int) -> List[Island]:
        islands: List[Island] = []
        current: List[V4Block] = []
        for block in blocks:
            is_contiguous = (
                current
                and current[-1].chapter_id == block.chapter_id
                and current[-1].global_index + 1 == block.global_index
            )
            if current and (not is_contiguous or len(current) >= island_size):
                islands.append(Island(id=f"island_{current[0].global_index:06d}", blocks=current))
                current = []
            current.append(block)
        if current:
            islands.append(Island(id=f"island_{current[0].global_index:06d}", blocks=current))
        return islands

    def _translation_config(self) -> TranslationConfig:
        return TranslationConfig(
            draft_temperature=self.config.draft_temperature,
            draft_max_tokens=self.config.draft_max_tokens,
            polish_temperature=self.config.polish_temperature,
            polish_max_tokens=self.config.polish_max_tokens,
            enable_polish=self.config.enable_polish,
            style_reference=self.config.style_reference,
            glossary_mode="manual",
            strict_response_parsing=True,
            persist_discoveries=False,
        )

    def _translate_island(
        self,
        island: Island,
        knowledge_version: int,
        concept_snapshot: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> List[TranslationOutcome]:
        audited_llm = AuditedLLM(self.llm_factory())
        semantic_mapper = SemanticMapper(
            audited_llm,
            SemanticMapperConfig(
                temperature=self.config.semantic_temperature,
                max_tokens=self.config.semantic_max_tokens,
                max_attempts=self.config.semantic_max_attempts,
            ),
        )
        proposals: List[tuple[str, Dict[str, Any]]] = []
        rendering_contexts_by_block: Mapping[
            str, Sequence[Mapping[str, Any]]
        ] = {}
        frozen_claims_by_block: Mapping[
            str, Sequence[Mapping[str, Any]]
        ] = {}
        frozen_prior_evidence_by_block: Mapping[
            str, Sequence[Mapping[str, Any]]
        ] = {}
        if isinstance(concept_snapshot, FrozenRenderIndex):
            active_bundle = getattr(self, "_active_render_bundle", None)
            if (
                isinstance(active_bundle, FrozenRenderBundle)
                and active_bundle.index is concept_snapshot
                and active_bundle.knowledge_version == knowledge_version
                and all(
                    block.id in active_bundle.block_ids for block in island.blocks
                )
            ):
                rendering_contexts_by_block = active_bundle.contexts_by_block
                frozen_claims_by_block = active_bundle.claims_by_block
                frozen_prior_evidence_by_block = (
                    active_bundle.prior_concept_evidence_by_block
                )
            else:
                raise RuntimeError(
                    "translation requires the exact frozen render bundle and contexts"
                )
        engine = TranslationEngine(
            llm_manager=audited_llm,
            glossary_manager=self._glossary_for(
                island.blocks,
                concept_snapshot=concept_snapshot,
                rendering_contexts_by_block=rendering_contexts_by_block,
            ),
            knowledge_base=None,
            prompts=self.prompts,
            config=self._translation_config(),
            proposal_sink=lambda kind, payload: proposals.append((kind, payload)),
        )
        outcomes: List[TranslationOutcome] = []
        previous_source = ""
        previous_translation = ""
        local_summary = ""
        for block in island.blocks:
            render_constraint_warnings: List[Dict[str, Any]] = []
            frozen_render_matches: tuple[RenderingMatchSnapshot, ...] = ()
            if isinstance(concept_snapshot, FrozenRenderIndex):
                engine.glossary = self._glossary_for(
                    [block],
                    concept_snapshot=concept_snapshot,
                    rendering_contexts_by_block=rendering_contexts_by_block,
                )
                render_constraint_warnings = [
                    dict(value)
                    for value in getattr(engine.glossary, "render_warnings", [])
                    if isinstance(value, Mapping)
                ]
                frozen_render_matches = tuple(
                    RenderingMatchSnapshot(
                        lexeme_id=match.lexeme_id,
                        concept_id=match.concept_id,
                        matched_form=match.matched_form,
                        start_offset=match.start_offset,
                        end_offset=match.end_offset,
                        rendered_target=match.rendered_target,
                        applied_rule_ids=tuple(match.applied_rule_ids),
                        dependency_fingerprint=match.dependency_fingerprint,
                    )
                    for match in concept_snapshot.matched_renderings(
                        block.source_text,
                        block_id=block.id,
                        occurrence_contexts=list(
                            rendering_contexts_by_block.get(block.id, [])
                        ),
                    )
                )
            frozen_block_concept_ids = [
                str(concept["id"])
                for concept in self.database.concepts_for_text(
                    block.source_text, concept_snapshot=concept_snapshot
                )
            ]
            try:
                packet = self.context_builder.build(
                    block,
                    previous_source=previous_source,
                    previous_translation=previous_translation,
                    local_summary=local_summary,
                    knowledge_version=knowledge_version,
                    concept_snapshot=concept_snapshot,
                    frozen_claims=frozen_claims_by_block.get(block.id, ()),
                    frozen_prior_concept_evidence=(
                        frozen_prior_evidence_by_block.get(block.id, ())
                    ),
                )
            except ContextOverflow as exc:
                outcomes.append(
                    TranslationOutcome(
                        block=block,
                        knowledge_version=knowledge_version,
                        status=V4BlockStatus.INCOMPLETE_REQUIRES_HUMAN.value,
                        matched_concept_ids=frozen_block_concept_ids,
                        matched_renderings=frozen_render_matches,
                        error=str(exc),
                    )
                )
                break

            started = time.perf_counter()
            call_start = len(audited_llm.calls)
            proposal_start = len(proposals)
            comparison_reference = ""
            if self.config.use_baseline_reference:
                reference = self.database.comparison_reference_for_block(block.id)
                if reference:
                    comparison_reference = str(reference.get("text") or "").strip()
                if (
                    comparison_reference
                    and packet.required_chars + len(comparison_reference)
                    > self.config.max_context_chars
                ):
                    outcomes.append(
                        TranslationOutcome(
                            block=block,
                            knowledge_version=knowledge_version,
                            status=V4BlockStatus.INCOMPLETE_REQUIRES_HUMAN.value,
                            matched_concept_ids=list(packet.matched_concept_ids),
                            matched_renderings=frozen_render_matches,
                            error=(
                                f"{block.id} 加入旧译文对照后必需上下文 "
                                f"{packet.required_chars + len(comparison_reference)} 字符超过预算 "
                                f"{self.config.max_context_chars}；需要人工处理"
                            ),
                        )
                    )
                    break
            result: Optional[TextChunk] = None
            attempts = 0
            semantic_obligations_hint = ""
            if self.config.enable_semantic_mapper:
                semantic_obligations_hint = semantic_mapper.map(
                    block.source_text,
                    packet.rendered,
                )
                required_with_hint = packet.required_chars + len(semantic_obligations_hint)
                if required_with_hint > self.config.max_context_chars:
                    block_audits = [dict(call) for call in audited_llm.calls[call_start:]]
                    for call in block_audits:
                        call["accepted"] = False
                    semantic_audits = [
                        call for call in block_audits if call.get("purpose") == "semantic"
                    ]
                    if semantic_audits:
                        semantic_audits[-1]["accepted"] = semantic_mapper.last_succeeded
                        semantic_audits[-1]["parsed"] = {
                            "semantic_obligations_hint": semantic_obligations_hint
                        }
                    outcomes.append(
                        TranslationOutcome(
                            block=block,
                            knowledge_version=knowledge_version,
                            status=V4BlockStatus.INCOMPLETE_REQUIRES_HUMAN.value,
                            matched_concept_ids=list(packet.matched_concept_ids),
                            matched_renderings=frozen_render_matches,
                            audit_calls=block_audits,
                            elapsed_ms=int((time.perf_counter() - started) * 1000),
                            error=(
                                f"{block.id} 加入独立语义映射后必需上下文为 "
                                f"{required_with_hint} 字符，超过预算 "
                                f"{self.config.max_context_chars}；需要人工处理"
                            ),
                        )
                    )
                    break
            for attempts in range(1, self.config.max_attempts + 1):
                result = engine.translate_chunk(
                    TextChunk(
                        id=block.id,
                        chapter_id=block.chapter_id,
                        index=block.block_index,
                        source_text=block.source_text,
                        token_count=block.token_count,
                    ),
                    memory_context=packet.rendered,
                    previous_summary=local_summary,
                    previous_chunk_text=previous_source,
                    comparison_reference=comparison_reference,
                    semantic_obligations_hint=semantic_obligations_hint,
                )
                if result.status in {ChunkStatus.COMPLETED, ChunkStatus.HUMAN_REVIEW}:
                    break
            assert result is not None
            block_proposals = proposals[proposal_start:]
            term_proposals = [payload for kind, payload in block_proposals if kind == "term"]
            relation_proposals = [payload for kind, payload in block_proposals if kind == "relation"]
            if result.status == ChunkStatus.COMPLETED:
                status = V4BlockStatus.COMPLETED.value
            elif result.status == ChunkStatus.HUMAN_REVIEW:
                status = V4BlockStatus.COMPLETED_WITH_WARNINGS.value
            else:
                status = V4BlockStatus.FAILED_RETRYABLE.value
            block_audits = [dict(call) for call in audited_llm.calls[call_start:]]
            for call in block_audits:
                call["accepted"] = False
            draft_audits = [call for call in block_audits if call.get("purpose") == "draft"]
            polish_audits = [call for call in block_audits if call.get("purpose") == "polish"]
            semantic_audits = [
                call for call in block_audits if call.get("purpose") == "semantic"
            ]
            if semantic_audits:
                semantic_audits[-1]["accepted"] = semantic_mapper.last_succeeded
                semantic_audits[-1]["parsed"] = {
                    "semantic_obligations_hint": semantic_obligations_hint
                }
            if draft_audits:
                draft_audits[-1]["accepted"] = result.status in {
                    ChunkStatus.COMPLETED,
                    ChunkStatus.HUMAN_REVIEW,
                }
                draft_audits[-1]["parsed"] = {
                    "analysis": result.analysis or "",
                    "semantic_obligations": result.semantic_obligations or "",
                    "translation": result.draft_translation or "",
                    "memory_summary": result.memory_summary or "",
                }
            if polish_audits:
                normalized_polish = engine._normalize_chinese_punctuation(
                    result.polished_translation or ""
                )
                polish_audits[-1]["accepted"] = bool(
                    result.polished_translation
                    and result.final_translation == normalized_polish
                )
                polish_audits[-1]["parsed"] = {
                    "final_translation": result.final_translation or "",
                    "warnings": list(result.quality_warnings),
                }
            quality_warnings: List[Any] = []
            warning_keys: set[str] = set()
            for warning in list(result.quality_warnings) + render_constraint_warnings:
                key = json.dumps(
                    warning,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ) if isinstance(warning, Mapping) else str(warning)
                if key in warning_keys:
                    continue
                warning_keys.add(key)
                quality_warnings.append(warning)
            audit_with_warnings = (
                polish_audits[-1]
                if polish_audits
                else draft_audits[-1] if draft_audits else None
            )
            if audit_with_warnings is not None and quality_warnings:
                parsed_with_warnings = dict(
                    audit_with_warnings.get("parsed") or {}
                )
                parsed_with_warnings["quality_warnings"] = quality_warnings
                audit_with_warnings["parsed"] = parsed_with_warnings
            matched_claim_ids = frozenset(
                str(claim_id) for claim_id in packet.matched_claim_ids
            )
            outcome = TranslationOutcome(
                block=block,
                knowledge_version=knowledge_version,
                status=status,
                draft_translation=result.draft_translation or "",
                final_translation=result.final_translation or "",
                analysis=result.analysis or "",
                semantic_obligations=result.semantic_obligations or "",
                memory_summary=result.memory_summary or "",
                warnings=quality_warnings,
                term_proposals=term_proposals,
                relation_proposals=relation_proposals,
                matched_concept_ids=list(packet.matched_concept_ids),
                matched_renderings=frozen_render_matches,
                claim_dependencies=tuple(
                    ClaimDependencySnapshot(
                        claim_id=str(claim["id"]),
                        semantic_fingerprint=str(claim["semantic_fingerprint"]),
                    )
                    for claim in frozen_claims_by_block.get(block.id, ())
                    if str(claim["id"]) in matched_claim_ids
                ),
                audit_calls=block_audits,
                attempts=attempts,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                error=result.error_message,
            )
            outcome.quality_warnings = quality_warnings
            outcomes.append(outcome)
            if status == V4BlockStatus.FAILED_RETRYABLE.value:
                break
            previous_source = block.source_text
            previous_translation = outcome.final_translation
            local_summary = outcome.memory_summary
        return outcomes

    def translate_block_factory(self) -> Callable[[V4Block], TranslationOutcome]:
        """Build the normal frozen-bundle translator used by revalidation."""

        def translate_block(block: V4Block) -> TranslationOutcome:
            if not isinstance(block, V4Block):
                raise TypeError("retranslation requires a V4Block")
            bundle = self.database.freeze_render_bundle([block.id])
            self._active_render_bundle = bundle
            outcomes = self._translate_island(
                Island(id=f"revalidate-{block.id}", blocks=[block]),
                bundle.knowledge_version,
                bundle.index,
            )
            if len(outcomes) != 1 or outcomes[0].block.id != block.id:
                raise RuntimeError("retranslation structure did not return one full block")
            return outcomes[0]

        return translate_block

    def run(self) -> Dict[str, Any]:
        eligible_statuses = [
            V4BlockStatus.READY.value,
            V4BlockStatus.FAILED_RETRYABLE.value,
            V4BlockStatus.NEEDS_REVALIDATE.value,
        ]
        if self.config.force:
            eligible_statuses.extend(
                [
                    V4BlockStatus.COMPLETED.value,
                    V4BlockStatus.COMPLETED_WITH_WARNINGS.value,
                ]
            )
        eligible = self.database.list_blocks(eligible_statuses)
        active = self.database.active_translations("parallel_v4")
        candidates = [
            block
            for block in eligible
            if self.config.force or not (
                block.id in active
                and active[block.id]["status"]
                in {
                    V4BlockStatus.COMPLETED.value,
                    V4BlockStatus.COMPLETED_WITH_WARNINGS.value,
                }
            )
        ]
        if self.config.include_block_ids:
            included = set(self.config.include_block_ids)
            candidates = [block for block in candidates if block.id in included]
        if self.config.max_blocks is not None:
            candidates = candidates[: self.config.max_blocks]
        islands = self._make_islands(candidates, self.config.island_size)
        run_id = f"translate_{uuid4().hex}"
        run_config = dict(self.config.__dict__)
        workers = min(self.config.initial_workers, self.config.max_workers)
        completed = warnings = failed = manual = 0
        paused = False
        knowledge_stale = False
        change_ids: set[int] = set()
        cursor = 0
        try:
            render_bundle = self.database.freeze_render_bundle(
                [block.id for block in candidates]
            )
            knowledge_version = render_bundle.knowledge_version
            concept_snapshot = render_bundle.index
            target_signature = render_bundle.signature
            self._active_render_bundle = render_bundle
        except KnowledgeSnapshotError as exc:
            self.database.fail_run_for_invalid_snapshot(
                run_id, "translate", run_config, exc
            )
            return {
                "run_id": run_id,
                "status": "failed",
                "completed": 0,
                "completed_with_warnings": 0,
                "failed_retryable": 0,
                "incomplete_requires_human": 0,
                "remaining_islands": len(islands),
                "final_workers": workers,
                "knowledge_stale": False,
                "frozen_knowledge_version": None,
                "change_ids": [],
            }
        run_config["frozen_knowledge_version"] = knowledge_version
        run_config["target_snapshot_signature"] = target_signature
        run_config["render_context_block_ids"] = list(render_bundle.block_ids)
        self.database.start_run(
            run_id,
            "translate",
            run_config,
            knowledge_version=knowledge_version,
        )
        try:
            while cursor < len(islands):
                wave = islands[cursor : cursor + workers]
                wave_outcomes: List[TranslationOutcome] = []
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(
                            self._translate_island,
                            island,
                            knowledge_version,
                            concept_snapshot,
                        ): island
                        for island in wave
                    }
                    for future in as_completed(futures):
                        wave_outcomes.extend(future.result())
                self.database.commit_translation_batch(
                    run_id,
                    wave_outcomes,
                    audit_mode=self.config.audit_mode,
                )
                proposal_result = self.database.commit_translation_proposals(
                    run_id,
                    wave_outcomes,
                    enqueue_review=self.config.decision_mode == "interactive",
                    return_change_ids=True,
                )
                if isinstance(proposal_result, dict):
                    proposal_version = proposal_result["knowledge_version"]
                    change_ids.update(
                        int(value) for value in proposal_result["change_ids"]
                    )
                else:
                    # Preserve compatibility with injected/legacy scalar writers.
                    proposal_version = proposal_result
                completed += sum(o.status == V4BlockStatus.COMPLETED.value for o in wave_outcomes)
                warnings += sum(
                    o.status == V4BlockStatus.COMPLETED_WITH_WARNINGS.value
                    for o in wave_outcomes
                )
                failed += sum(o.status == V4BlockStatus.FAILED_RETRYABLE.value for o in wave_outcomes)
                manual += sum(
                    o.status == V4BlockStatus.INCOMPLETE_REQUIRES_HUMAN.value
                    for o in wave_outcomes
                )
                wave_has_problem = any(
                    o.status
                    in {
                        V4BlockStatus.FAILED_RETRYABLE.value,
                        V4BlockStatus.INCOMPLETE_REQUIRES_HUMAN.value,
                    }
                    for o in wave_outcomes
                )
                if wave_has_problem:
                    workers = max(1, workers - 1)
                elif workers < self.config.max_workers:
                    workers += 1
                cursor += len(wave)
                current_signature = self.database.render_bundle_signature(
                    render_bundle.block_ids
                )
                if current_signature != target_signature:
                    knowledge_stale = True
                    break
                if self.config.decision_mode == "interactive" and proposal_version is not None:
                    paused = True
                    break
            desired_status = "paused_for_review" if paused else (
                "completed_with_errors" if failed or manual else "completed"
            )
            status, finalized_knowledge_stale = (
                self.database.finish_translation_run_atomically(
                    run_id,
                    target_signature,
                    desired_status,
                    force_revalidate=knowledge_stale,
                    context_block_ids=render_bundle.block_ids,
                )
            )
            knowledge_stale = knowledge_stale or finalized_knowledge_stale
        except KnowledgeSnapshotError as exc:
            self.database.fail_run_for_invalid_snapshot(
                run_id, "translate", run_config, exc
            )
            status = "failed"
        except Exception as exc:
            self.database.finish_run(run_id, "failed", str(exc))
            raise
        return {
            "run_id": run_id,
            "status": status,
            "completed": completed,
            "completed_with_warnings": warnings,
            "failed_retryable": failed,
            "incomplete_requires_human": manual,
            "remaining_islands": max(0, len(islands) - cursor),
            "final_workers": workers,
            "knowledge_stale": knowledge_stale,
            "frozen_knowledge_version": knowledge_version,
            "change_ids": sorted(change_ids),
        }
