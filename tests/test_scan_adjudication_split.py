import json
from contextlib import closing

import pytest

from src.core.v4.adjudicator import AdjudicationResult, V4Adjudicator
from src.core.v4.candidate_clusters import CandidateCluster
from src.core.v4.database import V4Database
from src.core.v4.lexical_index import LexicalCandidate
from src.core.v4.models import ScanOutcome, ScanResponse
from src.core.v4.scanner import V4Scanner


class ExplodingLLM:
    def __init__(self):
        self.calls = 0

    def chat(self, **_kwargs):
        self.calls += 1
        raise AssertionError("local indexing must not call the model")

    def get_model(self, _purpose):
        self.calls += 1
        raise AssertionError("local indexing must not inspect model configuration")


class PromotingLLM:
    def __init__(self, verdict="promote"):
        self.verdict = verdict
        self.calls = 0

    def chat(self, *, messages, **_kwargs):
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        decisions = []
        for cluster in payload["clusters"]:
            selected = []
            if self.verdict == "promote":
                selected = [
                    min(
                        cluster["alternatives"],
                        key=lambda item: (item["text"].casefold(), item["text"]),
                    )["id"]
                ]
            decisions.append(
                {
                    "cluster_id": cluster["cluster_id"],
                    "verdict": self.verdict,
                    "selected_ids": selected,
                    "entity_kind": "person",
                    "confidence": 0.99,
                    "reason": "",
                }
            )
        return json.dumps({"decisions": decisions})


class FailSecondBatchLLM(PromotingLLM):
    def chat(self, *, messages, **kwargs):
        if self.calls == 1:
            self.calls += 1
            return "{}"
        return super().chat(messages=messages, **kwargs)


def make_database(tmp_path, texts):
    db = V4Database(tmp_path / "book")
    edition = db.ensure_source_edition("raw", "normalized", "test", "source.txt")
    db.upsert_blocks(
        edition,
        [
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
                "source_hash": f"hash-{index}",
                "token_count": len(source.split()),
                "status": "pending",
            }
            for index, source in enumerate(texts)
        ],
    )
    return db


@pytest.fixture
def database(tmp_path):
    db = V4Database(tmp_path / "book")
    edition = db.ensure_source_edition("raw", "normalized", "test", "source.txt")
    rows = [
        {
            "id": "front",
            "legacy_id": "v00_pre_000",
            "chapter_id": "pre",
            "chapter_title": "Preamble",
            "chapter_index": 0,
            "block_index": 0,
            "global_index": 0,
            "block_type": "frontmatter",
            "source_text": "THE COMPLETE BOOK",
            "source_hash": "front-hash",
            "token_count": 3,
            "status": "pending",
        },
        {
            "id": "prose-1",
            "legacy_id": "v01_ch01_000",
            "chapter_id": "ch01",
            "chapter_title": "I",
            "chapter_index": 1,
            "block_index": 0,
            "global_index": 1,
            "block_type": "prose",
            "source_text": "Severian waited beside the Corpse Door.",
            "source_hash": "prose-1-hash",
            "token_count": 6,
            "status": "pending",
        },
        {
            "id": "prose-2",
            "legacy_id": "v01_ch01_001",
            "chapter_id": "ch01",
            "chapter_title": "I",
            "chapter_index": 1,
            "block_index": 1,
            "global_index": 2,
            "block_type": "prose",
            "source_text": "Severian crossed the Old Yard.",
            "source_hash": "prose-2-hash",
            "token_count": 5,
            "status": "pending",
        },
    ]
    db.upsert_blocks(edition, rows)
    return db


