"""Resolve provisional book-wide translations through bounded local aliases."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Sequence
from uuid import uuid4

from .models import WorkingTargetResponse


TARGET_PROTOCOL = """CANONICAL WORKING-TARGET JSON PROTOCOL
The root object has exactly one key, "decisions". Its value is an array with one
object for every supplied concept. Each decision object has exactly these four keys:
"concept_id", "working_target", "rules", "confidence".
"concept_id" must be the supplied local Qxx alias. "working_target" must be a
non-empty Chinese string. "confidence" must be a number from 0 through 1. "rules"
must be an array containing no more than six rule objects. Each rule object has
exactly these two keys: "condition", "target". "condition" must be a non-empty
JSON object whose concrete contextual property names and values are strings;
"target" must be a non-empty Chinese string.

This is a complete one-decision JSON template (use [] for rules when none apply):
{"decisions":[{"concept_id":"Q01","working_target":"示例译名","rules":[{"condition":{"discourse_function":"vocative"},"target":"阁下"}],"confidence":0.95}]}

Do not use synonym keys such as "translation", "rule", "when", "id", or
"default_target". Never copy or invent an identifier. Do not return Markdown,
prose, schema commentary, or any keys other than the canonical keys above.
"""


TARGET_SYSTEM = f"""You choose provisional Chinese working translations for recurring
fictional names and terms. The main target must be a non-empty, stable book-wide
rendering. Add a contextual rule only when the same source form genuinely needs a
different Chinese rendering in a clearly expressible local condition.

