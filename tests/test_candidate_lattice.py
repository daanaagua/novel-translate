import json

from src.core.v4.lexical_index import LexicalCandidateExtractor
from src.core.v4.models import V4Block


def make_block(source_text: str) -> V4Block:
    return V4Block(
        id="block-1",
        source_edition_id=1,
        chapter_id="chapter-1",
        chapter_index=0,
        block_index=0,
        global_index=0,
        block_type="prose",
        source_text=source_text,
        source_hash="source-hash",
        token_count=len(source_text.split()),
        status="pending",
    )


def test_extract_builds_reversible_overlapping_candidate_lattice():
    source_text = "Drotte and Roche waited beside the Corpse Door."
    block = make_block(source_text)

    candidates = LexicalCandidateExtractor([block], max_candidates=80).extract(block)
    by_text = {candidate.original_text: candidate for candidate in candidates}

    required = {"Drotte", "Roche", "Drotte and Roche", "Corpse", "Corpse Door"}
    assert required <= by_text.keys()

    locations = [
        (candidate.paragraph_id, candidate.start_offset, candidate.end_offset)
        for candidate in candidates
    ]
    assert len(locations) == len(set(locations))
    for candidate in candidates:
        assert source_text[candidate.start_offset:candidate.end_offset] == candidate.original_text

    assert "coordination" in by_text["Drotte and Roche"].risk_flags
    for original_text in required:
        assert "span_competition" in by_text[original_text].risk_flags


def test_risk_flags_are_serialized_as_json_storage_value():
    block = make_block("Drotte and Roche waited.")

    candidates = LexicalCandidateExtractor([block], max_candidates=80).extract(block)
    coordinated = next(
        candidate for candidate in candidates if candidate.original_text == "Drotte and Roche"
    )
    payload = coordinated.storage_payload()

    assert json.loads(payload["risk_flags_json"]) == list(coordinated.risk_flags)


def test_coordination_span_supports_general_coordinators():
    block = make_block("Drotte or Roche waited.")

    candidates = LexicalCandidateExtractor([block], max_candidates=80).extract(block)
    coordinated = next(
        candidate for candidate in candidates if candidate.original_text == "Drotte or Roche"
    )

    assert "coordination" in coordinated.risk_flags


def test_capitalized_function_word_stops_phrase_expansion():
    block = make_block("Drotte I waited. He left.")

    candidates = LexicalCandidateExtractor([block], max_candidates=80).extract(block)

    assert "Drotte I" not in {candidate.original_text for candidate in candidates}


def test_extract_respects_max_candidates_for_overlapping_spans():
    block = make_block("Drotte and Roche waited beside the Corpse Door.")

    candidates = LexicalCandidateExtractor([block], max_candidates=3).extract(block)

    assert len(candidates) == 3


def test_competition_marking_only_receives_capped_candidate_set():
    names = [
        f"Name{chr(ord('A') + index // 26)}{chr(ord('A') + index % 26)}"
        for index in range(120)
    ]
    block = make_block(" ".join(f"{name}." for name in names))
    marked_lengths = []

    class RecordingExtractor(LexicalCandidateExtractor):
        def _mark_candidate_risks(self, candidates):
            marked_lengths.append(len(candidates))
            return super()._mark_candidate_risks(candidates)

    candidates = RecordingExtractor([block], max_candidates=80).extract(block)

    assert len(candidates) == 80
    assert marked_lengths == [80]
