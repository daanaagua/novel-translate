"""Resolve provisional book-wide translations through bounded local aliases."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence
from uuid import uuid4

from .models import WorkingTargetResponse


TARGET_SYSTEM = """You choose provisional Chinese working translations for recurring
fictional names and terms. Return strict JSON as {"decisions":[...]}; each decision
contains concept_id, working_target, rules, and confidence, with exactly
one decision for every Q alias supplied. Never copy or invent an identifier. The
main target must be a non-empty, stable book-wide rendering. Add a contextual rule
only when the same source form genuinely needs a different Chinese rendering in a
clearly expressible local condition. Use no more than six rules per concept.
"""


class TargetResolutionProtocolError(ValueError):
    """The model response cannot be safely mapped to the current concept batch."""


class TargetResolver:
    def __init__(
        self,
        database: Any,
        llm: Any,
        max_attempts: int = 2,
        max_tokens: int = 8192,
    ):
        self.database = database
        self.llm = llm
        self.max_attempts = max(1, int(max_attempts))
        self.max_tokens = max(1, int(max_tokens))

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
                        "content": (
                            "The previous answer failed strict validation: "
                            f"{last_error[:500]}. Return the complete JSON again."
                        ),
                    }
                )
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
                return self._resolve_response(parsed, alias_map), ""
            except Exception as exc:
                last_error = str(exc)
        return None, last_error or "model protocol failure"

    def run(self, max_concepts: int | None = None) -> Dict[str, Any]:
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
        resolved = queued = changed = 0
        last_version = None
        affected_blocks = 0
        try:
            for start in range(0, len(candidates), 24):
                batch = candidates[start : start + 24]
                decisions, error = self._call_batch(batch)
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
        return {
            "run_id": run_id,
            "resolved": resolved,
            "queued": queued,
            "changed": changed,
            "knowledge_version": last_version,
            "affected_blocks": affected_blocks,
        }
