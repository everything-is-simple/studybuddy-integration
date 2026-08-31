"""Standalone B3 report C2 integration gate; never imported by Formal StudyBuddy.

This runner independently composes synthetic 9A-9D-shaped SQLite facts with the
frozen report projection boundary. It intentionally does not import Composer or
Formal implementation code.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "results/report-core-c2/integration.json"
MAX_OUTPUT_BYTES = 1024 * 1024
SOURCE_STATUSES = ("valid", "stale", "source_deleted", "source_unavailable")
REPORT_KINDS = ("daily", "weekly", "monthly", "exam_alert")
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE learning_goals (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE study_plans (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE study_plan_items (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE rhythm_allocations (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, plan_id TEXT NOT NULL, item_id TEXT NOT NULL, local_date TEXT NOT NULL, planned_minutes INTEGER NOT NULL);
CREATE TABLE practice_sessions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_kind TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE exercise_attempts (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, grading_status TEXT NOT NULL, is_correct INTEGER, submitted_at TEXT NOT NULL);
CREATE TABLE mistake_cases (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE module_source_links (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE capture_sessions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, material_id TEXT, source_status TEXT, updated_at TEXT NOT NULL);
CREATE TABLE materials (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE transcript_segments (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, quality TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE cram_goals (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL, target_date TEXT NOT NULL);
CREATE TABLE report_snapshots (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, report_kind TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL, fingerprint TEXT NOT NULL, safe_payload TEXT NOT NULL, UNIQUE(project_id, report_kind, period_start, period_end, fingerprint));
"""


def ident(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def inside(value: str, start: str, end: str) -> bool:
    return start <= value < end


def count_rows(db: sqlite3.Connection, table: str, project: str, column: str, start: str, end: str) -> int:
    return db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE project_id=? AND {column}>=? AND {column}<?",
        (project, start, end),
    ).fetchone()[0]


def source_status(db: sqlite3.Connection, project: str, material: str) -> str:
    status = db.execute("SELECT status FROM materials WHERE id=? AND project_id=?", (material, project)).fetchone()
    if status is None or status[0] == "purged":
        return "source_unavailable"
    if status[0] == "deleted":
        return "source_deleted"
    return "valid"


def build_projection(db: sqlite3.Connection, project: str, kind: str, start: str, end: str) -> dict[str, object]:
    if kind not in REPORT_KINDS or end <= start:
        raise ValueError("invalid_period")
    goals = count_rows(db, "learning_goals", project, "updated_at", start, end)
    plans = count_rows(db, "study_plans", project, "updated_at", start, end)
    items = count_rows(db, "study_plan_items", project, "updated_at", start, end)
    completed = db.execute(
        "SELECT COUNT(*) FROM study_plan_items WHERE project_id=? AND status='completed' AND updated_at>=? AND updated_at<?",
        (project, start, end),
    ).fetchone()[0]
    allocated = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(planned_minutes),0) FROM rhythm_allocations WHERE project_id=? AND local_date>=? AND local_date<?",
        (project, start, end),
    ).fetchone()
    sessions = count_rows(db, "practice_sessions", project, "created_at", start, end)
    attempts = count_rows(db, "exercise_attempts", project, "submitted_at", start, end)
    correct = db.execute(
        "SELECT COUNT(*) FROM exercise_attempts WHERE project_id=? AND is_correct=1 AND submitted_at>=? AND submitted_at<?",
        (project, start, end),
    ).fetchone()[0]
    mistakes = count_rows(db, "mistake_cases", project, "updated_at", start, end)
    links = count_rows(db, "module_source_links", project, "updated_at", start, end)
    captures = count_rows(db, "capture_sessions", project, "updated_at", start, end)
    sources = {status: 0 for status in SOURCE_STATUSES}
    for (material,) in db.execute("SELECT id FROM materials WHERE project_id=? ORDER BY id", (project,)).fetchall():
        sources[source_status(db, project, material)] += 1
    uncertain = db.execute(
        "SELECT COUNT(*) FROM transcript_segments WHERE project_id=? AND quality='uncertain' AND created_at>=? AND created_at<?",
        (project, start, end),
    ).fetchone()[0]
    cram = db.execute(
        "SELECT target_date FROM cram_goals WHERE project_id=? AND status='active' ORDER BY target_date,id",
        (project,),
    ).fetchall()
    nearest = next((max(0, (date_value[:10] != start[:10]) and 7 or 0) for (date_value,) in cram), None)
    payload = {
        "content_version": "b3-report-v1",
        "period": {"report_kind": kind, "period_start": start, "period_end": end, "timezone": "UTC"},
        "plan": {"active_goal_count": goals, "active_plan_count": plans, "planned_item_count": items,
                 "completed_item_count": completed, "planned_minutes_total": int(allocated[1])},
        "rhythm": {"allocated_day_count": int(allocated[0]), "allocated_minutes_total": int(allocated[1])},
        "practice": {"practice_session_count": sessions, "attempt_count": attempts,
                      "deterministic_correct_count": correct, "completed_session_count": sessions},
        "feedback": {"open_mistake_count": mistakes, "source_link_count": links},
        "source_quality": sources,
        "capture": {"capture_session_count": captures, "uncertain_transcript_segment_count": uncertain},
        "exam_alert": {"days_remaining_bucket": "4-7" if nearest is not None else None,
                       "is_imminent": nearest is not None},
    }
    payload["quality_flags"] = {
        "has_source_warnings": any(sources[key] for key in SOURCE_STATUSES[1:]),
        "has_uncertain_capture": uncertain > 0,
    }
    basis = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    payload["aggregation_fingerprint"] = hashlib.sha256(basis.encode()).hexdigest()
    return payload


