import json
import sqlite3
from contextlib import closing

import pytest

from src.core.v4.adjudicator import AdjudicationResult
from src.core.v4.candidate_clusters import CandidateCluster, CandidateContext
from src.core.v4.database import SCHEMA_VERSION, V4Database
from src.core.v4.lexical_index import LexicalCandidate
from src.core.v4.models import ScanOutcome


def _candidate(candidate_id, block, start, text, *, risk_flags=()):
    return LexicalCandidate(
        id=candidate_id,
        block_id=block.id,
        paragraph_id="P000",
        start_offset=start,
        end_offset=start + len(text),
        original_text=text,
        normalized_text=text,
        left_context="left",
        right_context="right",
        extraction_reason="test",
        book_frequency=1,
        score=1,
        risk_flags=tuple(risk_flags),
    )


def _cluster(cluster_id, candidates):
    contexts = tuple(
        CandidateContext(
            candidate_id=item.id,
            block_id=item.block_id,
            paragraph_id=item.paragraph_id,
            original_text=item.original_text,
            left_context=item.left_context,
            right_context=item.right_context,
            risk_flags=item.risk_flags,
        )
        for item in candidates[:3]
    )
    return CandidateCluster(
        id=cluster_id,
        alternatives=tuple(candidates[:4]),
        contexts=contexts,
        risk_flags=tuple(sorted({flag for item in candidates for flag in item.risk_flags})),
        affected_blocks=len({item.block_id for item in candidates}),
    )


def _seed_database(tmp_path, names=("Drotte", "Roche")):
    root = tmp_path / "book"
    db = V4Database(root)
    edition = db.ensure_source_edition("raw", "normalized", "test", "source.txt")
    source = " ".join(names)
    db.upsert_blocks(
        edition,
        [
            {
                "id": "block-1",
                "chapter_id": "ch01",
                "chapter_title": "I",
                "chapter_index": 0,
                "block_index": 0,
                "global_index": 0,
                "block_type": "prose",
                "source_text": source,
                "source_hash": "hash-1",
                "token_count": len(names),
            }
        ],
    )
    block = db.list_blocks()[0]
    candidates = []
    offset = 0
    for index, name in enumerate(names):
        candidates.append(
            _candidate(
                f"candidate-{index:04d}",
                block,
                offset,
                name,
                risk_flags=("span_competition",) if index == 0 else (),
            )
        )
        offset += len(name) + 1
    db.start_run("scan-run", "scan", {})
    db.start_run("judge-run", "candidate_adjudication", {})
    db.commit_scan_batch(
        "scan-run",
        [
            ScanOutcome(
                block=block,
                response=None,
                lexical_candidates=[item.storage_payload() for item in candidates],
            )
        ],
        "fake",
    )
    return db, edition, block, candidates


