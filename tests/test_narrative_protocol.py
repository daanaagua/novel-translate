from __future__ import annotations

import json

import pytest

from src.core.v4.models import ContextPacket, V4Block


def _block(source: str = "The woman entered; she sat.") -> V4Block:
    return V4Block(
        id="block-1",
        source_edition_id=1,
        chapter_id="ch01",
        chapter_index=1,
        block_index=0,
        global_index=0,
        block_type="prose",
        source_text=source,
        source_hash="source-hash",
        token_count=8,
        status="ready",
        legacy_id="v01_ch01_000",
    )


def _valid_payload() -> dict:
    return {
        "semantic_relations": [
            {
                "relation_type": "referential_link",
                "inference_strength": "explicit",
                "source_spans": ["The woman", "she"],
                "related_memory_ids": ["R1"],
                "translation_constraint": "保留两处指向同一人物的关系。",
            }
        ],
        "memory_candidates": [
            {
                "candidate_id": "M1",
                "memory_type": "explicit_fact",
                "statement": "A woman entered.",
                "truth_status": "asserted",
                "visibility": "reader_visible",
                "confidence": 0.9,
                "subjects": [
                    {
                        "subject_type": "concept",
                        "subject_id": "C1",
                        "role": "actor",
                    }
                ],
                "related_memory_ids": ["R1"],
                "evidence_spans": ["The woman entered"],
                "state_operation": "append",
                "high_impact": False,
            }
        ],
        "discourse_delta": {
            "active_speakers": ["C1"],
            "viewpoint_holder": "C1",
            "state_confidence": 0.8,
        },
    }


def test_memory_candidate_rejects_unknown_type():
    from src.core.v4.narrative_models import NarrativeMemoryCandidate

    with pytest.raises(ValueError, match="memory_type"):
        NarrativeMemoryCandidate(
            candidate_id="M1",
            memory_type="plot_answer",
            statement="x",
            truth_status="asserted",
            visibility="reader_visible",
            confidence=0.9,
            evidence_spans=("x",),
        )


def test_context_packet_records_dual_versions_and_dependencies():
    packet = ContextPacket(
        block_id="b1",
        knowledge_version=4,
        memory_version=7,
        snapshot_id="snapshot-7",
        rendered="context",
        required_chars=7,
        matched_lexeme_ids=["L1"],
        matched_memory_ids=["M1"],
        context_hash="context-hash",
    )

    assert packet.memory_version == 7
    assert packet.snapshot_id == "snapshot-7"
    assert packet.matched_lexeme_ids == ["L1"]
    assert packet.matched_memory_ids == ["M1"]


def test_premap_validation_keeps_valid_sections_when_memory_is_ungrounded():
    from src.core.v4.narrative_protocol import validate_premap_payload

    payload = _valid_payload()
    payload["memory_candidates"][0]["evidence_spans"] = ["not in source"]

    result = validate_premap_payload(
        payload,
        "The woman entered; she sat.",
        allowed_subject_ids={"C1"},
        allowed_memory_ids={"R1"},
    )

    assert len(result.semantic_relations) == 1
    assert result.memory_candidates == ()
    assert result.discourse_delta.viewpoint_holder == "C1"
    assert result.degraded is True
    assert any("memory_candidates" in warning for warning in result.validation_warnings)


def test_premap_validation_rejects_ids_not_present_in_request():
    from src.core.v4.narrative_protocol import validate_premap_payload

    payload = _valid_payload()
    payload["memory_candidates"][0]["subjects"][0]["subject_id"] = "C-future"

    result = validate_premap_payload(
        payload,
        "The woman entered; she sat.",
        allowed_subject_ids={"C1"},
        allowed_memory_ids={"R1"},
    )

    assert result.memory_candidates == ()
    assert result.semantic_relations


def test_premapper_degrades_after_invalid_json_without_losing_prior_state():
    from src.core.v4.narrative_models import DiscourseState
    from src.core.v4.narrative_protocol import NarrativePremapper, PremapperConfig

    class InvalidLLM:
        def __init__(self):
            self.calls = 0

        def chat(self, **_kwargs):
            self.calls += 1
            return "{invalid"

    llm = InvalidLLM()
    prior = DiscourseState(
        viewpoint_holder="C1",
        active_speakers=("C1",),
        state_confidence=0.7,
    )
    premapper = NarrativePremapper(
        llm,
        PremapperConfig(max_attempts=2, max_tokens=1000),
    )

    result = premapper.map(
        block=_block(),
        structure={},
        prior_snapshot={"visible_memories": []},
        discourse_state=prior,
        provisional_subjects=[{"id": "C1", "label": "the woman"}],
    )

    assert llm.calls == 2
    assert result.degraded is True
    assert result.semantic_relations == ()
    assert result.memory_candidates == ()
    assert result.discourse_delta.viewpoint_holder == "C1"


def test_premapper_accepts_strict_json_response():
    from src.core.v4.narrative_models import DiscourseState
    from src.core.v4.narrative_protocol import NarrativePremapper

    class ValidLLM:
        def chat(self, **_kwargs):
            return json.dumps(_valid_payload(), ensure_ascii=False)

    result = NarrativePremapper(ValidLLM()).map(
        block=_block(),
        structure={},
        prior_snapshot={"visible_memories": [{"id": "R1"}]},
        discourse_state=DiscourseState(),
        provisional_subjects=[{"id": "C1", "label": "the woman"}],
    )

    assert result.degraded is False
    assert result.semantic_relations[0].relation_type == "referential_link"
    assert result.memory_candidates[0].candidate_id == "M1"


def test_discourse_delta_preserves_omitted_fields_and_can_clear_speakers():
    from src.core.v4.narrative_models import (
        DiscourseDelta,
        DiscourseState,
        apply_discourse_delta,
    )

    prior = DiscourseState(
        viewpoint_holder="C1",
        active_speakers=("C1",),
        scene_location="L1",
        state_confidence=0.8,
    )
    delta = DiscourseDelta(
        active_speakers=(),
        scene_location="L2",
        state_confidence=0.9,
    )

    merged = apply_discourse_delta(prior, delta)

    assert merged.viewpoint_holder == "C1"
    assert merged.active_speakers == ()
    assert merged.scene_location == "L2"
    assert merged.state_confidence == 0.9
