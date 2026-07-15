import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from src.core.v4.adjudicator import AdjudicationResult
from src.core.v4.audit_archive import (
    AuditArchive,
    StorageBudget,
    StorageBudgetExceeded,
)
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


def test_unreleased_legacy_schema7_adjudication_table_is_migrated_in_place(tmp_path):
    root = tmp_path / "legacy-schema7"
    path = root / "artifacts" / "parallel_v4" / "book.db"
    path.parent.mkdir(parents=True)
    payload = {
        "cluster_id": "cluster-old",
        "verdict": "defer",
        "selected_candidate_ids": [],
        "entity_kind": "concept",
        "confidence": 0.4,
        "reason": "model_protocol_failure",
        "rounds": 1,
    }
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            f"""
            CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES('schema_version', '7');
            CREATE TABLE knowledge_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER REFERENCES knowledge_versions(id),
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO knowledge_versions(parent_id, reason, created_at)
            VALUES(NULL, 'legacy', 'now');
            CREATE TABLE runs(
                id TEXT PRIMARY KEY, stage TEXT NOT NULL, status TEXT NOT NULL,
                knowledge_version INTEGER, config_json TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, error TEXT
            );
            INSERT INTO runs VALUES(
                'run-old', 'candidate_adjudication', 'completed', 1, '{{}}',
                'now', 'now', NULL
            );
            CREATE TABLE candidate_clusters(
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id),
                risk_flags_json TEXT NOT NULL DEFAULT '[]',
                affected_blocks_json TEXT NOT NULL DEFAULT '[]',
                affected_block_count INTEGER NOT NULL DEFAULT 0,
                ordinal INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id, id)
            );
            INSERT INTO candidate_clusters VALUES(
                'cluster-old', 'run-old', '[]', '[]', 0, 0, 'now', 'now'
            );
            CREATE TABLE candidate_adjudications(
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                cluster_id TEXT NOT NULL,
                verdict TEXT NOT NULL,
                selected_candidate_ids_json TEXT NOT NULL DEFAULT '[]',
                entity_kind TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.0,
                reason TEXT NOT NULL DEFAULT '',
                rounds INTEGER NOT NULL DEFAULT 1,
                payload_json TEXT NOT NULL DEFAULT '{{}}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id, cluster_id),
                FOREIGN KEY(cluster_id) REFERENCES candidate_clusters(id) ON DELETE CASCADE
            );
            """
        )
        connection.execute(
            """INSERT INTO candidate_adjudications(
                   id, run_id, cluster_id, verdict, selected_candidate_ids_json,
                   entity_kind, confidence, reason, rounds, payload_json,
                   created_at, updated_at
               ) VALUES('adjud-old', 'run-old', 'cluster-old', 'defer', '[]',
                        'concept', 0.4, 'model_protocol_failure', 1, ?, 'now', 'now')""",
            (payload_json,),
        )
        connection.commit()

    db = V4Database(root)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    with closing(db.connect()) as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(candidate_adjudications)")
        }
        row = connection.execute(
            """SELECT payload_hash, knowledge_version, active, superseded_at
               FROM candidate_adjudications WHERE id='adjud-old'"""
        ).fetchone()
        table_sql = connection.execute(
            """SELECT sql FROM sqlite_master
               WHERE type='table' AND name='candidate_adjudications'"""
        ).fetchone()[0]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert {"payload_hash", "knowledge_version", "active", "superseded_at"} <= columns
    assert tuple(row) == (expected_hash, 1, 1, None)
    assert "UNIQUE(run_id, cluster_id)" not in table_sql.replace("\n", " ")


