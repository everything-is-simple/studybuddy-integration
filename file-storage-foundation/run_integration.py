from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PARSER_SRC = Path("H:/studybuddy-composer/components/backend-file-parsers/src")
sys.path.insert(0, str(PARSER_SRC))
from backend_file_parsers import parse_file

FIXTURES = Path("H:/studybuddy-test/fixtures/kaobuddy-foundation")
RUN_ROOT = Path("H:/studybuddy-test/runs/file-storage-foundation")
ARTIFACT = Path("H:/studybuddy-test/artifacts/file-storage-foundation/latest.json")
DB = RUN_ROOT / "foundation.sqlite3"
BACKUP = RUN_ROOT / "foundation-backup.sqlite3"
RESTORED = RUN_ROOT / "foundation-restored.sqlite3"

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE materials (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, original_name TEXT NOT NULL, source_sha256 TEXT NOT NULL, stored_path TEXT NOT NULL, media_type TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE extractions (id TEXT PRIMARY KEY, material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE, parser_id TEXT NOT NULL, parser_version TEXT NOT NULL, status TEXT NOT NULL, text TEXT NOT NULL, warnings_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE text_spans (id TEXT PRIMARY KEY, extraction_id TEXT NOT NULL REFERENCES extractions(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, span_kind TEXT NOT NULL, label TEXT NOT NULL, text TEXT NOT NULL);
"""
CASES = ["sample.txt", "sample.md", "chinese.txt", "empty.txt", "sample.pdf", "corrupt.pdf", "sample.docx", "empty.docx", "corrupt.docx", "sample.pptx", "empty.pptx", "corrupt.pptx", "sample.rtf", "sample.doc", "sample.ppt"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 2000")
    return connection


def read_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ["projects", "materials", "extractions", "text_spans"]}


def main() -> None:
    if DB.exists(): DB.unlink()
    if BACKUP.exists(): BACKUP.unlink()
    if RESTORED.exists(): RESTORED.unlink()
    originals = RUN_ROOT / "originals"
    if originals.exists(): shutil.rmtree(originals)
    originals.mkdir(parents=True)
    connection = connect(DB)
    connection.executescript(SCHEMA)
    connection.execute("PRAGMA journal_mode=WAL")
    project_id = "project_synthetic"
    created = now()
    connection.execute("INSERT INTO projects VALUES (?, ?, ?)", (project_id, "Synthetic integration project", created))
    records = []
    for name in CASES:
        source = FIXTURES / name
        target = originals / name
        shutil.copy2(source, target)
        source_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        parsed = parse_file(source)
        material_id, extraction_id = f"material_{uuid.uuid4().hex}", f"extraction_{uuid.uuid4().hex}"
        connection.execute("INSERT INTO materials VALUES (?, ?, ?, ?, ?, ?, ?)", (material_id, project_id, name, source_hash, str(target), source.suffix.lower(), created))
        connection.execute("INSERT INTO extractions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (extraction_id, material_id, parsed.parser_id, parsed.parser_version, parsed.status, parsed.text, json.dumps(parsed.warnings, ensure_ascii=False), created))
        for span in parsed.spans:
            connection.execute("INSERT INTO text_spans VALUES (?, ?, ?, ?, ?, ?)", (f"span_{uuid.uuid4().hex}", extraction_id, span.ordinal, span.kind, span.label, span.text))
        records.append({"fixture": name, "status": parsed.status, "sha256": source_hash, "stored_exists": target.exists(), "text_length": len(parsed.text), "span_count": len(parsed.spans), "error_code": parsed.error_code})
    connection.commit()
    before = read_counts(connection)
    rollback_ok = False
    try:
        with connection:
            connection.execute("INSERT INTO materials VALUES (?, ?, ?, ?, ?, ?, ?)", ("rollback-material", project_id, "rollback.txt", "hash", str(originals / "rollback.txt"), ".txt", created))
            connection.execute("INSERT INTO extractions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("rollback-extraction", "rollback-material", "test", "1", "success", "should rollback", "[]", created))
            connection.execute("INSERT INTO text_spans VALUES (?, ?, ?, ?, ?, ?)", ("rollback-span", "missing-extraction", 1, "document", "bad", "must fail"))
    except sqlite3.IntegrityError:
        rollback_ok = read_counts(connection) == before
    connection.close()
    reopened = connect(DB)
    reopened_counts = read_counts(reopened)
    integrity = reopened.execute("PRAGMA integrity_check").fetchone()[0]
    assert reopened_counts == before and integrity == "ok" and rollback_ok
    for record in records:
        assert hashlib.sha256((originals / record["fixture"]).read_bytes()).hexdigest() == record["sha256"]
    backup_target = sqlite3.connect(BACKUP)
    reopened.backup(backup_target)
    backup_target.close(); reopened.close()
    restored = connect(RESTORED)
    with sqlite3.connect(BACKUP) as backup_source:
        backup_source.backup(restored)
    restored_counts = read_counts(restored)
    restored_integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
    assert restored_counts == before and restored_integrity == "ok"
    restored.close()
    payload = {"component": "file-storage-foundation", "status": "integration_passed", "python": sys.version.split()[0], "command": f"{sys.executable} {Path(__file__).name}", "network": {"required": False, "called": False}, "formal_system_touched": False, "db_location": str(DB), "original_root": str(originals), "journal_mode": "wal", "records": records, "transaction_rollback": {"passed": rollback_ok, "counts_before": before, "counts_after": read_counts(connect(DB))}, "reopen": {"passed": True, "counts": reopened_counts, "integrity_check": integrity}, "backup_restore": {"passed": True, "counts": restored_counts, "integrity_check": restored_integrity}, "limitations": ["single-process synthetic run", "no crash/disk-full/network-share stress", "parser text output is stored in test database only"]}
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"component": payload["component"], "status": payload["status"], "counts": before, "rollback": rollback_ok}))


if __name__ == "__main__":
    main()