def test_scan_project_is_local_indexes_candidates_and_frontmatter_atomically(database):
    llm = ExplodingLLM()
    result = V4Scanner(database, llm).scan_project(max_blocks=2)

    assert result["indexed"] == 2
    assert result["clusters"] > 0
    assert result["run_id"].startswith("scan_")
    assert llm.calls == 0
    with closing(database.connect()) as connection:
        statuses = dict(connection.execute("SELECT id, status FROM blocks"))
        assert statuses["front"] == "ready"
        assert statuses["prose-1"] == "scanned"
        assert connection.execute("SELECT COUNT(*) FROM lexical_candidates").fetchone()[0] > 0
        assert connection.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM candidate_adjudications").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM lexical_candidates WHERE block_id='front'"
        ).fetchone()[0] == 0

    database.reconcile_exact_forms()
    with closing(database.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == 0


def test_pending_cluster_loader_roundtrips_roles_contexts_and_offsets(database):
    scanner = V4Scanner(database, ExplodingLLM())
    scanner.scan_project(max_blocks=3)

    clusters = database.load_pending_candidate_clusters()
    assert clusters
    assert any(len(cluster.contexts) for cluster in clusters)
    assert any(cluster.affected_blocks == 2 for cluster in clusters)
    for cluster in clusters:
        alternative_ids = {item.id for item in cluster.alternatives}
        assert alternative_ids
        for alternative in cluster.alternatives:
            source = database.get_block_by_identifier(alternative.block_id).source_text
            assert (
                source[alternative.start_offset : alternative.end_offset]
                == alternative.original_text
            )
        with closing(database.connect()) as connection:
            roles = {
                row["candidate_id"]: row["role"]
                for row in connection.execute(
                    "SELECT candidate_id, role FROM candidate_cluster_members WHERE cluster_id=?",
                    (cluster.id,),
                )
            }
        assert all(roles[item.id] in {"alternative", "both"} for item in cluster.alternatives)
        assert all(
            context.candidate_id not in alternative_ids
            for context in cluster.contexts
            if roles[context.candidate_id] == "context"
        )

    with database.transaction() as connection:
        candidate_id = clusters[0].alternatives[0].id
        connection.execute(
            "UPDATE lexical_candidates SET risk_flags_json='not-json' WHERE id=?",
            (candidate_id,),
        )
    reloaded = database.load_pending_candidate_clusters()
    candidate = next(
        item
        for cluster in reloaded
        for item in cluster.alternatives
        if item.id == candidate_id
    )
    assert candidate.risk_flags == ()


def test_reindex_rebuilds_pending_clusters_without_duplicates(database):
    scanner = V4Scanner(database, ExplodingLLM())
    scanner.scan_project(max_blocks=3)
    with closing(database.connect()) as connection:
        first_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM candidate_clusters ORDER BY id"
            )
        ]
        connection.execute("UPDATE blocks SET status='pending'")
        connection.commit()

    scanner = V4Scanner(database, ExplodingLLM())
    scanner.scan_project(max_blocks=3)
    with closing(database.connect()) as connection:
        second_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM candidate_clusters ORDER BY id"
            )
        ]
        duplicate_memberships = connection.execute(
            """SELECT COUNT(*) FROM (
                   SELECT candidate_id, COUNT(*) n
                   FROM candidate_cluster_members
                   GROUP BY candidate_id HAVING n > 1
               )"""
        ).fetchone()[0]
    assert second_ids == first_ids
    assert duplicate_memberships == 0


def test_indexing_failure_does_not_partially_advance_a_wave(database, monkeypatch):
    scanner = V4Scanner(database, ExplodingLLM())

    def explode(_block):
        raise RuntimeError("extractor failed")

    monkeypatch.setattr(scanner.extractor, "extract", explode)
    with pytest.raises(RuntimeError, match="extractor failed"):
        scanner.scan_project(initial_workers=2, max_workers=2, max_blocks=2)
    with closing(database.connect()) as connection:
        statuses = dict(connection.execute("SELECT id, status FROM blocks"))
        run = connection.execute(
            "SELECT stage, status, error FROM runs ORDER BY started_at DESC, id DESC LIMIT 1"
        ).fetchone()
    assert statuses["front"] == "pending"
    assert statuses["prose-1"] == "pending"
    assert tuple(run) == ("scan", "failed", "extractor failed")


