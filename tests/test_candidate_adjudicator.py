import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from src.core.v4.adjudicator import V4Adjudicator
from src.core.v4.candidate_clusters import (
    CandidateCluster,
    CandidateClusterBuilder,
    CandidateContext,
)
from src.core.v4.lexical_index import LexicalCandidate
from src.core.v4.models import AdjudicationDecision, AdjudicationResponse


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response)


def candidate(
    candidate_id,
    source,
    start,
    end,
    *,
    block_id="block-1",
    risk_flags=(),
):
    return LexicalCandidate(
        id=candidate_id,
        block_id=block_id,
        paragraph_id="P000",
        start_offset=start,
        end_offset=end,
        original_text=source[start:end],
        normalized_text=source[start:end],
        left_context="",
        right_context="",
        extraction_reason="test",
        book_frequency=1,
        score=1,
        risk_flags=tuple(risk_flags),
    )


def cluster(cluster_id, alternatives, *, risk_flags=(), affected_blocks=1):
    first = alternatives[0]
    return CandidateCluster(
        id=cluster_id,
        alternatives=tuple(alternatives),
        contexts=(
            CandidateContext(
                candidate_id=first.id,
                block_id=first.block_id,
                paragraph_id=first.paragraph_id,
                original_text=first.original_text,
                left_context=first.left_context,
                right_context=first.right_context,
                risk_flags=first.risk_flags,
            ),
        ),
        risk_flags=tuple(risk_flags),
        affected_blocks=affected_blocks,
    )


def batch(*clusters):
    return CandidateClusterBuilder().batch(clusters)[0]


def response(cluster_id="K01", verdict="promote", selected_ids=None, **overrides):
    decision = {
        "cluster_id": cluster_id,
        "verdict": verdict,
        "selected_ids": [f"{cluster_id}A"] if selected_ids is None else selected_ids,
        "entity_kind": "person",
        "confidence": 0.96,
    }
    decision.update(overrides)
    return {"decisions": [decision]}


def user_payload(request):
    return json.loads(request["messages"][1]["content"])


def test_strict_response_contract_rejects_extra_fields_and_illegal_values():
    with pytest.raises(ValidationError):
        AdjudicationDecision.model_validate(
            {
                "cluster_id": "cluster-long-id",
                "verdict": "promote",
                "selected_ids": ["K01A"],
                "entity_kind": "character",
                "confidence": 2,
                "extra": True,
            }
        )
    with pytest.raises(ValidationError):
        AdjudicationResponse.model_validate(
            {
                "decisions": [
                    {
                        "cluster_id": "K01",
                        "verdict": "promote",
                        "selected_ids": ["K01A"] * 5,
                        "entity_kind": "person",
                        "confidence": 0.9,
                    }
                ]
            }
        )


def test_unknown_alias_never_promotes_after_protocol_failure():
    source = "Alpha waited."
    item = cluster("stable-cluster", [candidate("candidate-long-id", source, 0, 5)])
    llm = FakeLLM([response(selected_ids=["K01D"])])

    result = V4Adjudicator(llm, max_attempts=1).adjudicate(
        batch(item), {"block-1": source}
    )

    assert result[0].verdict == "defer"
    assert result[0].reason == "model_protocol_failure"
    payload = user_payload(llm.requests[0])
    rendered = json.dumps(payload)
    assert "stable-cluster" not in rendered
    assert "candidate-long-id" not in rendered


def test_cross_cluster_alias_invalidates_the_whole_batch():
    sources = {"block-1": "Alpha waited.", "block-2": "Beta waited."}
    clusters = (
        cluster("cluster-a", [candidate("candidate-a", sources["block-1"], 0, 5)]),
        cluster(
            "cluster-b",
            [candidate("candidate-b", sources["block-2"], 0, 4, block_id="block-2")],
        ),
    )
    llm = FakeLLM(
        [
            {
                "decisions": [
                    response("K01", selected_ids=["K02A"])["decisions"][0],
                    response("K02")["decisions"][0],
                ]
            }
        ]
    )

    results = V4Adjudicator(llm, max_attempts=1).adjudicate(batch(*clusters), sources)

    assert {result.verdict for result in results} == {"defer"}
    assert {result.reason for result in results} == {"model_protocol_failure"}


