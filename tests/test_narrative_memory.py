from __future__ import annotations

import hashlib

import pytest

from src.core.v4.database import V4Database
from src.core.v4.narrative_models import (
    DiscourseState,
    NarrativeMemoryCandidate,
    NarrativePremapResult,
    NarrativeSubject,
)


def _seed_database(tmp_path, count: int = 7):
    database = V4Database(tmp_path / "book")
    edition = database.ensure_source_edition(
        "raw", "normalized", "test", "source.txt"
    )
    rows = []
    for index in range(count):
        source = (
            "The gate is locked."
            if index == 0
            else "Tecla speaks." if index == 1 else f"He waits at place {index}."
        )
        rows.append(
            {
                "id": f"block-{index}",
                "legacy_id": f"v01_ch01_{index:03d}",
                "chapter_id": "ch01",
                "chapter_title": "I",
                "chapter_index": 0,
                "block_index": index,
                "global_index": index,
                "block_type": "prose",
                "source_text": source,
                "source_hash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "token_count": len(source.split()),
                "status": "ready",
            }
        )
    database.upsert_blocks(edition, rows)
    return database, database.list_blocks()


def _fact(
    *,
    candidate_id: str = "M1",
    statement: str = "The gate is locked.",
    evidence: str = "The gate is locked",
    subjects=(),
    memory_type: str = "explicit_fact",
    related=(),
    operation: str = "append",
    visibility: str = "reader_visible",
    high_impact: bool = False,
):
    return NarrativeMemoryCandidate(
        candidate_id=candidate_id,
        memory_type=memory_type,
        statement=statement,
        truth_status="asserted",
        visibility=visibility,
        confidence=0.9,
        evidence_spans=(evidence,),
        subjects=tuple(subjects),
        related_memory_ids=tuple(related),
        state_operation=operation,
        high_impact=high_impact,
    )


def test_premap_cache_key_is_stable_and_result_is_reused(tmp_path):
    from src.core.v4.narrative_memory import NarrativeMemoryStore

    database, blocks = _seed_database(tmp_path)
    store = NarrativeMemoryStore(database)
    key = store.premap_cache_key(
        block=blocks[0],
        structure_hash="structure",
        prompt_hash="prompt",
        model_id="deepseek-v4-flash",
        parameters_hash="parameters",
        prior_snapshot_hash="before",
        provisional_subject_hash="subjects",
    )
    result = NarrativePremapResult(
        semantic_relations=(),
        memory_candidates=(_fact(),),
        discourse_delta=DiscourseState(),
    )

    first_id = store.save_premap_result(
        cache_key=key,
        block=blocks[0],
        result=result,
        model_id="deepseek-v4-flash",
        prompt_hash="prompt",
        prior_snapshot_hash="before",
        request_hash="request",
        response_hash="response",
    )
    second_id = store.save_premap_result(
        cache_key=key,
        block=blocks[0],
        result=result,
        model_id="deepseek-v4-flash",
        prompt_hash="prompt",
        prior_snapshot_hash="before",
        request_hash="request",
        response_hash="response",
    )

    assert first_id == second_id
    loaded = store.load_premap_result(key)
    assert loaded is not None
    assert loaded.memory_candidates[0].statement == "The gate is locked."
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM premap_results"
        ).fetchone()[0] == 1


def test_memory_merge_is_idempotent_and_append_only(tmp_path):
    from src.core.v4.narrative_memory import NarrativeMemoryStore

    database, blocks = _seed_database(tmp_path)
    store = NarrativeMemoryStore(database)

    first = store.merge_candidates(blocks[0], [_fact()])
    duplicate = store.merge_candidates(blocks[0], [_fact()])

    assert duplicate.memory_ids == first.memory_ids
    assert duplicate.memory_version == first.memory_version
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM narrative_memories"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_versions"
        ).fetchone()[0] == 2