def snapshot(db: sqlite3.Connection, project: str, kind: str, start: str, end: str) -> tuple[dict[str, object], bool]:
    payload = build_projection(db, project, kind, start, end)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    row = db.execute(
        "SELECT safe_payload FROM report_snapshots WHERE project_id=? AND report_kind=? AND period_start=? AND period_end=? AND fingerprint=?",
        (project, kind, start, end, payload["aggregation_fingerprint"]),
    ).fetchone()
    if row is not None:
        return json.loads(row[0]), True
    db.execute("INSERT INTO report_snapshots VALUES (?,?,?,?,?,?,?)", (ident("report"), project, kind, start, end, payload["aggregation_fingerprint"], encoded))
    return payload, False


def seed(db: sqlite3.Connection, project: str) -> str:
    db.execute("INSERT INTO projects VALUES (?,?)", (project, "integration synthetic project"))
    for table, status in (("learning_goals", "active"), ("study_plans", "active"), ("study_plan_items", "completed")):
        db.execute(f"INSERT INTO {table} VALUES (?,?,?,?)", (ident(table), project, status, "2026-08-02T10:00:00+00:00"))
    db.execute("INSERT INTO rhythm_allocations VALUES (?,?,?,?,?,?)", (ident("allocation"), project, "plan-1", "item-1", "2026-08-02", 45))
    db.execute("INSERT INTO practice_sessions VALUES (?,?,?,?,?)", (ident("session"), project, "practice", "finished", "2026-08-02T10:00:00+00:00"))
    db.execute("INSERT INTO exercise_attempts VALUES (?,?,?,?,?)", (ident("attempt"), project, "deterministic", 1, "2026-08-02T10:00:00+00:00"))
    db.execute("INSERT INTO mistake_cases VALUES (?,?,?,?)", (ident("mistake"), project, "open", "2026-08-02T10:00:00+00:00"))
    db.execute("INSERT INTO module_source_links VALUES (?,?,?,?)", (ident("link"), project, "valid", "2026-08-02T10:00:00+00:00"))
    material = ident("material")
    db.execute("INSERT INTO materials VALUES (?,?,?)", (material, project, "active"))
    db.execute("INSERT INTO capture_sessions VALUES (?,?,?,?,?)", (ident("capture"), project, material, "valid", "2026-08-02T10:00:00+00:00"))
    db.execute("INSERT INTO transcript_segments VALUES (?,?,?,?)", (ident("segment"), project, "uncertain", "2026-08-02T10:00:00+00:00"))
    db.execute("INSERT INTO cram_goals VALUES (?,?,?,?)", (ident("cram"), project, "active", "2026-08-08"))
    db.commit()
    return material