@pytest.mark.parametrize(
    "bad_response",
    [
        response(selected_ids=["K01A", "K01A"]),
        {"decisions": []},
        {"decisions": [response()["decisions"][0], response()["decisions"][0]]},
    ],
)
def test_duplicate_alias_or_missing_or_duplicate_cluster_decision_is_rejected(bad_response):
    source = "Alpha waited."
    item = cluster("cluster-a", [candidate("candidate-a", source, 0, 5)])

    result = V4Adjudicator(FakeLLM([bad_response]), max_attempts=1).adjudicate(
        batch(item), {"block-1": source}
    )

    assert result[0].verdict == "defer"
    assert result[0].reason == "model_protocol_failure"


def test_split_rejects_overlapping_source_spans():
    source = "Alpha Beta waited."
    whole = candidate("candidate-whole", source, 0, 10)
    alpha = candidate("candidate-alpha", source, 0, 5)
    item = cluster("cluster-a", [whole, alpha], risk_flags=("span_competition",))
    llm = FakeLLM(
        [response(verdict="split", selected_ids=["K01A", "K01B"])]
    )

    result = V4Adjudicator(llm, max_attempts=1).adjudicate(
        batch(item), {"block-1": source}
    )

    assert result[0].verdict == "defer"
    assert result[0].reason == "model_protocol_failure"


def test_supersede_requires_selected_existing_span_to_contain_replaced_span():
    source = "Alpha Beta waited."
    short = candidate("candidate-short", source, 0, 5)
    long = candidate("candidate-long", source, 0, 10)
    item = cluster("cluster-a", [short, long], risk_flags=("span_competition",))
    llm = FakeLLM(
        [response(verdict="supersede", selected_ids=["K01A"])]
    )

    result = V4Adjudicator(llm, max_attempts=1).adjudicate(
        batch(item), {"block-1": source}
    )

    assert result[0].verdict == "defer"
    assert result[0].reason == "model_protocol_failure"


def test_legal_split_and_supersede_survive_realiased_independent_review():
    split_source = "Alpha and Beta waited."
    alpha = candidate("candidate-alpha", split_source, 0, 5)
    beta = candidate("candidate-beta", split_source, 10, 14)
    split_cluster = cluster(
        "cluster-split", [alpha, beta], risk_flags=("span_competition",)
    )
    split_llm = FakeLLM(
        [
            response(verdict="split", selected_ids=["K01A", "K01B"]),
            response(verdict="split", selected_ids=["K01A", "K01B"]),
        ]
    )

    split_result = V4Adjudicator(split_llm).adjudicate(
        batch(split_cluster), {"block-1": split_source}
    )

    supersede_source = "Alpha Beta waited."
    short = candidate("candidate-short", supersede_source, 0, 5)
    long = candidate("candidate-long", supersede_source, 0, 10)
    supersede_cluster = cluster(
        "cluster-supersede", [short, long], risk_flags=("span_competition",)
    )
    supersede_llm = FakeLLM(
        [
            response(verdict="supersede", selected_ids=["K01B"]),
            response(verdict="supersede", selected_ids=["K01A"]),
        ]
    )

    supersede_result = V4Adjudicator(supersede_llm).adjudicate(
        batch(supersede_cluster), {"block-1": supersede_source}
    )

    assert split_result[0].verdict == "split"
    assert set(split_result[0].selected_candidate_ids) == {
        "candidate-alpha",
        "candidate-beta",
    }
    assert supersede_result[0].verdict == "supersede"
    assert supersede_result[0].selected_candidate_ids == ("candidate-long",)


def test_supersede_can_ignore_a_different_partially_overlapping_alternative():
    source = "Alpha Beta Gamma waited."
    selected = candidate("candidate-selected", source, 0, 10)
    contained = candidate("candidate-contained", source, 0, 5)
    partial = candidate("candidate-partial", source, 6, 16)
    item = cluster(
        "cluster-a",
        [selected, contained, partial],
        risk_flags=("span_competition",),
    )
    llm = FakeLLM(
        [
            response(verdict="supersede", selected_ids=["K01A"]),
            response(verdict="supersede", selected_ids=["K01C"]),
        ]
    )

    result = V4Adjudicator(llm).adjudicate(batch(item), {"block-1": source})

    assert result[0].verdict == "supersede"
    assert result[0].selected_candidate_ids == ("candidate-selected",)


