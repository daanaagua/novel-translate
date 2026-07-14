"""Evidence-only parallel scanner with strict structured output."""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional
from uuid import uuid4

from .database import V4Database
from .models import ScanOutcome, ScanResponse, V4Block, V4BlockStatus


SCAN_SCHEMA_VERSION = "scan-v4.1"


SCAN_SYSTEM_PROMPT = """你是英语长篇小说的翻译预扫描器。你只抽取会直接影响中文译法的可证实信息，不解释主题、象征或叙述陷阱。

必须遵守：
1. 只报告当前文本块中有原文证据的实体、专名、职衔、技术概念、计量单位和直接称呼。
2. 不推断未明示的真实身份、性别、阵营或象征意义。
3. suggested_target 可以为空；不确定时不得强猜。
4. evidence_quote 必须逐字取自对应段落。
5. ambiguity 只记录中文容易擅自消除的歧义，并写成保守翻译约束。
6. 只输出一个合法 JSON 对象，不得输出 Markdown 或说明。
7. discourse_function 只能是 referential、vocative、institutional、unknown；人物名通常是 referential。
8. JSON 字符串内容不得包含未转义的半角双引号；中文引用一律使用「」或『』。
9. category 只能是 person、place、organization、group、item、concept、unit、title、event、species、technology、work。

输出格式：
{
  "mentions": [
    {
      "paragraph_id": "P000",
      "source_form": "Archon",
      "category": "title",
      "suggested_target": "执政官",
      "description": "政治职衔",
      "discourse_function": "vocative",
      "evidence_quote": "Archon",
      "confidence": 0.95
    }
  ],
  "ambiguities": [
    {
      "paragraph_id": "P001",
      "evidence_quote": "the figure",
      "constraint": "身份和性别均未明，不得擅自使用姓名或性别代词",
      "confidence": 0.8
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
    ):
        self.database = database
        self.llm = llm_manager
        self.max_attempts = max(1, max_attempts)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.audit_mode = audit_mode

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

    def scan_block(self, block: V4Block) -> ScanOutcome:
        paragraphs = paragraph_map(block.source_text)
        request = {
            "schema_version": SCAN_SCHEMA_VERSION,
            "messages": [
                {"role": "system", "content": SCAN_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "请扫描以下带段落编号的原文：\n\n"
                    + render_numbered_source(block.source_text),
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
                                "上一次输出未通过严格校验。错误是："
                                f"{last_error[:500]}\n"
                                "请从头重新输出完整JSON；不得解释，不得复用错误片段。"
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
                data = json.loads(self._clean_json(last_raw))
                response = ScanResponse.model_validate(data)
                self._validate_evidence(response, paragraphs)
                audit_calls.append(
                    {
                        "request": current_request,
                        "raw_response": last_raw,
                        "parsed": response.model_dump(mode="json"),
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