def test_adjudicator_run_calls_model_commits_concepts_and_does_not_repeat(database):
    V4Scanner(database, ExplodingLLM()).scan_project(max_blocks=3)
    llm = PromotingLLM()
    pending_count = len(database.load_pending_candidate_clusters())
    result = V4Adjudicator(llm, database=database).run()

    assert result["adjudicated"] == pending_count
    assert result["concepts"] >= 1
    first_call_count = llm.calls
    assert first_call_count >= 1
    with closing(database.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] >= 1
        run = connection.execute(
            "SELECT stage, status FROM runs WHERE id=?", (result["run_id"],)
        ).fetchone()
        assert tuple(run) == ("adjudicate", "completed")

    second = V4Adjudicator(llm, database=database).run()
    assert second["adjudicated"] == 0
    assert llm.calls == first_call_count


def test_deferred_adjudication_is_active_but_creates_no_concept(database):
    V4Scanner(database, ExplodingLLM()).scan_project(max_blocks=3)
    llm = PromotingLLM(verdict="defer")
    result = V4Adjudicator(llm, database=database).run(max_clusters=1)

    assert result["adjudicated"] == 1
    assert result["concepts"] == 0
    assert result["failed"] == 0
    assert result["deferred"] == 1
    with closing(database.connect()) as connection:
        row = connection.execute(
            "SELECT verdict, active FROM candidate_adjudications WHERE run_id=?",
            (result["run_id"],),
        ).fetchone()
        assert tuple(row) == ("defer", 1)
        run = connection.execute(
            "SELECT status FROM runs WHERE id=?", (result["run_id"],)
        ).fetchone()
        assert run["status"] == "completed"
        assert connection.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == 0


def test_adjudicator_run_records_failed_stage(database, monkeypatch):
    V4Scanner(database, ExplodingLLM()).scan_project(max_blocks=3)

    def explode(*_args, **_kwargs):
        raise RuntimeError("storage failed")

    monkeypatch.setattr(database, "commit_adjudications", explode)
    with pytest.raises(RuntimeError, match="storage failed"):
        V4Adjudicator(PromotingLLM(), database=database).run(max_clusters=1)
    with closing(database.connect()) as connection:
        row = connection.execute(
            "SELECT stage, status, error FROM runs ORDER BY started_at DESC, id DESC LIMIT 1"
        ).fetchone()
    assert row["stage"] == "adjudicate"
    assert row["status"] == "failed"
    assert "storage failed" in row["error"]