@pytest.mark.parametrize("verdict", ["reject", "defer"])
def test_reject_and_defer_cannot_carry_selected_spans(verdict):
    source = "Alpha waited."
    item = cluster("cluster-a", [candidate("candidate-a", source, 0, 5)])

    result = V4Adjudicator(
        FakeLLM([response(verdict=verdict, selected_ids=[])]), max_attempts=1
    ).adjudicate(batch(item), {"block-1": source})
    invalid = V4Adjudicator(
        FakeLLM([response(verdict=verdict, selected_ids=["K01A"])]), max_attempts=1
    ).adjudicate(batch(item), {"block-1": source})

    assert result[0].verdict == verdict
    assert invalid[0].reason == "model_protocol_failure"


def test_missing_span_is_only_accepted_as_an_explicit_defer_reason():
    source = "Alpha waited."
    item = cluster("cluster-a", [candidate("candidate-a", source, 0, 5)])
    invalid = response(reason="missing_span")
    valid = response(
        verdict="defer",
        selected_ids=[],
        entity_kind="unknown_named_entity",
        reason="missing_span",
    )

    invalid_result = V4Adjudicator(FakeLLM([invalid]), max_attempts=1).adjudicate(
        batch(item), {"block-1": source}
    )
    valid_result = V4Adjudicator(FakeLLM([valid]), max_attempts=1).adjudicate(
        batch(item), {"block-1": source}
    )

    assert invalid_result[0].reason == "model_protocol_failure"
    assert valid_result[0].verdict == "defer"
    assert valid_result[0].reason == "missing_span"


def test_bad_candidate_source_offsets_never_reach_promotion():
    source = "Alpha waited."
    bad = replace(candidate("candidate-a", source, 0, 5), start_offset=1, end_offset=6)
    item = cluster("cluster-a", [bad])
    llm = FakeLLM([response()])

    result = V4Adjudicator(llm, max_attempts=1).adjudicate(
        batch(item), {"block-1": source}
    )

    assert result[0].verdict == "defer"
    assert result[0].reason == "model_protocol_failure"


def test_low_risk_legal_decision_is_accepted_without_a_second_round():
    source = "Alpha waited."
    item = cluster("cluster-a", [candidate("candidate-a", source, 0, 5)])
    llm = FakeLLM([response()])

    result = V4Adjudicator(llm).adjudicate(batch(item), {"block-1": source})

    assert result[0].verdict == "promote"
    assert result[0].selected_candidate_ids == ("candidate-a",)
    assert result[0].rounds == 1
    assert len(llm.requests) == 1


def test_independent_promote_vs_split_conflict_is_deferred():
    source = "Alpha and Beta waited."
    whole = candidate("candidate-whole", source, 0, 14, risk_flags=("coordination",))
    alpha = candidate("candidate-alpha", source, 0, 5)
    beta = candidate("candidate-beta", source, 10, 14)
    item = cluster(
        "cluster-a",
        [whole, alpha, beta],
        risk_flags=("coordination", "span_competition"),
    )
    llm = FakeLLM(
        [
            response(selected_ids=["K01A"]),
            response(verdict="split", selected_ids=["K01A", "K01B"]),
        ]
    )

    result = V4Adjudicator(llm).adjudicate(batch(item), {"block-1": source})

    assert result[0].verdict == "defer"
    assert result[0].reason == "independent_verdict_conflict"
    assert result[0].rounds == 2


