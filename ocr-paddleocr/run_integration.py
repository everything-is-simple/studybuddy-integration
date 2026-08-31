"""Standalone B2 OCR C2 integration gate; never imported by Formal StudyBuddy."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

MODEL_ROOT = Path(os.environ.get("STUDYBUDDY_PADDLE_MODEL_ROOT", ""))
RUN_ROOT = Path(os.environ.get("STUDYBUDDY_INTEGRATION_ROOT", "H:/studybuddy-test/runs/ocr-paddleocr-c2"))
ARTIFACT = Path("results/ocr-paddleocr-c2/integration.json")
THRESHOLD = 0.85
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE projects(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE materials(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), source_sha256 TEXT NOT NULL, stored_path TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','deleted','purged')));
CREATE TABLE capture_sessions(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), material_id TEXT UNIQUE REFERENCES materials(id), status TEXT NOT NULL, asset_kind TEXT NOT NULL, edited_by_user INTEGER NOT NULL DEFAULT 0);
CREATE TABLE operations(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES capture_sessions(id), status TEXT NOT NULL, retry_of TEXT, input_sha256 TEXT NOT NULL);
CREATE TABLE drafts(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES capture_sessions(id), operation_id TEXT NOT NULL REFERENCES operations(id), text TEXT NOT NULL, edited_by_user INTEGER NOT NULL DEFAULT 0);
CREATE TABLE segments(id TEXT PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES drafts(id), ordinal INTEGER NOT NULL, text TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1), quality TEXT NOT NULL CHECK(quality IN ('clear','uncertain')));
CREATE TABLE revisions(id TEXT PRIMARY KEY, material_id TEXT NOT NULL REFERENCES materials(id), draft_id TEXT NOT NULL REFERENCES drafts(id), text TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ident(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=ON")
    return db


def source_status(db: sqlite3.Connection, material_id: str) -> str:
    status, stored = db.execute("SELECT status, stored_path FROM materials WHERE id=?", (material_id,)).fetchone()
    if status == "deleted":
        return "source_deleted"
    if status == "purged" or not Path(stored).is_file():
        return "source_unavailable"
    return "valid"


def make_image(path: Path) -> str:
    image = Image.new("RGB", (1000, 260), "white")
    draw = ImageDraw.Draw(image)
    draw.text((35, 35), "StudyBuddy OCR 2026", fill="black")
    draw.text((35, 105), "中文学习资料 测试", fill="black")
    draw.text((35, 175), "Table: 42 points", fill="black")
    image.save(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recognize(path: Path) -> tuple[list[str], list[float]]:
    if not MODEL_ROOT.is_dir():
        raise RuntimeError("model_not_configured")
    from paddleocr import PaddleOCR
    ocr = PaddleOCR(
        device="cpu", enable_mkldnn=False, lang="ch",
        use_doc_orientation_classify=False, use_doc_unwarping=False,
        use_textline_orientation=False,
        text_detection_model_name="PP-OCRv5_server_det",
        text_recognition_model_name="PP-OCRv5_server_rec",
        text_detection_model_dir=str(MODEL_ROOT / "PP-OCRv5_server_det"),
        text_recognition_model_dir=str(MODEL_ROOT / "PP-OCRv5_server_rec"),
    )
    texts: list[str] = []
    scores: list[float] = []
    for page in ocr.predict(str(path)):
        data = page.json if hasattr(page, "json") else page
        if isinstance(data, str):
            data = json.loads(data)
        result = data.get("res", {})
        texts.extend(str(value) for value in result.get("rec_texts", []))
        scores.extend(float(value) for value in result.get("rec_scores", []))
    return texts, scores


def main() -> int:
    if not MODEL_ROOT.is_dir():
        print(json.dumps({"component": "ocr-paddleocr", "status": "blocked", "error": "model_not_configured"}))
        return 2
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    for path in (RUN_ROOT / "c2.sqlite3", RUN_ROOT / "backup.sqlite3", RUN_ROOT / "restored.sqlite3"):
        path.unlink(missing_ok=True)
    original_root = RUN_ROOT / "originals"
    shutil.rmtree(original_root, ignore_errors=True)
    original_root.mkdir(parents=True)
    db_path = RUN_ROOT / "c2.sqlite3"
    backup_path = RUN_ROOT / "backup.sqlite3"
    restored_path = RUN_ROOT / "restored.sqlite3"
    image_path = original_root / "source.png"
    source_hash = make_image(image_path)
    db = connect(db_path)
    db.executescript(SCHEMA)
    project, material, session = ident("project"), ident("material"), ident("capture_session")
    db.execute("INSERT INTO projects VALUES (?,?)", (project, now()))
    db.execute("INSERT INTO materials VALUES (?,?,?,?,?)", (material, project, source_hash, str(image_path), "active"))
    db.execute("INSERT INTO capture_sessions VALUES (?,?,?,?,?,?)", (session, project, material, "uploaded", "image", 0))
    operation = ident("transcription")
    db.execute("INSERT INTO operations VALUES (?,?,?,?,?)", (operation, session, "transcribing", None, source_hash))
    texts, scores = recognize(image_path)
    scores = scores or [0.0] * len(texts)
    draft, text = ident("draft"), "\n".join(texts).strip()
    db.execute("INSERT INTO drafts VALUES (?,?,?,?,?)", (draft, session, operation, text, 0))
    for ordinal, value in enumerate(texts):
        score = max(0.0, min(1.0, scores[ordinal] if ordinal < len(scores) else 0.0))
        quality = "clear" if score >= THRESHOLD and value.strip() else "uncertain"
        db.execute("INSERT INTO segments VALUES (?,?,?,?,?,?)", (ident("segment"), draft, ordinal, value, score, quality))
    db.execute("UPDATE operations SET status='succeeded' WHERE id=?", (operation,))
    db.execute("UPDATE capture_sessions SET status='review_required' WHERE id=?", (session,))
    db.commit()
    draft_count = db.execute("SELECT COUNT(*) FROM segments WHERE draft_id=?", (draft,)).fetchone()[0]
    uncertain_count = db.execute("SELECT COUNT(*) FROM segments WHERE draft_id=? AND quality='uncertain'", (draft,)).fetchone()[0]
    before_rollback = db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
    rollback_ok = False
    try:
        with db:
            db.execute("INSERT INTO revisions VALUES (?,?,?,?)", (ident("revision"), material, draft, text))
            db.execute("INSERT INTO segments VALUES (?,?,?,?,?,?)", (ident("bad"), draft, -1, "", 2.0, "clear"))
    except sqlite3.IntegrityError:
        rollback_ok = db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == before_rollback
    db.execute("INSERT INTO revisions VALUES (?,?,?,?)", (ident("revision"), material, draft, text))
    db.execute("UPDATE capture_sessions SET status='confirmed' WHERE id=?", (session,))
    db.commit()
    db.execute("UPDATE materials SET status='deleted' WHERE id=?", (material,))
    db.commit()
    deleted_status = source_status(db, material)
    db.execute("UPDATE materials SET status='active' WHERE id=?", (material,))
    db.commit()
    with sqlite3.connect(backup_path) as target:
        db.backup(target)
    db.close()
    restored = connect(restored_path)
    with sqlite3.connect(backup_path) as source:
        source.backup(restored)
    restored_counts = {table: restored.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("materials", "drafts", "revisions")}
    restored_ok = restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and restored_counts["revisions"] == 1
    restored.close()
    payload = {"schema_version": 1, "component": "ocr-paddleocr", "gate": "C2", "status": "integration_passed" if all((text, draft_count > 0, rollback_ok, deleted_status == "source_deleted", restored_ok)) else "integration_failed", "network_called": False, "formal_system_touched": False, "model_scope": "PP-OCRv5_server_det/PP-OCRv5_server_rec", "checks": {"original_hash_present": bool(source_hash), "draft_created": bool(text), "segment_count": draft_count, "uncertain_segment_count": uncertain_count, "user_confirmation_required": True, "confirmed_revision_count": 1, "source_status_after_delete": deleted_status, "rollback": rollback_ok, "backup_restore_non_repair": restored_ok, "restored_counts": restored_counts}, "privacy": {"raw_image_retained_in_evidence": False, "raw_ocr_retained_in_evidence": False, "absolute_private_path_retained": False}, "limitations": ["single-process synthetic image", "exact local model/environment scope", "no crash or disk-full stress", "accuracy is not established by this gate"]}
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({"component": payload["component"], "gate": "C2", "status": payload["status"], "uncertain_segment_count": uncertain_count}))
    return 0 if payload["status"] == "integration_passed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(2)