{TARGET_PROTOCOL}"""


def _target_retry_message(last_error: str) -> str:
    return (
        "The previous answer failed strict validation. Correct the complete answer "
        "and return it again. The full validator report follows:\n"
        f"{last_error}\n\n{TARGET_PROTOCOL}"
    )


class TargetResolutionProtocolError(ValueError):
    """The model response cannot be safely mapped to the current concept batch."""


class TargetResolver:
    def __init__(
        self,
        database: Any,
        llm: Any,
        max_attempts: int = 2,
        max_tokens: int = 8192,
        audit_mode: str = "full",
    ):
        if audit_mode not in {"full", "response", "minimal"}:
            raise ValueError("audit_mode must be full, response, or minimal")
        self.database = database
        self.llm = llm
        self.max_attempts = max(1, int(max_attempts))
        self.max_tokens = max(1, int(max_tokens))
        self.audit_mode = audit_mode

    @staticmethod
    def _clean_json(raw: str) -> str:
        text = str(raw).strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return text.strip()

    @staticmethod
    def _aliased_batch(
        concepts: Sequence[Dict[str, Any]],
    ) -> tuple[Dict[str, str], Dict[str, Any]]:
        if not concepts or len(concepts) > 24:
            raise ValueError("a target-resolution batch must contain 1..24 concepts")
        alias_map: Dict[str, str] = {}
        public = []
        for index, concept in enumerate(concepts, start=1):
            alias = f"Q{index:02d}"
            alias_map[alias] = str(concept["concept_id"])
            public.append(
                {
                    "concept_id": alias,
                    "source": concept["source"],
                    "kind": concept["kind"],
                    "description": concept.get("description") or "",
                    "contexts": list(concept.get("contexts") or [])[:3],
                    "baseline_translations": list(
                        concept.get("baseline_translations") or []
                    )[:2],
                }
            )
        return alias_map, {"concepts": public}

    @staticmethod
    def _resolve_response(
        parsed: WorkingTargetResponse,
        alias_map: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        decisions: Dict[str, Any] = {}
        for decision in parsed.decisions:
            alias = decision.concept_id
            if alias in decisions:
                raise TargetResolutionProtocolError(f"duplicate alias: {alias}")
            if alias not in alias_map:
                raise TargetResolutionProtocolError(f"unknown alias: {alias}")
            decisions[alias] = decision
        if set(decisions) != set(alias_map):
            raise TargetResolutionProtocolError("missing working-target alias")
        return [
            {
                "concept_id": alias_map[alias],
                "target": decisions[alias].working_target,
                "rules": [rule.model_dump() for rule in decisions[alias].rules],
                "confidence": decisions[alias].confidence,
            }
            for alias in sorted(alias_map)
        ]

    def _call_batch(
        self,
        concepts: Sequence[Dict[str, Any]],
        *,
        run_id: str | None = None,
        knowledge_version: int | None = None,
    ) -> tuple[List[Dict[str, Any]] | None, str]:
        alias_map, payload = self._aliased_batch(concepts)
        base_messages = [
            {"role": "system", "content": TARGET_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        last_error = ""
        for _attempt in range(1, self.max_attempts + 1):
            messages = list(base_messages)
            if last_error:
                messages.append(
                    {
                        "role": "user",
                        "content": _target_retry_message(last_error),
                    }
                )
            started = time.perf_counter()
            raw = ""
            parsed_payload = None
            try:
                raw = self.llm.chat(
                    messages=messages,
                    purpose="working_target",
                    temperature=0.0,
                    max_tokens=self.max_tokens,
                    json_mode=True,
                    stream=False,
                )
                parsed = WorkingTargetResponse.model_validate_json(
                    self._clean_json(raw)
                )
                parsed_payload = parsed.model_dump(mode="json")
                decisions = self._resolve_response(parsed, alias_map)
            except Exception as exc:
                last_error = str(exc)
                if run_id:
                    self._record_audit_attempt(
                        run_id,
                        messages,
                        raw,
                        parsed_payload,
                        accepted=False,
                        attempt=_attempt,
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        error=last_error,
                        knowledge_version=knowledge_version,
                    )
                continue
            if run_id:
                self._record_audit_attempt(
                    run_id,
                    messages,
                    raw,
                    parsed_payload,
                    accepted=True,
                    attempt=_attempt,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    error=None,
                    knowledge_version=knowledge_version,
                )
            return decisions, ""
        return None, last_error or "model protocol failure"

    def _record_audit_attempt(
        self,
        run_id: str,
        messages: Sequence[Dict[str, str]],
        raw_response: str,
        parsed: Dict[str, Any] | None,
        *,
        accepted: bool,
        attempt: int,
        elapsed_ms: int,
        error: str | None,
        knowledge_version: int | None,
    ) -> None:
        version = (
            int(knowledge_version)
            if knowledge_version is not None
            else int(self.database.current_knowledge_version())
        )
        try:
            model = self.llm.get_model("working_target")
        except (AttributeError, KeyError, TypeError):
            model = type(self.llm).__name__
        self.database.record_audit_call(
            run_id=run_id,
            block_id=None,
            purpose="working_target",
            model=str(model),
            knowledge_version=version,
            request={"messages": list(messages), "audit_mode": self.audit_mode},
            raw_response=str(raw_response or ""),
            parsed=parsed,
            accepted=accepted,
            attempts=attempt,
            elapsed_ms=elapsed_ms,
            error=error,
        )

    def run(
        self,
        max_concepts: int | None = None,
        *,
        prepared_block_ids: Sequence[str] | None = None,
    ) -> Dict[str, Any]:
        candidates = self.database.working_target_candidates()
        if max_concepts is not None:
            candidates = candidates[: max(0, int(max_concepts))]
        run_id = f"target_{uuid4().hex}"
        self.database.start_run(
            run_id,
            "working_target",
            {
                "max_concepts": max_concepts,
                "batch_size": 24,
                "max_attempts": self.max_attempts,
            },
        )
        knowledge_version = int(self.database.current_knowledge_version())
        resolved = queued = changed = 0
        last_version = None
        affected_blocks = 0
        try:
            for start in range(0, len(candidates), 24):
                batch = candidates[start : start + 24]
                decisions, error = self._call_batch(
                    batch,
                    run_id=run_id,
                    knowledge_version=knowledge_version,
                )
                if decisions is None:
                    self.database.enqueue_working_target_review(
                        [item["concept_id"] for item in batch], error
                    )
                    queued += len(batch)
                    continue
                applied = self.database.apply_working_target_decisions(decisions)
                resolved += int(applied["resolved"])
                changed += int(applied["changed"])
                affected_blocks += int(applied["affected_blocks"])
                if applied["knowledge_version"] is not None:
                    last_version = applied["knowledge_version"]
            status = "completed_with_errors" if queued else "completed"
            self.database.finish_run(run_id, status)
        except Exception as exc:
            self.database.finish_run(run_id, "failed", str(exc))
            raise
        preparation = self.database.advance_prepared_blocks(prepared_block_ids)
        return {
            "run_id": run_id,
            "resolved": resolved,
            "queued": queued,
            "changed": changed,
            "knowledge_version": last_version,
            "affected_blocks": affected_blocks,
            "prepared_blocks": preparation,
        }