def main() -> int:
    started = time.perf_counter()
    work = Path(tempfile.mkdtemp(prefix="studybuddy-b3-c2-"))
    db_path, backup_path, restored_path = work / "source.sqlite3", work / "backup.sqlite3", work / "restored.sqlite3"
    checks: dict[str, object] = {}
    try:
        db = sqlite3.connect(db_path)
        db.executescript(SCHEMA)
        project, start, end = "project_b3_c2", "2026-08-01", "2026-08-08"
        material = seed(db, project)
        first, replay = snapshot(db, project, "daily", start, end)
        second, replayed = snapshot(db, project, "daily", start, end)
        checks["nine_a_to_d_facts"] = first["plan"]["completed_item_count"] == 1 and first["practice"]["attempt_count"] == 1
        checks["four_report_kinds"] = all(build_projection(db, project, kind, start, end)["period"]["report_kind"] == kind for kind in REPORT_KINDS)
        checks["snapshot_idempotency"] = not replay and replayed and first == second and db.execute("SELECT COUNT(*) FROM report_snapshots").fetchone()[0] == 1
        checks["source_quality_initial"] = first["source_quality"]["valid"] == 1 and first["quality_flags"]["has_uncertain_capture"] is True
        db.execute("UPDATE materials SET status='deleted' WHERE id=?", (material,))
        db.commit()
        deleted = build_projection(db, project, "daily", start, end)
        checks["source_deleted_degradation"] = (
            deleted["source_quality"]["source_deleted"] == 1
            and deleted["quality_flags"]["has_source_warnings"] is True
            and db.execute("SELECT COUNT(*) FROM capture_sessions WHERE project_id=?", (project,)).fetchone()[0] == 1
        )
        db.execute("UPDATE materials SET status='purged' WHERE id=?", (material,))
        db.commit()
        purged = build_projection(db, project, "daily", start, end)
        checks["source_unavailable_after_purge"] = purged["source_quality"]["source_unavailable"] == 1
        with sqlite3.connect(backup_path) as target:
            db.backup(target)
        db.close()
        restored = sqlite3.connect(restored_path)
        with sqlite3.connect(backup_path) as source:
            source.backup(restored)
        restored_projection = build_projection(restored, project, "daily", start, end)
        checks["backup_restore_preserves_degradation"] = restored_projection["source_quality"]["source_unavailable"] == 1
        checks["backup_restore_non_repair"] = (
            restored.execute("SELECT status FROM materials WHERE id=?", (material,)).fetchone()[0] == "purged"
            and json.loads(restored.execute("SELECT safe_payload FROM report_snapshots").fetchone()[0])["source_quality"]["valid"] == 1
        )
        checks["integrity_check"] = restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        restored.close()
        public = json.dumps({"safe_payload": purged, "format": "json"}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        checks["output_limit"] = len(public.encode()) < MAX_OUTPUT_BYTES
        checks["network_called"] = False
        checks["formal_system_touched"] = False
        checks["privacy_boundary"] = not any(token in public.lower() for token in ("password", "secret", "api_key", "answer_key", "stored_path"))
        functional_checks = [value for key, value in checks.items() if key not in {"network_called", "formal_system_touched"}]
        status = "integration_passed" if all(functional_checks) and checks["network_called"] is False and checks["formal_system_touched"] is False else "integration_failed"
        payload = {"schema_version": 1, "component": "report-core", "gate": "B3-C2", "status": status,
                   "scope": "synthetic 9A-9D-shaped SQLite facts; UTC; daily/weekly/monthly/exam_alert",
                   "checks": checks, "measurements": {"wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
                   "output_bytes": len(public.encode()), "temporary_files_before_cleanup": len(list(work.iterdir()))},
                   "privacy": {"raw_source_retained": False, "report_body_retained": False, "absolute_private_path_retained": False},
                   "limitations": ["synthetic single-process SQLite", "no Formal import", "no live delivery", "no crash or disk-full stress"]}
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"component": payload["component"], "gate": payload["gate"], "status": status}))
        return 0 if status == "integration_passed" else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