def test_schema6_migration_backfills_locked_targets_without_overwriting_values(tmp_path):
    root = tmp_path / "legacy"
    path = root / "artifacts" / "parallel_v4" / "book.db"
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES('schema_version', '6');
            CREATE TABLE knowledge_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO knowledge_versions(parent_id, reason, created_at)
            VALUES(NULL, 'legacy', 'now');
            CREATE TABLE concepts(
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                canonical_source TEXT NOT NULL,
                default_target TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'provisional',
                scope TEXT NOT NULL DEFAULT 'book',
                locked INTEGER NOT NULL DEFAULT 0,
                created_version INTEGER NOT NULL REFERENCES knowledge_versions(id),
                retired_version INTEGER REFERENCES knowledge_versions(id),
                created_at TEXT NOT NULL
            );
            INSERT INTO concepts VALUES(
                'locked-old', 'person', 'Drotte', '德罗特', '', 'verified',
                'book', 1, 1, NULL, 'now'
            );
            CREATE TABLE lexical_candidates(
                id TEXT PRIMARY KEY,
                block_id TEXT NOT NULL,
                paragraph_id TEXT NOT NULL,
                start_offset INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                original_text TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                left_context TEXT NOT NULL DEFAULT '',
                right_context TEXT NOT NULL DEFAULT '',
                extraction_reason TEXT NOT NULL,
                book_frequency INTEGER NOT NULL DEFAULT 1,
                model_status TEXT NOT NULL DEFAULT 'pending',
                selected INTEGER NOT NULL DEFAULT 0,
                run_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(block_id, start_offset, end_offset, normalized_text)
            );
            """
        )

    db = V4Database(root)
    with closing(db.connect()) as connection:
        concept = connection.execute(
            "SELECT working_target, verified_target FROM concepts WHERE id='locked-old'"
        ).fetchone()
        lexical_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(lexical_candidates)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]

    assert SCHEMA_VERSION == 7
    assert version == "7"
    assert concept["working_target"] == "德罗特"
    assert concept["verified_target"] == "德罗特"
    assert {"risk_flags_json", "resolution_status"} <= lexical_columns
    assert {
        "candidate_clusters",
        "candidate_cluster_members",
        "candidate_adjudications",
        "candidate_resolutions",
    } <= tables

    with db.transaction() as connection:
        connection.execute(
            "UPDATE concepts SET working_target='人工工作译名', verified_target='人工审定译名' "
            "WHERE id='locked-old'"
        )
        connection.execute(
            "UPDATE schema_meta SET value='6' WHERE key='schema_version'"
        )
    V4Database(root)
    with closing(db.connect()) as connection:
        preserved = connection.execute(
            "SELECT working_target, verified_target FROM concepts WHERE id='locked-old'"
        ).fetchone()
    assert tuple(preserved) == ("人工工作译名", "人工审定译名")


def test_cluster_persistence_and_adjudication_promotion_are_atomic(tmp_path):
    db, _, _, candidates = _seed_database(tmp_path)
    item = _cluster("cluster-1", candidates)
    summary = db.persist_candidate_clusters("scan-run", [item])
    assert summary["clusters"] == 1
    assert summary["members"] == 2

    result = AdjudicationResult(
        cluster_id="cluster-1",
        verdict="promote",
        selected_candidate_ids=(candidates[0].id,),
        entity_kind="person",
        confidence=0.97,
        reason="consensus",
        rounds=2,
    )
    committed = db.commit_adjudications("judge-run", [result])

    with closing(db.connect()) as connection:
        selected = connection.execute(
            "SELECT selected, model_status, resolution_status, risk_flags_json "
            "FROM lexical_candidates WHERE id=?",
            (candidates[0].id,),
        ).fetchone()
        concept = connection.execute(
            "SELECT canonical_source, kind, working_target, verified_target, locked "
            "FROM concepts WHERE id=?",
            (committed["concept_ids"][0],),
        ).fetchone()
        resolution = connection.execute(
            "SELECT cluster_id, candidate_id, concept_id, evidence_id, decision "
            "FROM candidate_resolutions"
        ).fetchone()

    assert selected["selected"] == 1
    assert selected["model_status"] == "accepted"
    assert selected["resolution_status"] == "promoted"
    assert json.loads(selected["risk_flags_json"]) == ["span_competition"]
    assert tuple(concept) == ("Drotte", "person", "", "", 0)
    assert resolution["cluster_id"] == "cluster-1"
    assert resolution["candidate_id"] == candidates[0].id
    assert resolution["concept_id"] == committed["concept_ids"][0]
    assert resolution["evidence_id"] is not None
    assert resolution["decision"] == "promote"

    # Re-indexing must refresh evidence flags without undoing a final decision.
    block = db.list_blocks()[0]
    updated = candidates[0].storage_payload(model_status="pending")
    updated["risk_flags_json"] = '["updated-risk"]'
    db.commit_scan_batch(
        "scan-run",
        [ScanOutcome(block=block, response=None, lexical_candidates=[updated])],
        "fake",
    )
    with closing(db.connect()) as connection:
        preserved = connection.execute(
            "SELECT selected, model_status, resolution_status, risk_flags_json "
            "FROM lexical_candidates WHERE id=?",
            (candidates[0].id,),
        ).fetchone()
    assert tuple(preserved)[:3] == (1, "accepted", "promoted")
    assert json.loads(preserved["risk_flags_json"]) == ["updated-risk"]

    bad_cluster = _cluster("cluster-bad", [candidates[0]])
    missing = _candidate("missing-candidate", db.list_blocks()[0], 0, "Missing")
    bad_cluster = CandidateCluster(
        id=bad_cluster.id,
        alternatives=(candidates[0], missing),
        contexts=bad_cluster.contexts,
        risk_flags=(),
        affected_blocks=1,
    )
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        db.persist_candidate_clusters("scan-run", [bad_cluster])
    with closing(db.connect()) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_clusters WHERE id='cluster-bad'"
        ).fetchone()[0] == 0


def test_promotion_keeps_entity_kinds_distinct_but_never_overwrites_human_lock(tmp_path):
    db, _, _, candidates = _seed_database(tmp_path)
    place_id = db.import_legacy_concept("Drotte", "", "place", "legacy guess")
    db.persist_candidate_clusters("scan-run", [_cluster("cluster-1", candidates)])

    first = db.commit_adjudications(
        "judge-run",
        [AdjudicationResult("cluster-1", "promote", (candidates[0].id,), "person", 0.9)],
    )
    person_id = first["concept_ids"][0]
    assert person_id != place_id

    # An explicit human lock wins identity resolution and remains untouched.
    locked_db, _, _, locked_candidates = _seed_database(tmp_path / "locked")
    locked = locked_db.lock_concept_translation("Drotte", "德罗特", kind="title")
    locked_db.persist_candidate_clusters(
        "scan-run", [_cluster("cluster-locked", locked_candidates)]
    )
    locked_db.commit_adjudications(
        "judge-run",
        [
            AdjudicationResult(
                "cluster-locked",
                "promote",
                (locked_candidates[0].id,),
                "person",
                0.9,
            )
        ],
    )
    with closing(locked_db.connect()) as connection:
        row = connection.execute(
            """SELECT kind, default_target, working_target, verified_target, locked
               FROM concepts WHERE id=?""",
            (locked["concept_id"],),
        ).fetchone()
        latest_resolution = connection.execute(
            "SELECT concept_id FROM candidate_resolutions WHERE candidate_id=?",
            (locked_candidates[0].id,),
        ).fetchone()
    assert tuple(row) == ("title", "德罗特", "德罗特", "德罗特", 1)
    assert latest_resolution["concept_id"] == locked["concept_id"]


def test_invalid_adjudication_rolls_back_every_result_in_batch(tmp_path):
    db, _, _, candidates = _seed_database(tmp_path)
    db.persist_candidate_clusters("scan-run", [_cluster("cluster-1", candidates)])
    valid = AdjudicationResult(
        "cluster-1", "promote", (candidates[0].id,), "person", 0.9
    )
    invalid = AdjudicationResult(
        "missing-cluster", "promote", (candidates[1].id,), "person", 0.9
    )

    with pytest.raises(ValueError, match="cluster"):
        db.commit_adjudications("judge-run", [valid, invalid])

    with closing(db.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidate_adjudications").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM candidate_resolutions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM lexical_candidates WHERE resolution_status!='pending'"
        ).fetchone()[0] == 0


def test_database_failure_halfway_through_promotion_rolls_back_all_writes(tmp_path):
    db, _, _, candidates = _seed_database(tmp_path)
    db.persist_candidate_clusters("scan-run", [_cluster("cluster-1", candidates)])
    with db.transaction() as connection:
        connection.execute(
            f"""CREATE TRIGGER fail_second_resolution
                BEFORE INSERT ON candidate_resolutions
                WHEN NEW.candidate_id='{candidates[1].id}'
                BEGIN SELECT RAISE(ABORT, 'forced storage failure'); END"""
        )

    result = AdjudicationResult(
        "cluster-1", "promote", (candidates[0].id,), "person", 0.9
    )
    with pytest.raises(sqlite3.IntegrityError, match="forced storage failure"):
        db.commit_adjudications("judge-run", [result])

    with closing(db.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidate_adjudications").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM candidate_resolutions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
        statuses = connection.execute(
            "SELECT resolution_status, selected FROM lexical_candidates ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in statuses] == [("pending", 0), ("pending", 0)]


@pytest.mark.parametrize(
    ("verdict", "reason", "expected_status"),
    [
        ("defer", "model_protocol_failure", "deferred"),
        ("defer", "independent_verdict_conflict", "deferred"),
        ("reject", "not_an_entity", "rejected"),
    ],
)
def test_nonpromotion_adjudications_remain_traceable(
    tmp_path, verdict, reason, expected_status
):
    db, _, _, candidates = _seed_database(tmp_path)
    db.persist_candidate_clusters("scan-run", [_cluster("cluster-1", candidates)])

    db.commit_adjudications(
        "judge-run",
        [AdjudicationResult("cluster-1", verdict, (), "concept", 0.4, reason)],
    )

    with closing(db.connect()) as connection:
        adjudication = connection.execute(
            "SELECT verdict, reason FROM candidate_adjudications"
        ).fetchone()
        decisions = connection.execute(
            "SELECT decision FROM candidate_resolutions ORDER BY ordinal"
        ).fetchall()
        statuses = connection.execute(
            "SELECT resolution_status FROM lexical_candidates ORDER BY id"
        ).fetchall()
    assert tuple(adjudication) == (verdict, reason)
    assert [row[0] for row in decisions] == [expected_status, expected_status]
    assert [row[0] for row in statuses] == [expected_status, expected_status]


def test_reset_token_guards_snapshot_and_preserves_sources_baseline_and_locks(tmp_path):
    db, edition, block, candidates = _seed_database(tmp_path)
    db.persist_candidate_clusters("scan-run", [_cluster("cluster-1", candidates)])
    db.commit_adjudications(
        "judge-run",
        [AdjudicationResult("cluster-1", "promote", (candidates[0].id,), "person", 0.9)],
    )
    locked = db.lock_concept_translation("Roche", "罗奇", kind="person")
    manual_concept_id = db.import_legacy_concept(
        "ManualTerm", "人工译名", "concept", "non-scan human data"
    )
    with db.transaction() as connection:
        connection.execute(
            """INSERT INTO source_paragraphs(
                   source_edition_id, paragraph_index, source_text, source_hash,
                   char_start, char_end
               ) VALUES(?, 0, 'Drotte Roche', 'paragraph-hash', 0, 12)""",
            (edition,),
        )
        connection.execute(
            """INSERT INTO baseline_documents(
                   id, source_edition_id, name, kind, file_path, file_sha256,
                   paragraph_count, metadata_json, active, created_at
               ) VALUES('baseline-1', ?, 'old', 'docx', 'old.docx', 'sha', 1, '{}', 1, 'now')""",
            (edition,),
        )
        connection.execute(
            "INSERT INTO baseline_paragraphs VALUES('baseline-1', 0, '旧译', 'target-hash')"
        )
        connection.execute(
            """INSERT INTO block_baseline_links(
                   block_id, baseline_document_id, paragraph_index, ordinal,
                   overlap_start, overlap_end
               ) VALUES(?, 'baseline-1', 0, 0, 0, 12)""",
            (block.id,),
        )
        evidence_id = connection.execute(
            """INSERT INTO evidence(
                   block_id, paragraph_id, kind, source_form, evidence_quote,
                   payload_json, confidence, extractor, run_id, created_at
               ) VALUES(?, 'P000', 'human_note', 'ManualTerm', 'ManualTerm',
                        '{}', 1.0, 'human', NULL, 'now')""",
            (block.id,),
        ).lastrowid
        connection.execute(
            """INSERT INTO mentions(
                   block_id, paragraph_id, source_form, normalized_form,
                   discourse_function, concept_id, evidence_id
               ) VALUES(?, 'P000', 'ManualTerm', 'manualterm', 'referential', ?, ?)""",
            (block.id, manual_concept_id, evidence_id),
        )

    preview = db.preview_scan_reset()
    assert preview["lexical_candidates"] == 2
    assert preview["unlocked_concepts"] == 1
    assert preview["locked_concepts"] == 1

    with db.transaction() as connection:
        connection.execute(
            "UPDATE lexical_candidates SET updated_at='changed' WHERE id=?",
            (candidates[0].id,),
        )
    with pytest.raises(ValueError, match="token"):
        db.reset_scan_derivatives(preview["token"])
    assert db.preview_scan_reset()["lexical_candidates"] == 2

    fresh = db.preview_scan_reset()
    deleted = db.reset_scan_derivatives(fresh["token"])
    assert deleted["lexical_candidates"] == 2
    assert deleted["lexical_candidates_deleted"] == 2
    assert db.source_block_count() == 1
    assert db.locked_concept_count() == 1
    with closing(db.connect()) as connection:
        assert connection.execute("SELECT status FROM blocks").fetchone()[0] == "pending"
        assert connection.execute("SELECT COUNT(*) FROM source_editions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM source_paragraphs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM baseline_documents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM baseline_paragraphs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM block_baseline_links").fetchone()[0] == 1
        locked_targets = connection.execute(
            "SELECT default_target, working_target, verified_target FROM concepts WHERE id=?",
            (locked["concept_id"],),
        ).fetchone()
        assert tuple(locked_targets) == ("罗奇", "罗奇", "罗奇")
        assert connection.execute(
            "SELECT COUNT(*) FROM concepts WHERE id=?", (manual_concept_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM evidence WHERE extractor='human'"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_moderate_candidate_storage_scale_keeps_integrity(tmp_path):
    names = tuple(f"Name{index:03d}" for index in range(240))
    db, _, _, candidates = _seed_database(tmp_path, names=names)
    clusters = [
        _cluster(f"cluster-{index:03d}", candidates[index : index + 4])
        for index in range(0, len(candidates), 4)
    ]
    persisted = db.persist_candidate_clusters("scan-run", list(reversed(clusters)))

    assert persisted == {"clusters": 60, "members": 240}
    with closing(db.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM lexical_candidates").fetchone()[0] == 240
        assert connection.execute("SELECT COUNT(*) FROM candidate_clusters").fetchone()[0] == 60
        assert connection.execute("SELECT COUNT(*) FROM candidate_cluster_members").fetchone()[0] == 240
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
