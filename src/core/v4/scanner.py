"""Candidate-indexed parallel scanner with deterministic source evidence."""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional
from uuid import uuid4

from .database import V4Database
from .lexical_index import LexicalCandidateExtractor
from .models import (
    CandidateScanResponse,
    ScanMention,
    ScanOutcome,
    ScanResponse,
    V4Block,
    V4BlockStatus,
)


SCAN_SCHEMA_VERSION = "scan-v4.2-candidate-index"


SCAN_SYSTEM_PROMPT = """你是英语长篇小说的候选词分类器。候选词及上下文由本地脚本生成，标点已被删除，句界只保留为句号。你不得补写原文，也不负责解释主题、象征、人物关系、时间线或叙述陷阱。

必须遵守：
1. 输入中的每一项都有稳定candidate_id。只返回确实会影响中文翻译的实体、专名、职衔、人造概念、计量单位、物种名和作品名。
2. 不推断未明示的真实身份、性别、阵营或象征意义。
3. 普通英语词、一次性的普通名词、章节标题、版权页信息、出版社、DRM、泛称和仅为句首大写的词不得返回。
4. suggested_target可以是空字符串；不确定时不得猜测。不得返回null。
5. 只能使用输入中存在的candidate_id，不得复制原文证据、段落编号或标点。
6. 同一短语的嵌套候选通常只保留真正独立有用的词条。若一个候选只是另一个候选的头衔变体、所有格或明显词形变化，可用canonical_candidate_id指向同批候选中的规范形式；不确定则留空。
7. discourse_function只能是referential、vocative、institutional、unknown；人物名通常是referential。
8. category只能是person、place、organization、group、item、concept、unit、title、event、species、technology、work。
9. 只输出一个合法JSON对象，不得输出Markdown、歧义判断或说明。

输出格式：
{
  "mentions": [
    {
      "candidate_id": "C012",
      "category": "title",
      "suggested_target": "执政官",
      "description": "政治职衔",
      "discourse_function": "vocative",
      "canonical_candidate_id": null,
      "confidence": 0.95
    }
  ]
}
"""


def paragraph_map(source_text: str) -> Dict[str, str]:
    paragraphs = [
        part.strip()
        for part in re.split(r"\n\s*\n", source_text.strip())
        if part.strip()
    ]
    return {f"P{index:03d}": paragraph for index, paragraph in enumerate(paragraphs)}


def render_numbered_source(source_text: str) -> str:
    """Legacy diagnostic renderer; candidate-indexed scanning no longer sends this text."""
    return "\n\n".join(
        f"[{paragraph_id}]\n{text}"
        for paragraph_id, text in paragraph_map(source_text).items()
    )


class ScanProtocolError(ValueError):
    pass


