import json
import re

from src.core.v4.candidate_clusters import CandidateClusterBuilder
from src.core.v4.lexical_index import LexicalCandidateExtractor
from src.core.v4.models import V4Block


def make_block(source_text: str, *, block_id: str = "block-1") -> V4Block:
    return V4Block(
        id=block_id,
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


def test_cluster_builder_groups_overlapping_candidate_spans_for_adjudication():
    block = make_block("Drotte and Roche waited beside the Corpse Door.")
    candidates = LexicalCandidateExtractor([block], max_candidates=80).extract(block)

    clusters = CandidateClusterBuilder(max_contexts=3, max_alternatives=4).build(candidates)
    text_sets = [set(cluster.texts) for cluster in clusters]

    assert any({"Drotte", "Roche", "Drotte and Roche"} <= texts for texts in text_sets)
    assert any({"Corpse", "Corpse Door"} <= texts for texts in text_sets)
    assert all(len(cluster.alternatives) <= 4 for cluster in clusters)
    assert all(len(cluster.contexts) <= 3 for cluster in clusters)


def test_cluster_ids_and_member_sets_are_stable_when_input_order_is_reversed():
    block = make_block("Drotte and Roche waited beside the Corpse Door.")
    candidates = LexicalCandidateExtractor([block], max_candidates=80).extract(block)
    builder = CandidateClusterBuilder(max_contexts=3, max_alternatives=4)

    forward = {cluster.id: set(cluster.texts) for cluster in builder.build(candidates)}
    reversed_input = {
        cluster.id: set(cluster.texts) for cluster in builder.build(list(reversed(candidates)))
    }

    assert forward == reversed_input


def test_cross_block_clustering_requires_an_exact_normalized_span_signature():
    blocks = [
        make_block("Drotte waited.", block_id="block-a"),
        make_block("Drotte answered.", block_id="block-b"),
        make_block("Drottes waited.", block_id="block-c"),
    ]
    extractor = LexicalCandidateExtractor(blocks, max_candidates=80)
    candidates = [candidate for block in blocks for candidate in extractor.extract(block)]

    clusters = CandidateClusterBuilder().build(candidates)
    drotte = next(cluster for cluster in clusters if "Drotte" in cluster.texts)
    drottes = next(cluster for cluster in clusters if "Drottes" in cluster.texts)

    assert drotte.id != drottes.id
    assert drotte.affected_blocks == 2
    assert drottes.affected_blocks == 1


def test_batches_cap_clusters_and_alternatives_and_use_reversible_local_aliases():
    blocks = [
        make_block(f"Zorga{chr(65 + index)} waited.", block_id=f"block-{index:02d}")
        for index in range(13)
    ]
    extractor = LexicalCandidateExtractor(blocks, max_candidates=80)
    candidates = [candidate for block in blocks for candidate in extractor.extract(block)]
    builder = CandidateClusterBuilder(max_contexts=3, max_alternatives=4)

    batches = builder.batch(builder.build(candidates))

    assert len(batches) == 2
    assert all(len(batch.clusters) <= 12 for batch in batches)
    assert all(
        len(cluster.alternatives) <= 4 and len(cluster.contexts) <= 3
        for batch in batches
        for cluster in batch.clusters
    )
    for batch in batches:
        assert len(batch.alias_map) == len(set(batch.alias_map))
        assert all(re.fullmatch(r"K(?:0[1-9]|1[0-2])[A-D]", alias) for alias in batch.alias_map)
        assert all(len(alias) <= 4 for alias in batch.alias_map)
        candidate_ids = {
            alternative.id
            for cluster in batch.clusters
            for alternative in cluster.alternatives
        }
        assert set(batch.alias_map.values()) == candidate_ids
        assert all(
            batch.candidate_id_for_alias(alias) == candidate_id
            for alias, candidate_id in batch.alias_map.items()
        )
        assert all(
            batch.alias_for_candidate(candidate_id) == alias
            for alias, candidate_id in batch.alias_map.items()
        )