def test_active_cluster_context_only_member_is_not_reclustered(tmp_path):
    db = make_database(tmp_path, ["Severian waited.", "Severian returned."])
    V4Scanner(db, ExplodingLLM()).scan_project()
    cluster = next(
        item for item in db.load_pending_candidate_clusters() if item.affected_blocks == 2
    )
    alternative_ids = {item.id for item in cluster.alternatives}
    context_only_ids = {
        item.candidate_id for item in cluster.contexts
    } - alternative_ids
    assert context_only_ids

    db.start_run("judge-context", "adjudicate", {})
    db.commit_adjudications(
        "judge-context",
        [
            AdjudicationResult(
                cluster_id=cluster.id,
                verdict="promote",
                selected_candidate_ids=(cluster.alternatives[0].id,),
                entity_kind="person",
                confidence=0.99,
            )
        ],
    )
    db.finish_run("judge-context", "completed")
    with db.transaction() as connection:
        connection.execute("UPDATE blocks SET status='pending'")

    V4Scanner(db, ExplodingLLM()).scan_project()
    pending_ids = {item.id for item in db.load_pending_lexical_candidates()}
    assert not pending_ids.intersection(alternative_ids | context_only_ids)
    assert db.load_pending_candidate_clusters() == []
    with closing(db.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidate_clusters").fetchone()[0] == 1


def test_inactive_history_reopens_as_stable_new_generation(tmp_path):
    db = make_database(tmp_path, ["Severian waited."])
    V4Scanner(db, ExplodingLLM()).scan_project()
    historical = db.load_pending_candidate_clusters()[0]
    db.start_run("judge-old", "adjudicate", {})
    db.commit_adjudications(
        "judge-old",
        [
            AdjudicationResult(
                cluster_id=historical.id,
                verdict="reject",
                selected_candidate_ids=(),
                entity_kind="person",
                confidence=0.99,
            )
        ],
    )
    db.finish_run("judge-old", "completed")
    with db.transaction() as connection:
        connection.execute("UPDATE candidate_adjudications SET active=0")
        connection.execute(
            """UPDATE lexical_candidates
               SET resolution_status='pending', model_status='pending', selected=0"""
        )
        connection.execute("UPDATE blocks SET status='pending'")

    assert db.load_pending_candidate_clusters() == []
    V4Scanner(db, ExplodingLLM()).scan_project()
    reopened = db.load_pending_candidate_clusters()
    assert len(reopened) == 1
    assert reopened[0].id != historical.id
    first_generation_id = reopened[0].id
    with db.transaction() as connection:
        connection.execute("UPDATE blocks SET status='pending'")
    V4Scanner(db, ExplodingLLM()).scan_project()
    assert [item.id for item in db.load_pending_candidate_clusters()] == [
        first_generation_id
    ]
    with closing(db.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidate_clusters").fetchone()[0] == 2


def test_pending_cluster_loader_never_returns_resolved_alternative(database):
    V4Scanner(database, ExplodingLLM()).scan_project(max_blocks=2)
    cluster = next(
        item
        for item in database.load_pending_candidate_clusters()
        if len(item.alternatives) >= 2
    )
    rejected_id = cluster.alternatives[-1].id
    with database.transaction() as connection:
        connection.execute(
            """UPDATE lexical_candidates
               SET resolution_status='rejected', model_status='rejected'
               WHERE id=?""",
            (rejected_id,),
        )
    loaded = next(
        item
        for item in database.load_pending_candidate_clusters()
        if item.id == cluster.id
    )
    assert rejected_id not in {item.id for item in loaded.alternatives}


def test_adjudicator_run_marks_second_batch_protocol_failure_completed_with_errors(
    tmp_path,
):
    names = [f"Name{chr(ord('A') + index)}" for index in range(13)]
    source = " ".join(names)
    db = make_database(tmp_path, [source])
    block = db.list_blocks()[0]
    candidates = []
    cursor = 0
    for index, name in enumerate(names):
        start = source.index(name, cursor)
        end = start + len(name)
        cursor = end
        candidates.append(
            LexicalCandidate(
                id=f"candidate-{index:02d}",
                block_id=block.id,
                paragraph_id="P000",
                start_offset=start,
                end_offset=end,
                original_text=name,
                normalized_text=name,
                left_context="",
                right_context="",
                extraction_reason="test",
                book_frequency=1,
                score=0,
            )
        )
    db.start_run("scan-thirteen", "scan", {})
    db.commit_candidate_index_batch(
        "scan-thirteen",
        [
            ScanOutcome(
                block=block,
                response=ScanResponse(),
                lexical_candidates=[item.storage_payload() for item in candidates],
            )
        ],
    )
    db.persist_candidate_clusters(
        "scan-thirteen",
        [
            CandidateCluster(
                id=f"cluster-{index:02d}",
                alternatives=(candidate,),
                contexts=(),
                risk_flags=(),
                affected_blocks=1,
            )
            for index, candidate in enumerate(candidates)
        ],
    )
    db.finish_run("scan-thirteen", "completed")

    result = V4Adjudicator(
        FailSecondBatchLLM(), max_attempts=1, database=db
    ).run()
    assert result["adjudicated"] == 13
    assert result["failed"] == 1
    assert result["deferred"] == 1
    with closing(db.connect()) as connection:
        run = connection.execute(
            "SELECT status FROM runs WHERE id=?", (result["run_id"],)
        ).fetchone()
        failures = connection.execute(
            """SELECT COUNT(*) FROM candidate_adjudications
               WHERE run_id=? AND reason='model_protocol_failure' AND active=1""",
            (result["run_id"],),
        ).fetchone()[0]
    assert run["status"] == "completed_with_errors"
    assert failures == 1