class V4Scanner:
    def __init__(
        self,
        database: V4Database,
        llm_manager,
        max_attempts: int = 3,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        audit_mode: str = "full",
        max_candidates_per_block: int = 80,
        context_words: int = 4,
    ):
        self.database = database
        self.llm = llm_manager
        self.max_attempts = max(1, max_attempts)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.audit_mode = audit_mode
        self.extractor = LexicalCandidateExtractor(
            database.list_blocks(),
            max_candidates=max_candidates_per_block,
            context_words=context_words,
        )

    @staticmethod
    def _clean_json(raw: str) -> str:
        cleaned = raw.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        return cleaned.strip()

    @staticmethod
    def _validate_evidence(response: ScanResponse, paragraphs: Dict[str, str]) -> None:
        """Legacy validator retained for imported v4.1 scan responses and tests."""
        for item in [*response.mentions, *response.ambiguities]:
            paragraph = paragraphs.get(item.paragraph_id)
            if paragraph is None:
                raise ScanProtocolError(f"未知段落编号: {item.paragraph_id}")
            quote = " ".join(item.evidence_quote.split())
            normalized_paragraph = " ".join(paragraph.split())
            if quote not in normalized_paragraph:
                raise ScanProtocolError(
                    f"证据不在 {item.paragraph_id} 原文中: {item.evidence_quote!r}"
                )

    @staticmethod
    def _repair_unique_evidence_locations(
        response: ScanResponse, paragraphs: Dict[str, str]
    ) -> None:
        """Legacy repair retained for imported v4.1 scan responses and tests."""
        normalized_paragraphs = {
            paragraph_id: " ".join(paragraph.split())
            for paragraph_id, paragraph in paragraphs.items()
        }
        for item in [*response.mentions, *response.ambiguities]:
            quote = " ".join(item.evidence_quote.split())
            declared = normalized_paragraphs.get(item.paragraph_id, "")
            if quote in declared:
                continue
            matches = [
                paragraph_id
                for paragraph_id, paragraph in normalized_paragraphs.items()
                if quote in paragraph
            ]
            if len(matches) == 1:
                item.paragraph_id = matches[0]

    @staticmethod
    def _is_front_matter(block: V4Block) -> bool:
        legacy = getattr(block, "legacy_id", "").casefold()
        return (
            "_pre_" in legacy
            or legacy.startswith("v00_")
            or block.block_type.casefold() in {"frontmatter", "copyright", "title_page"}
        )

    @staticmethod
    def _sanitize_candidate_response(data: object) -> Dict[str, object]:
        if not isinstance(data, dict):
            raise ScanProtocolError("顶层必须是JSON对象")
        raw_mentions = data.get("mentions", [])
        if not isinstance(raw_mentions, list):
            raise ScanProtocolError("mentions必须是数组")
        category_aliases = {
            "institution": "organization",
            "location": "place",
            "object": "item",
            "role": "title",
            "book": "work",
            "race": "species",
        }
        discourse_aliases = {
            "reference": "referential",
            "address": "vocative",
            "institution": "institutional",
        }
        mentions = []
        for raw in raw_mentions:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            category = str(item.get("category") or "concept").casefold()
            discourse = str(item.get("discourse_function") or "referential").casefold()
            item["category"] = category_aliases.get(category, category)
            item["discourse_function"] = discourse_aliases.get(discourse, discourse)
            for field in ("suggested_target", "description"):
                if item.get(field) is None:
                    item[field] = ""
            if item.get("canonical_candidate_id") in {"", "null", "None"}:
                item["canonical_candidate_id"] = None
            mentions.append(item)
        return {"mentions": mentions}

    @staticmethod
    def _deterministic_canonical_form(original_text: str, normalized_text: str, category: str) -> str:
        words = normalized_text.split()
        title_prefixes = {
            "master", "brother", "father", "mother", "saint", "holy",
            "lord", "lady", "doctor", "captain", "general", "chatelaine",
        }
        if category == "person" and len(words) >= 2 and words[0].casefold() in title_prefixes:
            return " ".join(words[1:])
        if re.search(r"[’']s$", original_text, flags=re.I):
            return normalized_text
        if category in {"title", "group", "species", "concept"} and len(words) == 1:
            word = words[0]
            lower = word.casefold()
            if lower.endswith("ies") and len(word) > 4:
                return word[:-3] + ("Y" if word[-3:].isupper() else "y")
            if (
                lower.endswith("s")
                and not lower.endswith(("ss", "us", "is", "ses"))
                and len(word) > 4
            ):
                return word[:-1]
        return ""

    def scan_block(self, block: V4Block) -> ScanOutcome:
        candidates = [] if self._is_front_matter(block) else self.extractor.extract(block)
        candidate_rows = [candidate.storage_payload() for candidate in candidates]
        if not candidates:
            return ScanOutcome(
                block=block,
                response=ScanResponse(mentions=[], ambiguities=[]),
                request_payload={"schema_version": SCAN_SCHEMA_VERSION, "messages": []},
                attempts=0,
                elapsed_ms=0,
                lexical_candidates=candidate_rows,
            )

        candidate_map = {
            f"C{index:03d}": candidate for index, candidate in enumerate(candidates)
        }
        request = {
            "schema_version": SCAN_SCHEMA_VERSION,
            "messages": [
                {"role": "system", "content": SCAN_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "请分类以下候选词。只返回需要保留的candidate_id：\n\n"
                    + json.dumps(
                        {
                            "columns": ["candidate_id", "candidate", "context"],
                            "candidates": [
                                [
                                    model_id,
                                    candidate.normalized_text,
                                    " ".join(
                                        part
                                        for part in (
                                            candidate.left_context,
                                            candidate.normalized_text,
                                            candidate.right_context,
                                        )
                                        if part
                                    ),
                                ]
                                for model_id, candidate in candidate_map.items()
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        started = time.perf_counter()
        last_raw = ""
        last_error: Optional[str] = None
        current_request = request
        audit_calls: List[Dict[str, object]] = []

        for attempt in range(1, self.max_attempts + 1):
            attempt_started = time.perf_counter()
            last_raw = ""
            try:
                messages = list(request["messages"])
                if last_error:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "上一次输出未通过校验。错误是："
                                f"{last_error[:500]}\n"
                                "请从头重新输出完整JSON；只能使用候选列表中的candidate_id。"
                            ),
                        }
                    )
                current_request = {**request, "messages": messages}
                last_raw = self.llm.chat(
                    messages=messages,
                    purpose="scan",
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    json_mode=True,
                    stream=False,
                )
                data = self._sanitize_candidate_response(
                    json.loads(self._clean_json(last_raw))
                )
                classified = CandidateScanResponse.model_validate(data)
                seen_ids = set()
                mentions = []
                for decision in classified.mentions:
                    if decision.candidate_id in seen_ids:
                        raise ScanProtocolError(
                            f"candidate_id重复: {decision.candidate_id}"
                        )
                    seen_ids.add(decision.candidate_id)
                    candidate = candidate_map.get(decision.candidate_id)
                    if candidate is None:
                        raise ScanProtocolError(
                            f"未知candidate_id: {decision.candidate_id}"
                        )
                    canonical_form = self._deterministic_canonical_form(
                        candidate.original_text,
                        candidate.normalized_text,
                        decision.category,
                    )
                    if decision.canonical_candidate_id:
                        canonical = candidate_map.get(decision.canonical_candidate_id)
                        if canonical is None:
                            raise ScanProtocolError(
                                "canonical_candidate_id不在当前候选列表中: "
                                f"{decision.canonical_candidate_id}"
                            )
                        if not canonical_form:
                            source_word_count = len(candidate.normalized_text.split())
                            canonical_word_count = len(canonical.normalized_text.split())
                            if canonical_word_count <= source_word_count:
                                canonical_form = canonical.original_text
                    mentions.append(
                        ScanMention(
                            paragraph_id=candidate.paragraph_id,
                            source_form=candidate.original_text,
                            category=decision.category,
                            suggested_target=decision.suggested_target,
                            description=decision.description,
                            discourse_function=decision.discourse_function,
                            evidence_quote=candidate.original_text,
                            confidence=decision.confidence,
                            candidate_id=candidate.id,
                            canonical_form=canonical_form,
                        )
                    )
                response = ScanResponse(mentions=mentions, ambiguities=[])
                selected = {mention.candidate_id for mention in mentions}
                candidate_rows = [
                    candidate.storage_payload(
                        selected=candidate.id in selected,
                        model_status="selected" if candidate.id in selected else "rejected",
                    )
                    for candidate in candidates
                ]
                audit_calls.append(
                    {
                        "request": current_request,
                        "raw_response": last_raw,
                        "parsed": classified.model_dump(mode="json"),
                        "accepted": True,
                        "attempts": attempt,
                        "elapsed_ms": int((time.perf_counter() - attempt_started) * 1000),
                    }
                )
                return ScanOutcome(
                    block=block,
                    response=response,
                    raw_response=last_raw,
                    request_payload=current_request,
                    attempts=attempt,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    audit_calls=audit_calls,
                    lexical_candidates=candidate_rows,
                )
            except Exception as exc:
                last_error = str(exc)
                audit_calls.append(
                    {
                        "request": current_request,
                        "raw_response": last_raw,
                        "parsed": None,
                        "accepted": False,
                        "attempts": attempt,
                        "elapsed_ms": int((time.perf_counter() - attempt_started) * 1000),
                        "error": last_error,
                    }
                )
                if attempt >= self.max_attempts:
                    break

        return ScanOutcome(
            block=block,
            response=None,
            raw_response=last_raw,
            request_payload=current_request,
            attempts=self.max_attempts,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=last_error or "未知扫描错误",
            audit_calls=audit_calls,
            lexical_candidates=[
                {**row, "model_status": "model_failed"} for row in candidate_rows
            ],
        )

    def scan_project(
        self,
        initial_workers: int = 2,
        max_workers: int = 4,
        max_blocks: Optional[int] = None,
    ) -> Dict[str, int | str]:
        candidates = self.database.list_blocks(
            [V4BlockStatus.PENDING.value, V4BlockStatus.FAILED_RETRYABLE.value]
        )
        if max_blocks is not None:
            candidates = candidates[:max_blocks]
        run_id = f"scan_{uuid4().hex}"
        config = {
            "initial_workers": initial_workers,
            "max_workers": max_workers,
            "max_blocks": max_blocks,
            "schema_version": SCAN_SCHEMA_VERSION,
        }
        self.database.start_run(run_id, "scan", config)
        workers = max(1, min(initial_workers, max_workers))
        completed = failed = 0
        try:
            cursor = 0
            while cursor < len(candidates):
                wave = candidates[cursor : cursor + workers]
                outcomes: List[ScanOutcome] = []
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {executor.submit(self.scan_block, block): block for block in wave}
                    for future in as_completed(futures):
                        outcomes.append(future.result())
                self.database.commit_scan_batch(
                    run_id,
                    outcomes,
                    self.llm.get_model("scan"),
                    audit_mode=self.audit_mode,
                )
                wave_failed = sum(outcome.response is None for outcome in outcomes)
                completed += len(outcomes) - wave_failed
                failed += wave_failed
                if wave_failed:
                    workers = max(1, workers - 1)
                elif workers < max_workers:
                    workers += 1
                cursor += len(wave)
            self.database.finish_run(run_id, "completed" if not failed else "completed_with_errors")
        except Exception as exc:
            self.database.finish_run(run_id, "failed", str(exc))
            raise
        return {
            "run_id": run_id,
            "completed": completed,
            "failed": failed,
            "final_workers": workers,
        }