def test_second_round_reverses_alternatives_and_regenerates_alias_mapping():
    source = "Alpha and Beta waited."
    whole = candidate("candidate-whole", source, 0, 14)
    alpha = candidate("candidate-alpha", source, 0, 5)
    beta = candidate("candidate-beta", source, 10, 14)
    item = cluster("cluster-a", [whole, alpha, beta], risk_flags=("coordination",))
    llm = FakeLLM(
        [
            response(selected_ids=["K01A"]),
            response(selected_ids=["K01C"]),
        ]
    )

    result = V4Adjudicator(llm).adjudicate(batch(item), {"block-1": source})

    first, second = map(user_payload, llm.requests)
    first_aliases = {
        alternative["id"]: alternative["text"]
        for alternative in first["clusters"][0]["alternatives"]
    }
    second_aliases = {
        alternative["id"]: alternative["text"]
        for alternative in second["clusters"][0]["alternatives"]
    }
    assert first_aliases["K01A"] == "Alpha and Beta"
    assert second_aliases["K01A"] == "Beta"
    assert second_aliases["K01C"] == "Alpha and Beta"
    assert result[0].verdict == "promote"
    assert result[0].selected_candidate_ids == ("candidate-whole",)
    assert result[0].rounds == 2


def test_single_alternative_risk_review_uses_a_different_alias_mapping():
    source = "Alpha waited."
    item = cluster(
        "cluster-a",
        [candidate("candidate-alpha", source, 0, 5)],
        affected_blocks=3,
    )
    llm = FakeLLM(
        [
            response(selected_ids=["K01A"]),
            response(selected_ids=["K01D"]),
        ]
    )

    result = V4Adjudicator(llm).adjudicate(batch(item), {"block-1": source})

    first, second = map(user_payload, llm.requests)
    first_alternative = first["clusters"][0]["alternatives"][0]
    second_alternative = second["clusters"][0]["alternatives"][0]
    assert first_alternative == {"id": "K01A", "text": "Alpha", "risk_flags": []}
    assert second_alternative == {"id": "K01D", "text": "Alpha", "risk_flags": []}
    assert result[0].verdict == "promote"
    assert result[0].selected_candidate_ids == ("candidate-alpha",)
    assert result[0].rounds == 2


def test_second_round_protocol_failure_conservatively_defers_only_risky_cluster():
    sources = {"block-1": "Alpha waited.", "block-2": "Beta waited."}
    low = cluster("cluster-a", [candidate("candidate-a", sources["block-1"], 0, 5)])
    risky = cluster(
        "cluster-b",
        [candidate("candidate-b", sources["block-2"], 0, 4, block_id="block-2")],
        affected_blocks=3,
    )
    first = {
        "decisions": [
            response("K01")["decisions"][0],
            response("K02")["decisions"][0],
        ]
    }
    llm = FakeLLM([first, {"decisions": []}])

    results = V4Adjudicator(llm, max_attempts=1).adjudicate(
        batch(low, risky), sources
    )
    by_cluster = {result.cluster_id: result for result in results}

    assert by_cluster["cluster-a"].verdict == "promote"
    assert by_cluster["cluster-b"].verdict == "defer"
    assert by_cluster["cluster-b"].reason == "model_protocol_failure"


def test_invalid_first_attempt_can_retry_with_the_same_local_aliases():
    source = "Alpha waited."
    item = cluster("cluster-a", [candidate("candidate-a", source, 0, 5)])
    llm = FakeLLM([response(selected_ids=["K01D"]), response()])

    result = V4Adjudicator(llm, max_attempts=2).adjudicate(
        batch(item), {"block-1": source}
    )

    assert result[0].verdict == "promote"
    assert user_payload(llm.requests[0]) == user_payload(llm.requests[1])


def test_request_order_is_deterministic_for_reversed_cluster_input():
    sources = {"block-1": "Alpha waited.", "block-2": "Beta waited."}
    first = cluster("cluster-a", [candidate("candidate-a", sources["block-1"], 0, 5)])
    second = cluster(
        "cluster-b",
        [candidate("candidate-b", sources["block-2"], 0, 4, block_id="block-2")],
    )
    both_decisions = {
        "decisions": [
            response("K01")["decisions"][0],
            response("K02")["decisions"][0],
        ]
    }
    forward_llm = FakeLLM([both_decisions])
    reverse_llm = FakeLLM([both_decisions])

    V4Adjudicator(forward_llm).adjudicate([first, second], sources)
    V4Adjudicator(reverse_llm).adjudicate([second, first], sources)

    assert user_payload(forward_llm.requests[0]) == user_payload(reverse_llm.requests[0])
