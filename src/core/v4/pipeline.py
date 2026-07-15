"""Barriered parallel translation coordinator for the parallel_v4 shadow pipeline."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence
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
from .database import V4Database
from .models import Island, TranslationOutcome, V4Block, V4BlockStatus
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
    ) -> GlossaryManager:
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
            items.append(
                GlossaryItem(
                    id=concept["id"],
                    src=src,
                    default_target=target,
                    category=self._category(concept.get("kind") or "concept"),
                    status=TermStatus.VERIFIED if verified else TermStatus.PENDING,
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
        engine = TranslationEngine(
            llm_manager=audited_llm,
            glossary_manager=self._glossary_for(
                island.blocks, concept_snapshot=concept_snapshot
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
            try:
                packet = self.context_builder.build(
                    block,
                    previous_source=previous_source,
                    previous_translation=previous_translation,
                    local_summary=local_summary,
                    knowledge_version=knowledge_version,
                    concept_snapshot=concept_snapshot,
                )
            except ContextOverflow as exc:
                outcomes.append(
                    TranslationOutcome(
                        block=block,
                        knowledge_version=knowledge_version,
                        status=V4BlockStatus.INCOMPLETE_REQUIRES_HUMAN.value,
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
            outcome = TranslationOutcome(
                block=block,
                knowledge_version=knowledge_version,
                status=status,
                draft_translation=result.draft_translation or "",
                final_translation=result.final_translation or "",
                analysis=result.analysis or "",
                semantic_obligations=result.semantic_obligations or "",
                memory_summary=result.memory_summary or "",
                warnings=list(result.quality_warnings),
                term_proposals=term_proposals,
                relation_proposals=relation_proposals,
                claim_dependencies=list(packet.matched_claim_ids),
                audit_calls=block_audits,
                attempts=attempts,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                error=result.error_message,
            )
            outcomes.append(outcome)
            if status == V4BlockStatus.FAILED_RETRYABLE.value:
                break
            previous_source = block.source_text
            previous_translation = outcome.final_translation
            local_summary = outcome.memory_summary
        return outcomes

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
        (
            knowledge_version,
            concept_snapshot,
            target_signature,
        ) = self.database.freeze_translation_knowledge()
        run_config["frozen_knowledge_version"] = knowledge_version
        run_config["target_snapshot_signature"] = target_signature
        self.database.start_run(
            run_id,
            "translate",
            run_config,
            knowledge_version=knowledge_version,
        )
        workers = min(self.config.initial_workers, self.config.max_workers)
        completed = warnings = failed = manual = 0
        paused = False
        knowledge_stale = False
        try:
            cursor = 0
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
                proposal_version = self.database.commit_translation_proposals(
                    run_id,
                    wave_outcomes,
                    enqueue_review=self.config.decision_mode == "interactive",
                )
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
                current_signature = self.database.target_snapshot_signature(
                    self.database.concept_snapshot()
                )
                if current_signature != target_signature:
                    self.database.invalidate_translation_run(run_id)
                    knowledge_stale = True
                    break
                if self.config.decision_mode == "interactive" and proposal_version is not None:
                    paused = True
                    break
            status = "stale_knowledge" if knowledge_stale else "paused_for_review" if paused else (
                "completed_with_errors" if failed or manual else "completed"
            )
            self.database.finish_run(run_id, status)
        except Exception as exc:
            self.database.finish_run(run_id, "failed", str(exc))
            raise
        return {
            "run_id": run_id,
            "status": (
                "stale_knowledge"
                if knowledge_stale
                else "paused_for_review" if paused else "completed"
            ),
            "completed": completed,
            "completed_with_warnings": warnings,
            "failed_retryable": failed,
            "incomplete_requires_human": manual,
            "remaining_islands": max(0, len(islands) - cursor),
            "final_workers": workers,
            "knowledge_stale": knowledge_stale,
            "frozen_knowledge_version": knowledge_version,
        }
