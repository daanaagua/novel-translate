from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

import main as cli
from src.core.v5_exporter import V5BookExporter


def _fingerprint(blocks: list[tuple[str, int, str]]) -> str:
    digest = hashlib.sha256()
    for block_id, global_index, source_hash in blocks:
        digest.update(block_id.encode())
        digest.update(b"\0")
        digest.update(source_hash.encode())
        digest.update(b"\0")
        digest.update(str(global_index).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _project(tmp_path: Path):
    root = tmp_path / "projects" / "sample"
    root.mkdir(parents=True)
    (root / "config.yaml").write_text(
        "title: Sample Book\nauthor: Example Author\nlanguage: zh-CN\n",
        encoding="utf-8",
    )
    return SimpleNamespace(
        book_id="sample",
        root_dir=root,
        config_file=root / "config.yaml",
    )


def _store(project, *, missing: bool = False, corrupt_hash: bool = False) -> Path:
    path = project.root_dir / "artifacts" / "translator_v5" / "book.db"
    path.parent.mkdir(parents=True)
    blocks = [
        ("b1", 0, "hash-1"),
        ("b2", 1, "hash-2"),
        ("b3", 2, "hash-3"),
    ]
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE book_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE book_blocks(
                block_id TEXT PRIMARY KEY,
                global_index INTEGER NOT NULL UNIQUE,
                chapter_id TEXT NOT NULL,
                chapter_title TEXT,
                block_index INTEGER NOT NULL,
                source_text TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                token_count INTEGER NOT NULL
            );
            CREATE TABLE translations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                block_id TEXT NOT NULL,
                window_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                source_hash TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        db.execute(
            "INSERT INTO book_meta(key, value) VALUES('source_fingerprint', ?)",
            (_fingerprint(blocks),),
        )
        db.executemany(
            """
            INSERT INTO book_blocks VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("b1", 0, "ch-1", "第一章", 0, "Source one.", "hash-1", 3),
                ("b2", 1, "ch-1", "第一章", 1, "Source two.", "hash-2", 3),
                ("b3", 2, "ch-2", "第二章", 0, "Source three.", "hash-3", 3),
            ],
        )
        translations = [
            ("b1", "w1", 1, "bad-hash" if corrupt_hash else "hash-1", "译文一。", "completed", 1),
            ("b2", "w1", 1, "hash-2", "译文二。", "completed_with_warnings", 1),
        ]
        if not missing:
            translations.append(("b3", "w2", 1, "hash-3", "译文三。", "completed", 1))
        db.executemany(
            "INSERT INTO translations(block_id, window_id, version, source_hash, text, status, active) VALUES(?, ?, ?, ?, ?, ?, ?)",
            translations,
        )
    return path


def test_v5_exporter_builds_ordered_bom_txt_and_valid_epub(tmp_path):
    project = _project(tmp_path)
    _store(project)

    result = V5BookExporter(project).export_v5()

    raw = result.txt_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert text.index("第一章") < text.index("译文一。") < text.index("第二章") < text.index("译文三。")
    assert result.chapter_count == 2
    assert result.chunk_count == 3
    with ZipFile(result.epub_path) as archive:
        assert archive.read("mimetype") == b"application/epub+zip"
        assert "OEBPS/chapter-002.xhtml" in archive.namelist()


@pytest.mark.parametrize(
    ("missing", "corrupt_hash", "message"),
    [
        (True, False, "1 个文本块没有活动译文"),
        (False, True, "1 个活动译文的原文哈希不匹配"),
    ],
)
def test_v5_exporter_strict_mode_rejects_incomplete_or_stale_translations(
    tmp_path, missing, corrupt_hash, message
):
    project = _project(tmp_path)
    _store(project, missing=missing, corrupt_hash=corrupt_hash)

    with pytest.raises(ValueError, match=message):
        V5BookExporter(project).export_v5()


def test_v5_exporter_can_emit_an_explicit_incomplete_draft(tmp_path):
    project = _project(tmp_path)
    _store(project, missing=True)

    result = V5BookExporter(project).export_v5(allow_incomplete=True)

    assert result.chunk_count == 3
    text = result.txt_path.read_text(encoding="utf-8-sig")
    assert "译文二。" in text
    assert "译文三。" not in text


def test_export_v5_cli_routes_explicit_incomplete_mode(monkeypatch, tmp_path):
    project = _project(tmp_path)
    calls = {}

    class FakeExporter:
        def __init__(self, received):
            assert received is project

        def export_v5(self, output_dir=None, allow_incomplete=False):
            calls.update(output_dir=output_dir, allow_incomplete=allow_incomplete)
            return SimpleNamespace(
                txt_path=Path("draft.txt"),
                epub_path=Path("draft.epub"),
            )

    monkeypatch.setattr(cli, "_load_project_or_error", lambda _book_id: project)
    monkeypatch.setattr(cli, "V5BookExporter", FakeExporter, raising=False)

    result = cli.main(
        ["export-v5", "sample", "--allow-incomplete", "--output-dir", "drafts"]
    )

    assert result == 0
    assert calls == {"output_dir": "drafts", "allow_incomplete": True}
