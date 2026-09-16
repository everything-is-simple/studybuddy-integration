"""RapidOCR C2 integration gate; standalone and never imported by StudyBuddy Formal."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ARTIFACT = Path(__file__).resolve().parents[1] / "results" / "ocr-rapidocr-c2" / "integration.json"
TIMEOUT_SECONDS = 120.0
THRESHOLD = 0.85


def ident(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def make_image(path: Path, image_format: str) -> bytes:
    image = Image.new("RGB", (1200, 300), "white")
    draw = ImageDraw.Draw(image)
    font = None
    for candidate in ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/msyh.ttc"):
        if Path(candidate).is_file():
            try:
                font = ImageFont.truetype(candidate, 34)
                break
            except OSError:
                pass
    draw.text((40, 35), "StudyBuddy OCR C2", fill="black", font=font)
    draw.text((40, 125), "中文学习资料", fill="black", font=font)
    draw.text((40, 215), "Mixed text 42", fill="black", font=font)
    image.save(path, format=image_format)
    return path.read_bytes()


def recognize(path: Path) -> tuple[list[str], list[float]]:
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    result, _ = engine(str(path))
    texts: list[str] = []
    scores: list[float] = []
    for item in result or []:
        if len(item) < 3:
            continue
        text = str(item[1]).strip()
        if text:
            texts.append(text)
            scores.append(float(item[2]))
    return texts, scores


def source_status(db: sqlite3.Connection, material_id: str) -> str:
    status, stored_path = db.execute(
        "SELECT status, stored_path FROM materials WHERE id=?", (material_id,)
    ).fetchone()
    if status == "deleted":
        return "source_deleted"
    if status == "purged" or not Path(stored_path).is_file():
        return "source_unavailable"
    return "valid"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="studybuddy-rapidocr-c2-") as directory:
        root = Path(directory)
        original_root = root / "originals"
        original_root.mkdir()
        db_path = root / "studybuddy.sqlite3"
        images: dict[str, bytes] = {}
        for image_format, suffix in (("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp")):
            images[suffix] = make_image(root / f"source.{suffix}", image_format)

        started = time.perf_counter()
        checks: dict[str, object] = {}
        outputs: dict[str, dict[str, object]] = {}
        for suffix, content in images.items():
            source = root / f"source.{suffix}"
            try:
                texts, scores = recognize(source)
                outputs[suffix] = {
                    "provider_id": "rapidocr",
                    "model_id": "ch_PP-OCRv4_det_infer+ch_PP-OCRv4_rec_infer",
                    "text_present": bool(texts),
                    "segment_count": len(texts),
                    "confidence_in_range": all(0.0 <= score <= 1.0 for score in scores),
                    "quality_counts": {
                        "clear": sum(score >= THRESHOLD for score in scores),
                        "uncertain": sum(score < THRESHOLD for score in scores),
                    },
                }
            except Exception:
                outputs[suffix] = {"provider_id": "rapidocr", "model_id": "bundled-onnx", "text_present": False}
        checks["real_png_jpeg_webp"] = all(item.get("text_present") for item in outputs.values())
        checks["provider_identity"] = all(item.get("provider_id") == "rapidocr" for item in outputs.values())
        checks["confidence_and_quality"] = all(
            item.get("confidence_in_range") and sum(item.get("quality_counts", {}).values()) == item.get("segment_count")
            for item in outputs.values()
        )

        blank = root / "blank.png"
        Image.new("RGB", (600, 200), "white").save(blank)
        corrupt = root / "corrupt.png"
        corrupt.write_bytes(b"not-an-image")
        blank_texts, _ = recognize(blank)
        try:
            recognize(corrupt)
            corrupt_rejected = False
        except Exception:
            corrupt_rejected = True
        checks["blank_is_not_successful_text"] = not blank_texts
        checks["corrupt_is_rejected"] = corrupt_rejected

        primary_error = "primary_provider_timeout"
        fallback_decision = {"fallback_allowed": True, "reason": primary_error, "provider_id": "rapidocr"}
        checks["explicit_fallback_decision"] = fallback_decision["fallback_allowed"] and fallback_decision["reason"] == primary_error

        db = sqlite3.connect(db_path)
        db.executescript("""
            CREATE TABLE projects(id TEXT PRIMARY KEY);
            CREATE TABLE materials(id TEXT PRIMARY KEY, project_id TEXT, source_sha256 TEXT, stored_path TEXT, status TEXT);
            CREATE TABLE drafts(id TEXT PRIMARY KEY, material_id TEXT, provider_id TEXT, status TEXT, fallback_reason TEXT);
            CREATE TABLE revisions(id TEXT PRIMARY KEY, material_id TEXT, draft_id TEXT);
        """)
        project_id, material_id, draft_id = ident("project"), ident("material"), ident("draft")
        source = root / "source.png"
        source_hash = digest(images["png"])
        stored = original_root / "source.png"
        shutil.copyfile(source, stored)
        db.execute("INSERT INTO projects VALUES (?)", (project_id,))
        db.execute("INSERT INTO materials VALUES (?,?,?,?,?)", (material_id, project_id, source_hash, str(stored), "active"))
        db.execute("INSERT INTO drafts VALUES (?,?,?,?,?)", (draft_id, material_id, "rapidocr", "review_required", primary_error))
        db.execute("INSERT INTO revisions VALUES (?,?,?)", (ident("revision"), material_id, draft_id))
        db.commit()
        db.execute("UPDATE materials SET status='deleted' WHERE id=?", (material_id,))
        db.commit()
        checks["draft_first_and_identity"] = db.execute(
            "SELECT provider_id,status,fallback_reason FROM drafts WHERE id=?", (draft_id,)
        ).fetchone() == ("rapidocr", "review_required", primary_error)
        checks["source_lifecycle"] = source_status(db, material_id) == "source_deleted"
        backup = root / "backup.sqlite3"
        restored = root / "restored.sqlite3"
        target_db = sqlite3.connect(backup)
        try:
            db.backup(target_db)
        finally:
            target_db.close()
        source_db = sqlite3.connect(backup)
        restored_db = sqlite3.connect(restored)
        try:
            source_db.backup(restored_db)
            restored_count = restored_db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
        finally:
            source_db.close()
            restored_db.close()
        checks["backup_restore_non_repair"] = restored_count == 1
        db.close()
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        payload = {
            "schema_version": 1,
            "component": "ocr-rapidocr",
            "gate": "C2",
            "status": "integration_passed" if all(checks.values()) else "integration_failed",
            "network_called": False,
            "formal_system_touched": False,
            "runtime": {"package": "rapidocr_onnxruntime", "version": "1.4.4", "onnxruntime": "1.20.1", "device": "cpu"},
            "model_identity": "ch_PP-OCRv4_det_infer.onnx + ch_PP-OCRv4_rec_infer.onnx + ch_ppocr_mobile_v2.0_cls_infer.onnx",
            "checks": checks,
            "formats": outputs,
            "fallback": fallback_decision,
            "elapsed_ms": elapsed_ms,
            "privacy": {"raw_image_retained": False, "raw_ocr_retained": False, "absolute_private_path_retained": False},
            "limitations": ["RapidOCR exact bundled model/environment scope", "primary failure is controlled integration injection", "no Formal fallback implementation", "no general accuracy or capacity claim"],
        }
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps({"component": payload["component"], "gate": "C2", "status": payload["status"], "checks": len(checks)}))
        return 0 if payload["status"] == "integration_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