def test_legacy_schema7_migration_recovers_adjudication_version_for_exact_retry(tmp_path):
    db, _, _, candidates = _seed_database(tmp_path)
    db.persist_candidate_clusters("scan-run", [_cluster("cluster-old", candidates)])
    result = AdjudicationResult(
        cluster_id="cluster-old",
        verdict="defer",
        selected_candidate_ids=(),
        entity_kind="concept",
        confidence=0.4,
        reason="model_protocol_failure",
        rounds=1,
    )
    first = db.commit_adjudications("judge-run", [result])
    assert first["knowledge_version"] == 2

    with closing(sqlite3.connect(db.path)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executescript(
            """
            CREATE TABLE candidate_adjudications_b7(
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                cluster_id TEXT NOT NULL,
                verdict TEXT NOT NULL,
                selected_candidate_ids_json TEXT NOT NULL DEFAULT '[]',
                entity_kind TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.0,
                reason TEXT NOT NULL DEFAULT '',
                rounds INTEGER NOT NULL DEFAULT 1,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id, cluster_id),
                FOREIGN KEY(cluster_id)
                    REFERENCES candidate_clusters(id) ON DELETE CASCADE
            );
            INSERT INTO candidate_adjudications_b7(
                id, run_id, cluster_id, verdict, selected_candidate_ids_json,
                entity_kind, confidence, reason, rounds, payload_json,
                created_at, updated_at)
            SELECT id, run_id, cluster_id, verdict, selected_candidate_ids_json,
                   entity_kind, confidence, reason, rounds, payload_json,
                   created_at, updated_at
            FROM candidate_adjudications WHERE active=1;
            DROP TABLE candidate_adjudications;
            ALTER TABLE candidate_adjudications_b7
                RENAME TO candidate_adjudications;
            """
        )
        connection.commit()

    migrated = V4Database(db.project_root)
    with closing(migrated.connect()) as connection:
        adjudication_version = connection.execute(
            "SELECT knowledge_version FROM candidate_adjudications WHERE active=1"
        ).fetchone()[0]
        before_versions = connection.execute(
            "SELECT COUNT(*) FROM knowledge_versions"
        ).fetchone()[0]
    retry = migrated.commit_adjudications("judge-run", [result])
    with closing(migrated.connect()) as connection:
        after_versions = connection.execute(
            "SELECT COUNT(*) FROM knowledge_versions"
        ).fetchone()[0]

    assert adjudication_version == 2
    assert retry["knowledge_version"] == 2
    assert retry["changed"] == 0
    assert after_versions == before_versions == 2


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


def test_context_only_member_cannot_be_selected_or_resolved(tmp_path):
    db, _, _, candidates = _seed_database(tmp_path)
    alternative, context_only = candidates
    item = CandidateCluster(
        id="cluster-context",
        alternatives=(alternative,),
        contexts=(
            CandidateContext(
                candidate_id=context_only.id,
                block_id=context_only.block_id,
                paragraph_id=context_only.paragraph_id,
                original_text=context_only.original_text,
                left_context=context_only.left_context,
                right_context=context_only.right_context,
                risk_flags=context_only.risk_flags,
            ),
        ),
        risk_flags=(),
        affected_blocks=1,
    )
    db.persist_candidate_clusters("scan-run", [item])

    with pytest.raises(ValueError, match="alternative"):
        db.commit_adjudications(
            "judge-run",
            [
                AdjudicationResult(
                    "cluster-context",
                    "promote",
                    (context_only.id,),
                    "person",
                    0.9,
                )
            ],
        )
    with closing(db.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidate_adjudications").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM candidate_resolutions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == 0

    db.commit_adjudications(
        "judge-run",
        [AdjudicationResult("cluster-context", "reject", (), "concept", 0.8)],
    )
    with closing(db.connect()) as connection:
        rows = connection.execute(
            """SELECT lc.id, lc.resolution_status, cm.role,
                      EXISTS(SELECT 1 FROM candidate_resolutions cr
                             WHERE cr.candidate_id=lc.id) has_resolution
               FROM lexical_candidates lc
               JOIN candidate_cluster_members cm ON cm.candidate_id=lc.id
               ORDER BY lc.id"""
        ).fetchall()
    by_id = {row["id"]: row for row in rows}
    assert tuple(by_id[alternative.id])[1:] == ("rejected", "alternative", 1)
    assert tuple(by_id[context_only.id])[1:] == ("pending", "context", 0)


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


def test_changed_adjudication_keeps_history_and_exact_retry_is_idempotent(tmp_path):
    db, _, _, candidates = _seed_database(tmp_path)
    db.persist_candidate_clusters("scan-run", [_cluster("cluster-1", candidates)])
    promote = AdjudicationResult(
        "cluster-1", "promote", (candidates[0].id,), "person", 0.9
    )
    reject = AdjudicationResult(
        "cluster-1", "reject", (), "person", 0.8, "not_an_entity"
    )

    first = db.commit_adjudications("judge-run", [promote])
    with closing(db.connect()) as connection:
        before = {
            "knowledge_versions": connection.execute(
                "SELECT COUNT(*) FROM knowledge_versions"
            ).fetchone()[0],
            "adjudications": connection.execute(
                "SELECT COUNT(*) FROM candidate_adjudications"
            ).fetchone()[0],
            "resolutions": connection.execute(
                "SELECT COUNT(*) FROM candidate_resolutions"
            ).fetchone()[0],
            "evidence": connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
            "mentions": connection.execute("SELECT COUNT(*) FROM mentions").fetchone()[0],
            "concepts": connection.execute("SELECT COUNT(*) FROM concepts").fetchone()[0],
            "adjudication_row": tuple(
                connection.execute(
                    """SELECT id, verdict, payload_json, created_at, updated_at
                       FROM candidate_adjudications WHERE active=1"""
                ).fetchone()
            ),
        }

    identical = db.commit_adjudications("judge-run", [promote])
    with closing(db.connect()) as connection:
        assert identical["knowledge_version"] == first["knowledge_version"]
        assert connection.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0] == before["knowledge_versions"]
        assert connection.execute("SELECT COUNT(*) FROM candidate_adjudications").fetchone()[0] == before["adjudications"]
        assert connection.execute("SELECT COUNT(*) FROM candidate_resolutions").fetchone()[0] == before["resolutions"]
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == before["evidence"]
        assert connection.execute("SELECT COUNT(*) FROM mentions").fetchone()[0] == before["mentions"]
        assert connection.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == before["concepts"]
        assert tuple(
            connection.execute(
                """SELECT id, verdict, payload_json, created_at, updated_at
                   FROM candidate_adjudications WHERE active=1"""
            ).fetchone()
        ) == before["adjudication_row"]

    changed = db.commit_adjudications("judge-run", [reject])
    with closing(db.connect()) as connection:
        adjudications = connection.execute(
            """SELECT verdict, active, knowledge_version
               FROM candidate_adjudications ORDER BY created_at, id"""
        ).fetchall()
        active_resolutions = connection.execute(
            """SELECT cr.decision FROM candidate_resolutions cr
               JOIN candidate_adjudications ca ON ca.id=cr.adjudication_id
               WHERE ca.active=1 ORDER BY cr.ordinal"""
        ).fetchall()
        old_evidence = connection.execute(
            """SELECT COUNT(*) FROM candidate_resolutions cr
               JOIN candidate_adjudications ca ON ca.id=cr.adjudication_id
               JOIN evidence e ON e.id=cr.evidence_id
               WHERE ca.active=0 AND ca.verdict='promote'"""
        ).fetchone()[0]
        concept = connection.execute(
            "SELECT status, retired_version FROM concepts WHERE id=?",
            (first["concept_ids"][0],),
        ).fetchone()
        candidate_rows = connection.execute(
            "SELECT resolution_status, selected FROM lexical_candidates ORDER BY id"
        ).fetchall()

    assert changed["knowledge_version"] != first["knowledge_version"]
    assert [(row["verdict"], row["active"]) for row in adjudications] == [
        ("promote", 0),
        ("reject", 1),
    ]
    assert [row[0] for row in active_resolutions] == ["rejected", "rejected"]
    assert old_evidence == 1
    assert concept["status"] == "retired"
    assert concept["retired_version"] == changed["knowledge_version"]
    assert [tuple(row) for row in candidate_rows] == [("rejected", 0), ("rejected", 0)]

    before_retry = db.current_knowledge_version()
    retry = db.commit_adjudications("judge-run", [reject])
    with closing(db.connect()) as connection:
        assert retry["knowledge_version"] == changed["knowledge_version"]
        assert db.current_knowledge_version() == before_retry
        assert connection.execute("SELECT COUNT(*) FROM candidate_adjudications").fetchone()[0] == 2


def test_retired_automatic_concept_is_reactivated_when_promoted_again(tmp_path):
    db, _, _, candidates = _seed_database(tmp_path)
    db.persist_candidate_clusters("scan-run", [_cluster("cluster-1", candidates)])
    promote = AdjudicationResult(
        "cluster-1", "promote", (candidates[0].id,), "person", 0.9
    )
    reject = AdjudicationResult(
        "cluster-1", "reject", (), "person", 0.8, "not_an_entity"
    )

    first = db.commit_adjudications("judge-run", [promote])
    db.commit_adjudications("judge-run", [reject])
    third = db.commit_adjudications("judge-run", [promote])

    with closing(db.connect()) as connection:
        active_resolution = connection.execute(
            """SELECT cr.concept_id, c.status, c.retired_version
               FROM candidate_resolutions cr
               JOIN candidate_adjudications ca ON ca.id=cr.adjudication_id
               JOIN concepts c ON c.id=cr.concept_id
               WHERE ca.active=1 AND cr.candidate_id=?""",
            (candidates[0].id,),
        ).fetchone()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert third["concept_ids"] == first["concept_ids"]
    assert active_resolution["concept_id"] == first["concept_ids"][0]
    assert active_resolution["status"] == "provisional"
    assert active_resolution["retired_version"] is None


def test_active_human_lock_wins_over_retired_automatic_identity(tmp_path):
    db, _, _, candidates = _seed_database(tmp_path)
    db.persist_candidate_clusters("scan-run", [_cluster("cluster-1", candidates)])
    promote = AdjudicationResult(
        "cluster-1", "promote", (candidates[0].id,), "person", 0.9
    )
    reject = AdjudicationResult(
        "cluster-1", "reject", (), "person", 0.8, "not_an_entity"
    )
    automatic_id = db.commit_adjudications("judge-run", [promote])["concept_ids"][0]
    db.commit_adjudications("judge-run", [reject])
    human = db.lock_concept_translation(
        candidates[0].original_text,
        "人工译名",
        kind="title",
        description="人工说明",
    )

    promoted = db.commit_adjudications("judge-run", [promote])
    with closing(db.connect()) as connection:
        active_concept_id = connection.execute(
            """SELECT cr.concept_id FROM candidate_resolutions cr
               JOIN candidate_adjudications ca ON ca.id=cr.adjudication_id
               WHERE ca.active=1 AND cr.candidate_id=?""",
            (candidates[0].id,),
        ).fetchone()[0]
        automatic = connection.execute(
            """SELECT status, locked, retired_version FROM concepts WHERE id=?""",
            (automatic_id,),
        ).fetchone()
        human_row = connection.execute(
            """SELECT kind, default_target, working_target, verified_target,
                      description, status, locked, retired_version
               FROM concepts WHERE id=?""",
            (human["concept_id"],),
        ).fetchone()

    assert promoted["concept_ids"] == [human["concept_id"]]
    assert active_concept_id == human["concept_id"]
    assert tuple(automatic) == ("retired", 0, 3)
    assert tuple(human_row) == (
        "title",
        "人工译名",
        "人工译名",
        "人工译名",
        "人工说明",
        "verified",
        1,
        None,
    )


def test_locked_automatic_base_id_is_reused_without_collision(tmp_path):
    db, _, _, candidates = _seed_database(tmp_path)
    db.persist_candidate_clusters("scan-run", [_cluster("cluster-1", candidates)])
    promote = AdjudicationResult(
        "cluster-1", "promote", (candidates[0].id,), "person", 0.9
    )
    automatic_id = db.commit_adjudications("judge-run", [promote])["concept_ids"][0]
    with db.transaction() as connection:
        connection.execute(
            """UPDATE concepts SET locked=1, status='verified',
                      default_target='人工译名', working_target='人工译名',
                      verified_target='人工译名'
               WHERE id=?""",
            (automatic_id,),
        )
        connection.execute(
            """UPDATE lexical_candidates SET resolution_status='rejected',
                      model_status='rejected', selected=0 WHERE id=?""",
            (candidates[0].id,),
        )

    repaired = db.commit_adjudications("judge-run", [promote])
    with closing(db.connect()) as connection:
        concept_ids = connection.execute(
            "SELECT id FROM concepts ORDER BY id"
        ).fetchall()
        locked_row = connection.execute(
            """SELECT default_target, working_target, verified_target,
                      status, locked, retired_version
               FROM concepts WHERE id=?""",
            (automatic_id,),
        ).fetchone()

    assert repaired["concept_ids"] == [automatic_id]
    assert [row[0] for row in concept_ids] == [automatic_id]
    assert tuple(locked_row) == (
        "人工译名",
        "人工译名",
        "人工译名",
        "verified",
        1,
        None,
    )


@pytest.mark.parametrize(
    ("verdict", "selected_indexes", "reason", "overlap"),
    [
        ("promote", (0, 1), "", False),
        ("split", (0,), "", False),
        ("split", (0, 1), "", True),
        ("supersede", (0, 1), "", False),
        ("supersede", (0,), "", False),
        ("promote", (0,), "missing_span", False),
    ],
)
def test_commit_adjudications_revalidates_protocol_atomically(
    tmp_path, verdict, selected_indexes, reason, overlap
):
    db, _, _, candidates = _seed_database(tmp_path)
    db.persist_candidate_clusters("scan-run", [_cluster("cluster-1", candidates)])
    if overlap:
        with db.transaction() as connection:
            connection.execute(
                """UPDATE lexical_candidates SET start_offset=3, end_offset=9
                   WHERE id=?""",
                (candidates[1].id,),
            )
    before_version = db.current_knowledge_version()
    selected = tuple(candidates[index].id for index in selected_indexes)

    with pytest.raises(ValueError):
        db.commit_adjudications(
            "judge-run",
            [
                AdjudicationResult(
                    "cluster-1", verdict, selected, "concept", 0.9, reason
                )
            ],
        )

    with closing(db.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidate_adjudications").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM candidate_resolutions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == 0
        statuses = connection.execute(
            "SELECT resolution_status, selected FROM lexical_candidates ORDER BY id"
        ).fetchall()
    assert db.current_knowledge_version() == before_version
    assert [tuple(row) for row in statuses] == [("pending", 0), ("pending", 0)]


def test_identical_payload_repairs_drifted_active_candidate_state(tmp_path):
    db, _, _, candidates = _seed_database(tmp_path)
    db.persist_candidate_clusters("scan-run", [_cluster("cluster-1", candidates)])
    result = AdjudicationResult(
        "cluster-1", "promote", (candidates[0].id,), "person", 0.9
    )
    first = db.commit_adjudications("judge-run", [result])
    with db.transaction() as connection:
        connection.execute(
            """UPDATE lexical_candidates SET resolution_status='rejected',
               model_status='rejected', selected=0 WHERE id=?""",
            (candidates[0].id,),
        )

    repaired = db.commit_adjudications("judge-run", [result])

    assert repaired["knowledge_version"] != first["knowledge_version"]
    with closing(db.connect()) as connection:
        candidate_row = connection.execute(
            "SELECT resolution_status, model_status, selected FROM lexical_candidates WHERE id=?",
            (candidates[0].id,),
        ).fetchone()
        assert tuple(candidate_row) == ("promoted", "accepted", 1)
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_adjudications"
        ).fetchone()[0] == 2


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
    assert preview["concepts"] == 1
    assert preview["preserved_locked_concepts"] == 1

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
    deletion_keys = {
        "lexical_candidates",
        "candidate_clusters",
        "candidate_cluster_members",
        "candidate_adjudications",
        "candidate_resolutions",
        "usage_decisions",
        "mentions",
        "evidence",
        "claim_evidence",
        "claims",
        "source_forms",
        "rendering_rules",
        "verification_votes",
        "verification_tasks",
        "concepts",
        "blocks_reset",
    }
    assert {key: fresh[key] for key in deletion_keys} == deleted
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


def test_reset_token_tracks_usage_lock_and_preserves_locked_occurrence_dependencies(tmp_path):
    db, _, block, candidates = _seed_database(tmp_path)
    db.persist_candidate_clusters("scan-run", [_cluster("cluster-1", candidates)])
    committed = db.commit_adjudications(
        "judge-run",
        [AdjudicationResult("cluster-1", "promote", (candidates[0].id,), "person", 0.9)],
    )
    concept_id = committed["concept_ids"][0]
    with db.transaction() as connection:
        version = connection.execute("SELECT MAX(id) FROM knowledge_versions").fetchone()[0]
        cursor = connection.execute(
            """INSERT INTO evidence(
                   block_id, paragraph_id, kind, source_form, evidence_quote,
                   payload_json, confidence, extractor, run_id, created_at
               ) VALUES(?, 'P000', 'mention', 'Drotte', 'Drotte', '{}', 1.0,
                        'scan_v4_2', 'scan-run', 'now')""",
            (block.id,),
        )
        evidence_id = cursor.lastrowid
        cursor = connection.execute(
            """INSERT INTO mentions(
                   block_id, paragraph_id, source_form, normalized_form,
                   discourse_function, concept_id, evidence_id
               ) VALUES(?, 'P000', 'Drotte', 'drotte', 'vocative', ?, ?)""",
            (block.id, concept_id, evidence_id),
        )
        mention_id = cursor.lastrowid
        cursor = connection.execute(
            """INSERT INTO usage_decisions(
                   mention_id, rendering, status, scope, locked,
                   created_version, created_at
               ) VALUES(?, '德罗特阁下', 'provisional', 'occurrence', 0, ?, 'now')""",
            (mention_id, version),
        )
        usage_id = cursor.lastrowid

    preview = db.preview_scan_reset()
    assert preview["usage_decisions"] == 1
    with db.transaction() as connection:
        connection.execute(
            """UPDATE usage_decisions SET locked=1, status='verified'
               WHERE id=?""",
            (usage_id,),
        )
    with pytest.raises(ValueError, match="token"):
        db.reset_scan_derivatives(preview["token"])

    fresh = db.preview_scan_reset()
    assert fresh["usage_decisions"] == 0
    db.reset_scan_derivatives(fresh["token"])
    with closing(db.connect()) as connection:
        usage = connection.execute(
            "SELECT locked, status, mention_id FROM usage_decisions WHERE id=?", (usage_id,)
        ).fetchone()
        mention = connection.execute(
            "SELECT evidence_id, concept_id FROM mentions WHERE id=?", (mention_id,)
        ).fetchone()
        assert tuple(usage) == (1, "verified", mention_id)
        assert mention["evidence_id"] == evidence_id
        assert mention["concept_id"] == concept_id
        assert connection.execute("SELECT COUNT(*) FROM evidence WHERE id=?", (evidence_id,)).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM source_forms WHERE concept_id=?", (concept_id,)).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM concepts WHERE id=?", (concept_id,)).fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reset_preserves_locked_claim_and_its_scan_evidence_chain(tmp_path):
    db, _, block, _ = _seed_database(tmp_path)
    version = db.current_knowledge_version()
    with db.transaction() as connection:
        evidence_id = connection.execute(
            """INSERT INTO evidence(
                   block_id, paragraph_id, kind, source_form, evidence_quote,
                   payload_json, confidence, extractor, run_id, created_at)
               VALUES(?, 'P000', 'claim', 'Drotte', 'Drotte', '{}', 1.0,
                      'scan_full', 'scan-run', 'now')""",
            (block.id,),
        ).lastrowid
        connection.execute(
            """INSERT INTO claims(
                   id, kind, statement, subject_form, status, scope, confidence,
                   reveal_global_index, high_impact, locked, created_version,
                   created_at)
               VALUES('claim-locked', 'identity', 'Drotte is Drotte', 'Drotte',
                      'verified', 'book', 1.0, 0, 1, 1, ?, 'now')""",
            (version,),
        )
        connection.execute(
            "INSERT INTO claim_evidence(claim_id, evidence_id) VALUES('claim-locked', ?)",
            (evidence_id,),
        )

    preview = db.preview_scan_reset()
    assert preview["claims"] == 0
    assert preview["claim_evidence"] == 0
    assert preview["evidence"] == 0
    deleted = db.reset_scan_derivatives(preview["token"])

    assert deleted["claims"] == preview["claims"]
    assert deleted["claim_evidence"] == preview["claim_evidence"]
    assert deleted["evidence"] == preview["evidence"]
    with closing(db.connect()) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM claims WHERE id='claim-locked'"
        ).fetchone()[0] == 1
        assert connection.execute(
            """SELECT COUNT(*) FROM claim_evidence
               WHERE claim_id='claim-locked' AND evidence_id=?""",
            (evidence_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM evidence WHERE id=?", (evidence_id,)
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reset_token_tracks_exact_block_status_rows(tmp_path):
    db, edition, block, _ = _seed_database(tmp_path)
    db.upsert_blocks(
        edition,
        [
            {
                "id": "block-pending",
                "chapter_id": "ch01",
                "chapter_title": "I",
                "chapter_index": 0,
                "block_index": 1,
                "global_index": 1,
                "block_type": "prose",
                "source_text": "Pending.",
                "source_hash": "pending-hash",
                "token_count": 1,
                "status": "pending",
            }
        ],
    )
    with db.transaction() as connection:
        pending_before = connection.execute(
            "SELECT updated_at FROM blocks WHERE id='block-pending'"
        ).fetchone()[0]

    preview = db.preview_scan_reset()
    assert preview["blocks_reset"] == 1
    with db.transaction() as connection:
        connection.execute(
            """UPDATE blocks SET status='ready', last_error='changed',
               updated_at='changed' WHERE id=?""",
            (block.id,),
        )
    with pytest.raises(ValueError, match="token"):
        db.reset_scan_derivatives(preview["token"])

    fresh = db.preview_scan_reset()
    assert fresh["blocks_reset"] == 1
    deleted = db.reset_scan_derivatives(fresh["token"])
    assert deleted["blocks_reset"] == 1
    with closing(db.connect()) as connection:
        reset_row = connection.execute(
            "SELECT status, last_error FROM blocks WHERE id=?", (block.id,)
        ).fetchone()
        pending_after = connection.execute(
            "SELECT status, last_error, updated_at FROM blocks WHERE id='block-pending'"
        ).fetchone()
    assert tuple(reset_row) == ("pending", None)
    assert tuple(pending_after) == ("pending", None, pending_before)


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


def test_accepted_audit_round_trips_from_independent_zstd_frames(tmp_path):
    archive = AuditArchive(tmp_path / "audit")
    first_payload = {"request": {"x": 1}, "response": "ok"}
    second_payload = {"request": {"x": 2}, "response": "also ok"}

    first = archive.append("run_1", first_payload, stage="translate")
    second = archive.append("run_1", second_payload, stage="translate")

    assert first.relative_path == second.relative_path
    assert second.offset == first.offset + first.compressed_length
    assert len(first.sha256) == 64
    assert archive.read(first) == first_payload
    assert archive.read(second) == second_payload


def test_database_externalizes_only_successful_automatic_audits(tmp_path):
    db, _, block, _ = _seed_database(tmp_path)
    accepted_id = db.record_audit_call(
        "scan-run",
        block.id,
        "translate",
        "fake",
        db.current_knowledge_version(),
        {"messages": [{"content": "large request"}]},
        "large response",
        {"translation": "译文"},
        True,
        1,
        12,
        None,
    )
    failed_id = db.record_audit_call(
        "scan-run",
        block.id,
        "translate",
        "fake",
        db.current_knowledge_version(),
        {"messages": [{"content": "failed request"}]},
        "failed response",
        None,
        False,
        2,
        24,
        "protocol failure",
    )
    manual_id = db.record_audit_call(
        "scan-run",
        block.id,
        "manual_review",
        "human",
        db.current_knowledge_version(),
        {"note": "keep in SQL"},
        "accepted by editor",
        {"choice": "A"},
        True,
        1,
        0,
        None,
    )

    with closing(db.connect()) as connection:
        accepted = connection.execute(
            "SELECT * FROM audit_calls WHERE id=?", (accepted_id,)
        ).fetchone()
        failed = connection.execute(
            "SELECT * FROM audit_calls WHERE id=?", (failed_id,)
        ).fetchone()
        manual = connection.execute(
            "SELECT * FROM audit_calls WHERE id=?", (manual_id,)
        ).fetchone()

    assert accepted["request_json"] == "{}"
    assert accepted["raw_response"] == ""
    assert accepted["parsed_json"] is None
    assert accepted["archive_relative_path"].endswith("translate_scan-run.jsonl.zst")
    assert accepted["archive_offset"] >= 0
    assert accepted["archive_compressed_length"] > 0
    assert len(accepted["archive_sha256"]) == 64
    assert db.read_audit_payload(accepted_id) == {
        "request": {"messages": [{"content": "large request"}]},
        "raw_response": "large response",
        "parsed": {"translation": "译文"},
    }

    assert failed["archive_relative_path"] is None
    assert json.loads(failed["request_json"])["messages"][0]["content"] == "failed request"
    assert failed["raw_response"] == "failed response"
    assert manual["archive_relative_path"] is None
    assert json.loads(manual["request_json"]) == {"note": "keep in SQL"}
    assert manual["raw_response"] == "accepted by editor"


def test_existing_schema7_audit_table_gains_archive_locator_columns(tmp_path):
    root = tmp_path / "legacy-audit"
    path = root / "artifacts" / "parallel_v4" / "book.db"
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES('schema_version', '7');
            CREATE TABLE audit_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                block_id TEXT,
                purpose TEXT NOT NULL,
                model TEXT NOT NULL,
                knowledge_version INTEGER,
                request_json TEXT NOT NULL,
                raw_response TEXT NOT NULL,
                parsed_json TEXT,
                accepted INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 1,
                elapsed_ms INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL
            );
            """
        )

    V4Database(root)

    with closing(sqlite3.connect(path)) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(audit_calls)")
        }
    assert {
        "archive_relative_path",
        "archive_offset",
        "archive_compressed_length",
        "archive_sha256",
    } <= columns


def test_storage_budget_formula_and_batch_rollback(tmp_path, monkeypatch):
    budget = StorageBudget(source_bytes=1_000_000)
    assert budget.active_limit == 40_000_000 + 64 * 1024**2
    with pytest.raises(StorageBudgetExceeded):
        budget.check(budget.active_limit + 1)

    db, _, _, candidates = _seed_database(tmp_path)
    monkeypatch.setattr(StorageBudget, "FIXED_ALLOWANCE_BYTES", 0)
    item = _cluster("cluster-over-budget", candidates)

    with pytest.raises(StorageBudgetExceeded):
        db.persist_candidate_clusters("scan-run", [item])

    with closing(db.connect()) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_clusters WHERE id='cluster-over-budget'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_cluster_members WHERE cluster_id='cluster-over-budget'"
        ).fetchone()[0] == 0
