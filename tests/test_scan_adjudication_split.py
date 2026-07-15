import json
from contextlib import closing

import pytest

from src.core.v4.adjudicator import V4Adjudicator
from src.core.v4.database import V4Database
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
    with closing(database.connect()) as connection:
        row = connection.execute(
            "SELECT verdict, active FROM candidate_adjudications WHERE run_id=?",
            (result["run_id"],),
        ).fetchone()
        assert tuple(row) == ("defer", 1)
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