def test_contradiction_candidate_preserves_both_memories_and_links_them(tmp_path):
    from src.core.v4.narrative_memory import NarrativeMemoryStore

    database, blocks = _seed_database(tmp_path)
    store = NarrativeMemoryStore(database)
    first = store.merge_candidates(blocks[0], [_fact()])
    first_id = first.memory_ids[0]

    second = store.merge_candidates(
        blocks[0],
        [
            _fact(
                candidate_id="M2",
                statement="The description conflicts with the locked gate.",
                memory_type="contradiction",
                related=(first_id,),
            )
        ],
    )

    assert second.memory_ids[0] != first_id
    assert store.links_for_memory(second.memory_ids[0]) == [
        {
            "to_memory_id": first_id,
            "relation": "contradicts",
        }
    ]


def test_snapshot_excludes_future_and_system_private_memories(tmp_path):
    from src.core.v4.narrative_memory import NarrativeMemoryStore

    database, blocks = _seed_database(tmp_path)
    store = NarrativeMemoryStore(database)
    past = store.merge_candidates(blocks[0], [_fact()]).memory_ids[0]
    private = store.merge_candidates(
        blocks[1],
        [
            _fact(
                candidate_id="M-private",
                statement="Private hypothesis.",
                evidence="Tecla speaks",
                memory_type="hypothesis",
                visibility="system_private",
            )
        ],
    ).memory_ids[0]
    future = store.merge_candidates(
        blocks[6],
        [
            _fact(
                candidate_id="M-future",
                statement="A future location is known.",
                evidence="He waits at place 6",
                memory_type="location_state",
            )
        ],
    ).memory_ids[0]

    snapshot = store.build_snapshot(
        blocks[3],
        knowledge_version=database.current_knowledge_version(),
        discourse_state=DiscourseState(),
    )

    assert past in snapshot.visible_memory_ids
    assert private not in snapshot.visible_memory_ids
    assert future not in snapshot.visible_memory_ids


def test_retrieval_uses_active_speaker_without_keyword_match(tmp_path):
    from src.core.v4.narrative_memory import NarrativeMemoryStore

    database, blocks = _seed_database(tmp_path)
    store = NarrativeMemoryStore(database)
    tecla = NarrativeSubject("concept", "concept-tecla", "character")
    memory_id = store.merge_candidates(
        blocks[1],
        [
            _fact(
                candidate_id="M-tecla",
                statement="Tecla is speaking.",
                evidence="Tecla speaks",
                memory_type="character_state",
                subjects=(tecla,),
            )
        ],
    ).memory_ids[0]
    snapshot = store.build_snapshot(
        blocks[3],
        knowledge_version=database.current_knowledge_version(),
        discourse_state=DiscourseState(
            active_speakers=("concept-tecla",),
            state_confidence=0.8,
        ),
    )

    retrieval = store.retrieve_for_block(
        blocks[3],
        snapshot,
        matched_subject_ids=(),
        max_chars=4_000,
    )

    assert memory_id in {memory.id for memory in retrieval.memories}


def test_required_memory_over_budget_returns_manual_boundary(tmp_path):
    from src.core.v4.narrative_memory import (
        NarrativeContextOverflow,
        NarrativeMemoryStore,
    )

    database, blocks = _seed_database(tmp_path)
    store = NarrativeMemoryStore(database)
    subject = NarrativeSubject("concept", "concept-tecla", "character")
    store.merge_candidates(
        blocks[1],
        [
            _fact(
                candidate_id="M-required",
                statement="X" * 500,
                evidence="Tecla speaks",
                memory_type="explicit_fact",
                subjects=(subject,),
                high_impact=True,
            )
        ],
    )
    snapshot = store.build_snapshot(
        blocks[3],
        knowledge_version=database.current_knowledge_version(),
        discourse_state=DiscourseState(active_speakers=("concept-tecla",)),
    )

    with pytest.raises(NarrativeContextOverflow, match="required narrative"):
        store.retrieve_for_block(
            blocks[3],
            snapshot,
            matched_subject_ids=("concept-tecla",),
            max_chars=50,
        )
