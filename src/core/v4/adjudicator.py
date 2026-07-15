"""Conservative model adjudication for bounded lexical candidate clusters."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Dict, Mapping, Sequence

from .candidate_clusters import CandidateCluster, CandidateClusterBatch
from .lexical_index import COORDINATORS, LexicalCandidate, lexical_key
from .models import AdjudicationDecision, AdjudicationResponse


ADJUDICATION_SYSTEM = """You adjudicate bounded lexical candidate spans.
Return one decision for every cluster as strict JSON: {"decisions":[...]}.
Use only the local Kxx and KxxA aliases in this request. Never invent a span.
promote selects exactly one existing span; reject/defer select none; split selects
two or more non-overlapping existing spans; supersede selects one existing span
that contains the span it replaces. If the correct span is absent, return defer
with reason "missing_span". Prefer conservative entity types when evidence is weak.
"""


@dataclass(frozen=True)
class AdjudicationResult:
    """A validated decision resolved back to stable local candidate identifiers."""

    cluster_id: str
    verdict: str
    selected_candidate_ids: tuple[str, ...]
    entity_kind: str
    confidence: float
    reason: str = ""
    rounds: int = 1


@dataclass(frozen=True)
class _RoundBatch:
    clusters: tuple[CandidateCluster, ...]
    cluster_by_alias: Mapping[str, CandidateCluster]
    candidate_by_alias: Mapping[str, LexicalCandidate]
    aliases_by_cluster: Mapping[str, frozenset[str]]
    payload: Mapping[str, object]


class AdjudicationProtocolError(ValueError):
    """The model response cannot be resolved safely to the supplied lattice."""


class V4Adjudicator:
    """Run strict local adjudication and independent re-aliased risk review."""

    def __init__(
        self,
        llm: Any,
        max_attempts: int = 2,
        max_tokens: int = 8192,
    ):
        self.llm = llm
        self.max_attempts = max(1, int(max_attempts))
        self.max_tokens = max(1, int(max_tokens))

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

    @staticmethod
    def _clusters_from_subject(
        subject: CandidateClusterBatch | CandidateCluster | Sequence[CandidateCluster],
    ) -> tuple[CandidateCluster, ...]:
        if isinstance(subject, CandidateClusterBatch):
            clusters = subject.clusters
        elif isinstance(subject, CandidateCluster):
            clusters = (subject,)
        else:
            clusters = tuple(subject)
        return tuple(sorted(clusters, key=lambda cluster: cluster.id))

    @classmethod
    def _round_batch(
        cls,
        clusters: Sequence[CandidateCluster],
        *,
        reverse_alternatives: bool,
    ) -> _RoundBatch:
        ordered = tuple(sorted(clusters, key=lambda cluster: cluster.id))
        if not ordered or len(ordered) > 12:
            raise ValueError("an adjudication batch must contain 1..12 clusters")

        cluster_by_alias: Dict[str, CandidateCluster] = {}
        candidate_by_alias: Dict[str, LexicalCandidate] = {}
        aliases_by_cluster: Dict[str, frozenset[str]] = {}
        rendered_clusters = []
        for cluster_index, cluster in enumerate(ordered, start=1):
            if not cluster.alternatives or len(cluster.alternatives) > 4:
                raise ValueError("each cluster must contain 1..4 alternatives")
            cluster_alias = f"K{cluster_index:02d}"
            cluster_by_alias[cluster_alias] = cluster
            alternatives = tuple(cluster.alternatives)
            if reverse_alternatives:
                alternatives = tuple(reversed(alternatives))
            rendered_alternatives = []
            local_aliases = []
            for alternative_index, alternative in enumerate(alternatives):
                alias = f"{cluster_alias}{chr(ord('A') + alternative_index)}"
                candidate_by_alias[alias] = alternative
                local_aliases.append(alias)
                rendered_alternatives.append(
                    {
                        "id": alias,
                        "text": alternative.original_text,
                        "risk_flags": list(alternative.risk_flags),
                    }
                )
            aliases_by_cluster[cluster.id] = frozenset(local_aliases)
            rendered_clusters.append(
                {
                    "cluster_id": cluster_alias,
                    "alternatives": rendered_alternatives,
                    "contexts": [
                        {
                            "text": context.text,
                            "risk_flags": list(context.risk_flags),
                        }
                        for context in cluster.contexts
                    ],
                    "risk_flags": list(cluster.risk_flags),
                    "affected_blocks": cluster.affected_blocks,
                }
            )
        return _RoundBatch(
            clusters=ordered,
            cluster_by_alias=cluster_by_alias,
            candidate_by_alias=candidate_by_alias,
            aliases_by_cluster=aliases_by_cluster,
            payload={"clusters": rendered_clusters},
        )

    @staticmethod
    def _source_spans_are_valid(
        clusters: Sequence[CandidateCluster], source_texts: Mapping[str, str]
    ) -> bool:
        for cluster in clusters:
            for candidate in cluster.alternatives:
                source = source_texts.get(candidate.block_id)
                if source is None:
                    return False
                if not (0 <= candidate.start_offset < candidate.end_offset <= len(source)):
                    return False
                if source[candidate.start_offset : candidate.end_offset] != candidate.original_text:
                    return False
        return True

    @staticmethod
    def _overlap(left: LexicalCandidate, right: LexicalCandidate) -> bool:
        return (
            left.block_id == right.block_id
            and left.paragraph_id == right.paragraph_id
            and left.start_offset < right.end_offset
            and right.start_offset < left.end_offset
        )

    @classmethod
    def _valid_supersede(
        cls,
        selected: LexicalCandidate,
        alternatives: Sequence[LexicalCandidate],
    ) -> bool:
        contains_replaced_span = False
        for other in alternatives:
            if other.id == selected.id or not cls._overlap(selected, other):
                continue
            contains = (
                selected.start_offset <= other.start_offset
                and other.end_offset <= selected.end_offset
            )
            if not contains:
                return False
            if (
                selected.start_offset < other.start_offset
                or other.end_offset < selected.end_offset
            ):
                contains_replaced_span = True
        return contains_replaced_span

    @classmethod
    def _resolve_decisions(
        cls,
        parsed: AdjudicationResponse,
        round_batch: _RoundBatch,
    ) -> Dict[str, AdjudicationResult]:
        decisions_by_alias: Dict[str, AdjudicationDecision] = {}
        for decision in parsed.decisions:
            if decision.cluster_id in decisions_by_alias:
                raise AdjudicationProtocolError(
                    f"duplicate cluster decision: {decision.cluster_id}"
                )
            decisions_by_alias[decision.cluster_id] = decision
        if set(decisions_by_alias) != set(round_batch.cluster_by_alias):
            raise AdjudicationProtocolError("missing or unknown cluster decision")

        resolved: Dict[str, AdjudicationResult] = {}
        for cluster_alias, cluster in round_batch.cluster_by_alias.items():
            decision = decisions_by_alias[cluster_alias]
            if len(decision.selected_ids) != len(set(decision.selected_ids)):
                raise AdjudicationProtocolError("duplicate selected alias")
            allowed_aliases = round_batch.aliases_by_cluster[cluster.id]
            if any(alias not in allowed_aliases for alias in decision.selected_ids):
                raise AdjudicationProtocolError("unknown or cross-cluster selected alias")
            selected = tuple(
                round_batch.candidate_by_alias[alias] for alias in decision.selected_ids
            )

            if decision.reason == "missing_span" and decision.verdict != "defer":
                raise AdjudicationProtocolError("missing_span requires defer")
            if decision.verdict == "promote" and len(selected) != 1:
                raise AdjudicationProtocolError("promote requires exactly one span")
            if decision.verdict in {"reject", "defer"} and selected:
                raise AdjudicationProtocolError(
                    f"{decision.verdict} cannot select spans"
                )
            if decision.verdict == "split":
                if len(selected) < 2:
                    raise AdjudicationProtocolError("split requires at least two spans")
                if any(
                    cls._overlap(left, right)
                    for index, left in enumerate(selected)
                    for right in selected[index + 1 :]
                ):
                    raise AdjudicationProtocolError("split spans overlap")
            if decision.verdict == "supersede":
                if len(selected) != 1 or not cls._valid_supersede(
                    selected[0], cluster.alternatives
                ):
                    raise AdjudicationProtocolError(
                        "supersede span does not contain a replaced span"
                    )

            resolved[cluster.id] = AdjudicationResult(
                cluster_id=cluster.id,
                verdict=decision.verdict,
                selected_candidate_ids=tuple(sorted(item.id for item in selected)),
                entity_kind=decision.entity_kind,
                confidence=decision.confidence,
                reason=decision.reason,
            )
        return resolved

    def _call_round(
        self,
        round_batch: _RoundBatch,
    ) -> Dict[str, AdjudicationResult] | None:
        base_messages = [
            {"role": "system", "content": ADJUDICATION_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(round_batch.payload, ensure_ascii=False),
            },
        ]
        last_error = ""
        for _attempt in range(1, self.max_attempts + 1):
            messages = list(base_messages)
            if last_error:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "The previous JSON failed strict local validation: "
                            f"{last_error[:500]}. Return the complete JSON again."
                        ),
                    }
                )
            try:
                raw = self.llm.chat(
                    messages=messages,
                    purpose="candidate_adjudication",
                    temperature=0.0,
                    max_tokens=self.max_tokens,
                    json_mode=True,
                    stream=False,
                )
                parsed = AdjudicationResponse.model_validate_json(
                    self._clean_json(raw)
                )
                return self._resolve_decisions(parsed, round_batch)
            except Exception as exc:
                last_error = str(exc)
        return None

    @staticmethod
    def _needs_independent_round(
        cluster: CandidateCluster,
        decision: AdjudicationResult,
    ) -> bool:
        flags = set(cluster.risk_flags)
        for alternative in cluster.alternatives:
            flags.update(alternative.risk_flags)
        if {"coordination", "span_competition"} & flags:
            return True
        if any("type_conflict" in flag for flag in flags):
            return True
        if cluster.affected_blocks >= 3:
            return True
        if decision.confidence < 0.90:
            return True
        if decision.verdict in {"split", "supersede"}:
            return True
        if decision.entity_kind == "person":
            selected_ids = set(decision.selected_candidate_ids)
            for alternative in cluster.alternatives:
                if alternative.id not in selected_ids:
                    continue
                words = lexical_key(alternative.original_text).split()
                if any(word in COORDINATORS for word in words[1:-1]):
                    return True
        return False

    @staticmethod
    def _semantically_equal(
        first: AdjudicationResult,
        second: AdjudicationResult,
    ) -> bool:
        return (
            first.verdict,
            first.selected_candidate_ids,
            first.entity_kind,
        ) == (
            second.verdict,
            second.selected_candidate_ids,
            second.entity_kind,
        )

    @staticmethod
    def _deferred(
        cluster: CandidateCluster,
        reason: str,
        *,
        rounds: int,
    ) -> AdjudicationResult:
        return AdjudicationResult(
            cluster_id=cluster.id,
            verdict="defer",
            selected_candidate_ids=(),
            entity_kind="unknown_named_entity",
            confidence=0.0,
            reason=reason,
            rounds=rounds,
        )

    def _adjudicate_one_batch(
        self,
        clusters: Sequence[CandidateCluster],
        source_texts: Mapping[str, str],
    ) -> tuple[AdjudicationResult, ...]:
        ordered = tuple(sorted(clusters, key=lambda cluster: cluster.id))
        if not self._source_spans_are_valid(ordered, source_texts):
            return tuple(
                self._deferred(cluster, "model_protocol_failure", rounds=0)
                for cluster in ordered
            )

        first_round = self._round_batch(ordered, reverse_alternatives=False)
        first = self._call_round(first_round)
        if first is None:
            return tuple(
                self._deferred(cluster, "model_protocol_failure", rounds=1)
                for cluster in ordered
            )

        risky = tuple(
            cluster
            for cluster in ordered
            if self._needs_independent_round(cluster, first[cluster.id])
        )
        if not risky:
            return tuple(first[cluster.id] for cluster in ordered)

        second_round = self._round_batch(risky, reverse_alternatives=True)
        second = self._call_round(second_round)
        final = dict(first)
        if second is None:
            for cluster in risky:
                final[cluster.id] = self._deferred(
                    cluster, "model_protocol_failure", rounds=2
                )
        else:
            for cluster in risky:
                first_decision = first[cluster.id]
                second_decision = second[cluster.id]
                if not self._semantically_equal(first_decision, second_decision):
                    final[cluster.id] = self._deferred(
                        cluster, "independent_verdict_conflict", rounds=2
                    )
                else:
                    final[cluster.id] = replace(
                        first_decision,
                        confidence=min(
                            first_decision.confidence, second_decision.confidence
                        ),
                        rounds=2,
                    )
        return tuple(final[cluster.id] for cluster in ordered)

    def adjudicate(
        self,
        subject: CandidateClusterBatch | CandidateCluster | Sequence[CandidateCluster],
        source_texts: Mapping[str, str],
    ) -> tuple[AdjudicationResult, ...]:
        """Adjudicate one batch or deterministically partition a cluster sequence."""

        clusters = self._clusters_from_subject(subject)
        if not clusters:
            return ()
        results = []
        for start in range(0, len(clusters), 12):
            results.extend(
                self._adjudicate_one_batch(clusters[start : start + 12], source_texts)
            )
        return tuple(results)
