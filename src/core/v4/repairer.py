"""Block-scoped repair with strict paragraph preservation and versioned commits."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from ..translator import TranslationEngine
from .context import ContextBuilder
from .database import V4Database
from .models import RepairResponse
from .validation import V4Validator


REPAIR_SYSTEM = """你是文学译文的局部修复器。你只修复给定文本块，不改动相邻文本块，也不扩写、删节或解释剧情。
必须遵守：
1. paragraphs 数组的元素数量必须与 source_paragraphs 完全相同；每个元素对应同序号原文段落。
2. 只处理 issues 中列出的问题，同时保持原译文中没有问题的内容、语气和段落边界。
3. 不输出脚注，不把 repair_notes 混入正文。
4. 只输出合法JSON：{"paragraphs":["..."],"repair_notes":["..."]}。
5. JSON字符串内部引用中文时使用「」或『』，不要使用未转义的半角双引号。"""


def split_paragraphs(text: str) -> List[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]


class V4Repairer:
    def __init__(
        self,
        database: V4Database,
        llm_factory: Callable[[], Any],
        max_attempts: int = 2,
        max_tokens: int = 37200,
        max_context_chars: int = 24000,
    ):
        self.database = database
        self.llm_factory = llm_factory
        self.max_attempts = max(1, max_attempts)
        self.max_tokens = max_tokens
        self.max_context_chars = max_context_chars

    @staticmethod
    def _clean_json(raw: str) -> str:
        text = raw.strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return text.strip()

    def _repair_one(self, task: Dict[str, Any], run_id: str) -> bool:
        detail = self.database.get_review_block(task["block_id"])
        source_paragraphs = split_paragraphs(detail["source_text"])
        current_translation = detail.get("v4_translation") or detail.get("draft_translation") or ""
        if not current_translation.strip():
            self.database.commit_repair_failure(
                task["id"], "当前文本块没有可修复译文", run_id=run_id
            )
            return False
        issues = json.loads(task["issues_json"])
        block = self.database.get_block_by_identifier(task["block_id"])
        try:
            context = ContextBuilder(self.database, self.max_context_chars).build(block).rendered
        except Exception as exc:
            self.database.commit_repair_failure(task["id"], str(exc), run_id=run_id)
            return False
        request = {
            "issues": issues,
            "source_paragraphs": source_paragraphs,
            "current_translation": current_translation,
            "translation_constraints": context,
        }
        base_messages = [
            {"role": "system", "content": REPAIR_SYSTEM},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False, indent=2)},
        ]
        llm = self.llm_factory()
        started = time.perf_counter()
        last_error: Optional[str] = None
        last_raw = ""
        for attempt in range(1, self.max_attempts + 1):
            messages = list(base_messages)
            if last_error:
                messages.append(
                    {
                        "role": "user",
                        "content": f"上次输出未通过校验：{last_error[:600]}。请返回完整、合法且段落数正确的JSON。",
                    }
                )
            try:
                last_raw = llm.chat(
                    messages=messages,
                    purpose="repair",
                    temperature=0.0,
                    max_tokens=self.max_tokens,
                    json_mode=True,
                    stream=False,
                )
                parsed = RepairResponse.model_validate_json(self._clean_json(last_raw))
                if len(parsed.paragraphs) != len(source_paragraphs):
                    raise ValueError(
                        f"修复段落数为{len(parsed.paragraphs)}，原文段落数为{len(source_paragraphs)}"
                    )
                if any(not paragraph.strip() for paragraph in parsed.paragraphs):
                    raise ValueError("修复结果包含空段落")
                final_translation = "\n\n".join(item.strip() for item in parsed.paragraphs)
                if V4Validator._has_wrapper(final_translation):
                    raise ValueError("修复结果含代码块或结构包装标记")
                shape_problems: List[str] = []
                if block.block_type != "bibliography":
                    shape_problems.extend(
                        TranslationEngine._translation_shape_problems(
                            block.source_text,
                            final_translation,
                            stage="局部修复",
                            min_length_ratio=0.15,
                        )
                    )
                draft_translation = detail.get("draft_translation") or ""
                if draft_translation.strip():
                    shape_problems.extend(
                        TranslationEngine._translation_shape_problems(
                            draft_translation,
                            final_translation,
                            stage="局部修复定稿",
                            min_length_ratio=0.75,
                        )
                    )
                if shape_problems:
                    raise ValueError("；".join(shape_problems))
                audit = {
                    "purpose": "repair",
                    "model": llm.get_model("repair"),
                    "request": {"messages": messages, "json_mode": True},
                    "raw_response": last_raw,
                    "parsed": parsed.model_dump(mode="json"),
                    "accepted": True,
                    "attempts": attempt,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                }
                self.database.commit_repair_result(
                    run_id, task["id"], final_translation, audit
                )
                return True
            except Exception as exc:
                last_error = str(exc)
        audit = {
            "purpose": "repair",
            "model": llm.get_model("repair"),
            "request": {"messages": base_messages, "json_mode": True},
            "raw_response": last_raw,
            "parsed": None,
            "accepted": False,
            "attempts": self.max_attempts,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "error": last_error or "未知修复错误",
        }
        self.database.commit_repair_failure(
            task["id"], last_error or "未知修复错误", run_id=run_id, audit=audit
        )
        return False

    def run(
        self,
        max_tasks: Optional[int] = None,
        block_identifier: Optional[str] = None,
        issues: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if block_identifier:
            self.database.request_repair(block_identifier, issues or ["人工请求局部复核"])
        tasks = self.database.list_repair_tasks("open")
        if not block_identifier and not tasks:
            repairable_codes = {
                "completed_with_warnings",
                "output_wrapper",
                "final_shape",
            }
            grouped: Dict[str, List[str]] = {}
            for issue in V4Validator(self.database).validate().issues:
                if issue.code in repairable_codes:
                    grouped.setdefault(issue.block_id, []).append(
                        f"{issue.code}: {issue.message}"
                    )
            for identifier, block_issues in grouped.items():
                self.database.request_repair(identifier, block_issues)
            tasks = self.database.list_repair_tasks("open")
        if block_identifier:
            block = self.database.get_block_by_identifier(block_identifier)
            tasks = [task for task in tasks if task["block_id"] == block.id]
        if max_tasks is not None:
            tasks = tasks[:max_tasks]
        run_id = f"repair_{uuid4().hex}"
        self.database.start_run(run_id, "repair", {"max_tasks": max_tasks, "block": block_identifier})
        completed = needs_human = 0
        try:
            for task in tasks:
                if self._repair_one(task, run_id):
                    completed += 1
                else:
                    needs_human += 1
            self.database.finish_run(run_id, "completed")
        except Exception as exc:
            self.database.finish_run(run_id, "failed", str(exc))
            raise
        return {
            "run_id": run_id,
            "tasks": len(tasks),
            "completed": completed,
            "needs_human": needs_human,
        }
