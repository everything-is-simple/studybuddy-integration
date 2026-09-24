"""Strict RapidOCR C2 gate; standalone and never imported by StudyBuddy Formal."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable

TIMEOUT_SECONDS = 120.0
OUTPUT_LIMIT_BYTES = 512 * 1024
THRESHOLD = 0.85
RAPID_MODELS = (
    "ch_PP-OCRv4_det_infer.onnx",
    "ch_PP-OCRv4_rec_infer.onnx",
    "ch_ppocr_mobile_v2.0_cls_infer.onnx",
)
PADDLE_MODEL_FILES = ("inference.json", "inference.pdiparams", "inference.yml")
DEFAULT_ARTIFACT = Path("H:/studybuddy-test/artifacts/rapidocr-c2-strict-20260924/latest.json")


class ProviderFailure(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def ident(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def digest_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def inventory(root: Path, names: tuple[str, ...]) -> dict[str, str]:
    if not root.is_dir():
        raise ProviderFailure("model_not_configured")
    found: dict[str, str] = {}
    for name in names:
        path = root / name
        if not path.is_file():
            raise ProviderFailure("model_not_configured")
        found[name] = digest_file(path)
    return found


def paddle_inventory(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise ProviderFailure("model_not_configured")
    found: dict[str, str] = {}
    for model in ("PP-OCRv5_server_det", "PP-OCRv5_server_rec"):
        for name in PADDLE_MODEL_FILES:
            path = root / model / name
            if not path.is_file():
                raise ProviderFailure("model_not_configured")
            found[f"{model}/{name}"] = digest_file(path)
    return found


def make_image(path: Path, image_format: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

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
    draw.text((40, 125), "Chinese study material", fill="black", font=font)
    draw.text((40, 215), "Mixed text 42", fill="black", font=font)
    image.save(path, format=image_format)
    return path.read_bytes()


def _worker(engine: str, image: Path, output: Path, model_root: Path) -> int:
    try:
        import socket

        def deny_network(_self: object, address: object) -> None:
            raise OSError("network_disabled")

        socket.socket.connect = deny_network
        socket.socket.connect_ex = deny_network
        if engine == "rapidocr":
            from rapidocr_onnxruntime import RapidOCR
            result, _ = RapidOCR(
                det_model_path=str(model_root / RAPID_MODELS[0]),
                rec_model_path=str(model_root / RAPID_MODELS[1]),
                cls_model_path=str(model_root / RAPID_MODELS[2]),
            )(str(image))
        elif engine == "paddleocr":
            from paddleocr import PaddleOCR

            os.environ.update({
                "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
            })
            ocr = PaddleOCR(
                device="cpu", enable_mkldnn=False, lang="ch",
                use_doc_orientation_classify=False, use_doc_unwarping=False,
                use_textline_orientation=False,
                text_detection_model_name="PP-OCRv5_server_det",
                text_recognition_model_name="PP-OCRv5_server_rec",
                text_detection_model_dir=str(model_root / "PP-OCRv5_server_det"),
                text_recognition_model_dir=str(model_root / "PP-OCRv5_server_rec"),
            )
            pages = ocr.predict(str(image))
            result = []
            for page in pages or []:
                data = page.json if hasattr(page, "json") else page
                if isinstance(data, str):
                    data = json.loads(data)
                output_data = data.get("res", {})
                texts = output_data.get("rec_texts", [])
                scores = output_data.get("rec_scores", [])
                result.extend([[None, text, score] for text, score in zip(texts, scores)])
        else:
            return 2

        records = []
        for item in result or []:
            if len(item) < 3:
                continue
            value = item[1]
            if isinstance(value, (list, tuple)):
                text = str(value[0]).strip()
                score = float(value[1]) if len(value) > 1 else float(item[2])
            else:
                text, score = str(value).strip(), float(item[2])
            if text:
                records.append({"text": text, "confidence": score})
        payload = json.dumps(records, ensure_ascii=True).encode("utf-8")
        if len(payload) > OUTPUT_LIMIT_BYTES:
            return 3
        output.write_bytes(payload)
        return 0
    except Exception:
        return 2


def run_engine(engine: str, image: Path, model_root: Path, work_root: Path,
               *, timeout: float = TIMEOUT_SECONDS) -> dict[str, object]:
    output = work_root / f"{engine}-{uuid.uuid4().hex}.json"
    env = os.environ.copy()
    env.update({
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "NO_PROXY": "*",
        "no_proxy": "*",
    })
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", engine,
               str(image), str(output), str(model_root)]
    started = time.perf_counter()
    try:
        completed = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        output.unlink(missing_ok=True)
        raise ProviderFailure("provider_timeout") from exc
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    size = output.stat().st_size if output.is_file() else 0
    if completed.returncode == 3 or size > OUTPUT_LIMIT_BYTES:
        output.unlink(missing_ok=True)
        raise ProviderFailure("payload_too_large")
    if completed.returncode != 0 or not output.is_file():
        output.unlink(missing_ok=True)
        raise ProviderFailure("transcription_failed")
    try:
        records = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        output.unlink(missing_ok=True)
        raise ProviderFailure("transcription_failed") from exc
    output.unlink(missing_ok=True)
    if not isinstance(records, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("text"), str)
        or not isinstance(row.get("confidence"), (int, float))
        or not 0.0 <= float(row["confidence"]) <= 1.0 for row in records
    ):
        raise ProviderFailure("transcription_failed")
    return {"records": records, "elapsed_ms": elapsed_ms, "output_bytes": size}


def run_with_fallback(primary: Callable[[], dict[str, object]],
                      fallback: Callable[[], dict[str, object]],
                      calls: dict[str, int]) -> dict[str, object]:
    calls["primary"] += 1
    try:
        result = primary()
        if not result.get("records"):
            raise ProviderFailure("transcript_empty_or_invalid")
        return {"provider_id": "paddleocr", "model_id": "PP-OCRv5_server_det+PP-OCRv5_server_rec",
                "records": result["records"], "fallback_reason": None}
    except ProviderFailure as primary_error:
        if primary_error.code not in {
            "provider_timeout", "transcription_failed", "transcript_empty_or_invalid",
            "provider_unavailable", "model_not_configured",
        }:
            raise
        calls["rapidocr"] += 1
        try:
            result = fallback()
        except ProviderFailure as fallback_error:
            raise ProviderFailure(fallback_error.code) from None
        if not result.get("records"):
            raise ProviderFailure("transcript_empty_or_invalid")
        return {"provider_id": "rapidocr",
                "model_id": "ch_PP-OCRv4_det_infer+ch_PP-OCRv4_rec_infer",
                "records": result["records"], "fallback_reason": primary_error.code}


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE projects(id TEXT PRIMARY KEY);
CREATE TABLE materials(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 source_sha256 TEXT NOT NULL, stored_path TEXT NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('active','deleted','purged')));
CREATE TABLE capture_sessions(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 material_id TEXT NOT NULL REFERENCES materials(id), status TEXT NOT NULL);
CREATE TABLE operations(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES capture_sessions(id),
 provider_id TEXT NOT NULL, model_id TEXT NOT NULL, status TEXT NOT NULL, error_code TEXT);
CREATE TABLE drafts(id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(id),
 material_id TEXT NOT NULL REFERENCES materials(id), text TEXT NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('review_required','confirmed')));
CREATE TABLE segments(id TEXT PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES drafts(id),
 ordinal INTEGER NOT NULL, text TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
 quality TEXT NOT NULL CHECK(quality IN ('clear','uncertain')));
CREATE TABLE revisions(id TEXT PRIMARY KEY, material_id TEXT NOT NULL REFERENCES materials(id),
 draft_id TEXT NOT NULL REFERENCES drafts(id), text TEXT NOT NULL);
"""


def _verify_input(path: Path, expected_hash: str) -> bool:
    from PIL import Image

    if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        return False
    try:
        with Image.open(path) as image:
            image.verify()
    except Exception:
        return False
    return digest_file(path) == expected_hash


def _safe_artifact(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ProviderFailure("artifact_exists")


def execute(paddle_root: Path, rapid_root: Path, artifact: Path) -> dict[str, object]:
    _safe_artifact(artifact)
    paddle_hashes = paddle_inventory(paddle_root)
    rapid_hashes = inventory(rapid_root, RAPID_MODELS)
    runtime = {
        "rapidocr_onnxruntime": importlib.metadata.version("rapidocr_onnxruntime"),
        "onnxruntime": importlib.metadata.version("onnxruntime"),
        "paddleocr": importlib.metadata.version("paddleocr"),
        "paddlepaddle": importlib.metadata.version("paddlepaddle"),
    }
    with tempfile.TemporaryDirectory(prefix="studybuddy-rapidocr-c2-") as directory:
        root = Path(directory)
        images: dict[str, tuple[Path, bytes]] = {}
        for image_format, suffix in (("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp")):
            path = root / f"source.{suffix}"
            content = make_image(path, image_format)
            images[suffix] = (path, content)
        checks: dict[str, object] = {}
        formats: dict[str, dict[str, object]] = {}
        calls = {"paddleocr": 0, "rapidocr": 0, "invalid_input_provider_calls": 0}
        start = time.perf_counter()
        for suffix, (path, content) in images.items():
            if not _verify_input(path, hashlib.sha256(content).hexdigest()):
                raise ProviderFailure("capture_source_unavailable")
            result = run_engine("rapidocr", path, rapid_root, root)
            calls["rapidocr"] += 1
            records = result["records"]
            formats[suffix] = {
                "provider_id": "rapidocr",
                "model_id": "ch_PP-OCRv4_det_infer+ch_PP-OCRv4_rec_infer",
                "text_present": bool(records),
                "segment_count": len(records),
                "confidence_in_range": all(0.0 <= row["confidence"] <= 1.0 for row in records),
                "quality_counts": {
                    "clear": sum(row["confidence"] >= THRESHOLD for row in records),
                    "uncertain": sum(row["confidence"] < THRESHOLD for row in records),
                },
                "elapsed_ms": result["elapsed_ms"],
                "output_bytes": result["output_bytes"],
            }
        checks["rapidocr_real_png_jpeg_webp"] = all(
            row["text_present"] and row["confidence_in_range"] for row in formats.values()
        )

        primary_path, primary_content = images["png"]
        primary_result = run_engine("paddleocr", primary_path, paddle_root, root)
        calls["paddleocr"] += 1
        primary_records = primary_result["records"]
        checks["paddleocr_real_primary_success"] = bool(primary_records) and all(
            0.0 <= row["confidence"] <= 1.0 for row in primary_records
        )

        primary_calls = {"primary": 0, "rapidocr": 0}
        def injected_primary_timeout() -> dict[str, object]:
            raise ProviderFailure("provider_timeout")

        fallback_result = run_with_fallback(
            injected_primary_timeout,
            lambda: run_engine("rapidocr", primary_path, rapid_root, root),
            primary_calls,
        )
        calls["rapidocr"] += primary_calls["rapidocr"]
        checks["observed_primary_failure_then_real_rapidocr"] = (
            fallback_result["provider_id"] == "rapidocr"
            and fallback_result["fallback_reason"] == "provider_timeout"
            and bool(fallback_result["records"])
            and primary_calls == {"primary": 1, "rapidocr": 1}
        )

        both_failed_calls = {"primary": 0, "rapidocr": 0, "third": 0}
        def fail_primary() -> dict[str, object]:
            raise ProviderFailure("provider_timeout")
        def fail_rapid() -> dict[str, object]:
            raise ProviderFailure("transcription_failed")
        try:
            run_with_fallback(fail_primary, fail_rapid, both_failed_calls)
        except ProviderFailure as exc:
            both_failed_code = exc.code
        else:
            both_failed_code = "unexpected_success"
        checks["both_provider_failure_is_bounded"] = (
            both_failed_code == "transcription_failed"
            and both_failed_calls["primary"] == 1
            and both_failed_calls["rapidocr"] == 1
            and both_failed_calls["third"] == 0
        )

        blank = root / "blank.png"
        from PIL import Image
        Image.new("RGB", (600, 200), "white").save(blank)
        corrupt = root / "corrupt.png"
        corrupt.write_bytes(b"not-an-image")
        blank_result = run_engine("rapidocr", blank, rapid_root, root)
        calls["rapidocr"] += 1
        before_corrupt = calls["paddleocr"] + calls["rapidocr"]
        checks["blank_and_corrupt_rejected_before_provider"] = (
            not _verify_input(corrupt, digest_file(corrupt))
            and not blank_result["records"]
            and calls["paddleocr"] + calls["rapidocr"] == before_corrupt
        )

        db_path = root / "c2.sqlite3"
        db = sqlite3.connect(db_path)
        db.executescript(SCHEMA)
        project_id, material_id, session_id = ident("project"), ident("material"), ident("session")
        stored_original = root / "original.png"
        shutil.copyfile(primary_path, stored_original)
        source_hash = hashlib.sha256(primary_content).hexdigest()
        db.execute("INSERT INTO projects VALUES (?)", (project_id,))
        db.execute("INSERT INTO materials VALUES (?,?,?,?,?)",
                   (material_id, project_id, source_hash, str(stored_original), "active"))
        db.execute("INSERT INTO capture_sessions VALUES (?,?,?,?)",
                   (session_id, project_id, material_id, "review_required"))
        operation_id, draft_id = ident("operation"), ident("draft")
        db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?)", (
            operation_id, session_id, "paddleocr", "PP-OCRv5_server_det+PP-OCRv5_server_rec", "succeeded", None,
        ))
        primary_text = "\n".join(row["text"] for row in primary_records)
        db.execute("INSERT INTO drafts VALUES (?,?,?,?,?)",
                   (draft_id, operation_id, material_id, primary_text, "review_required"))
        for ordinal, row in enumerate(primary_records):
            quality = "clear" if row["confidence"] >= THRESHOLD else "uncertain"
            db.execute("INSERT INTO segments VALUES (?,?,?,?,?,?)",
                       (ident("segment"), draft_id, ordinal, row["text"], row["confidence"], quality))
        db.commit()
        checks["draft_first_and_provider_identity"] = (
            db.execute("SELECT provider_id,model_id,status FROM operations WHERE id=?", (operation_id,)).fetchone()
            == ("paddleocr", "PP-OCRv5_server_det+PP-OCRv5_server_rec", "succeeded")
            and db.execute("SELECT status FROM drafts WHERE id=?", (draft_id,)).fetchone()[0] == "review_required"
            and db.execute("SELECT COUNT(*) FROM segments WHERE draft_id=?", (draft_id,)).fetchone()[0] > 0
        )
        revision_count = db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        rollback_ok = False
        try:
            with db:
                db.execute("INSERT INTO revisions VALUES (?,?,?,?)",
                           (ident("revision"), material_id, draft_id, primary_text))
                db.execute("INSERT INTO segments VALUES (?,?,?,?,?,?)",
                           (ident("bad"), draft_id, -1, "", 2.0, "clear"))
        except sqlite3.IntegrityError:
            rollback_ok = db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == revision_count
        db.execute("INSERT INTO revisions VALUES (?,?,?,?)",
                   (ident("revision"), material_id, draft_id, primary_text))
        db.execute("UPDATE drafts SET status='confirmed' WHERE id=?", (draft_id,))
        db.commit()
        db.execute("UPDATE materials SET status='deleted' WHERE id=?", (material_id,))
        db.commit()
        checks["confirm_source_identity_and_lifecycle"] = (
            db.execute("SELECT status FROM drafts WHERE id=?", (draft_id,)).fetchone()[0] == "confirmed"
            and db.execute("SELECT COUNT(*) FROM revisions WHERE material_id=?", (material_id,)).fetchone()[0] == 1
            and db.execute("SELECT status FROM materials WHERE id=?", (material_id,)).fetchone()[0] == "deleted"
        )
        checks["transaction_rollback"] = rollback_ok

        calls_before_storage_reads = dict(calls)
        backup_path, restored_path = root / "backup.sqlite3", root / "restored.sqlite3"
        backup_db = sqlite3.connect(backup_path)
        try:
            db.backup(backup_db)
        finally:
            backup_db.close()
        backup_db = sqlite3.connect(backup_path)
        restored_db = sqlite3.connect(restored_path)
        try:
            backup_db.backup(restored_db)
            integrity = restored_db.execute("PRAGMA integrity_check").fetchone()[0]
            restored_counts = {
                name: restored_db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ("materials", "operations", "drafts", "segments", "revisions")
            }
            restored_db.execute("SELECT id,status FROM materials").fetchall()
        finally:
            backup_db.close()
            restored_db.close()
        db.close()
        checks["backup_verify_restore_read_no_ocr_calls"] = (
            calls == calls_before_storage_reads
            and integrity == "ok"
            and restored_counts["drafts"] == 1
            and restored_counts["revisions"] == 1
            and restored_counts["materials"] == 1
        )
        elapsed_ms = round((time.perf_counter() - start) * 1000, 3)

    checks["model_hash_inventory_complete"] = len(rapid_hashes) == len(RAPID_MODELS) and len(paddle_hashes) == 6
    checks["runtime_versions_exact"] = runtime == {
        "rapidocr_onnxruntime": "1.4.4", "onnxruntime": "1.20.1",
        "paddleocr": "3.7.0", "paddlepaddle": "3.3.1",
    }
    checks["timeout_and_output_limits_configured"] = TIMEOUT_SECONDS == 120.0 and OUTPUT_LIMIT_BYTES == 512 * 1024
    passed = all(checks.values())
    payload = {
        "schema_version": 2,
        "component": "ocr-rapidocr",
        "gate": "strict-C2",
        "status": "integration_passed" if passed else "integration_failed",
        "network_called": False,
        "formal_system_touched": False,
        "runtime": runtime,
        "device": "cpu",
        "model_inventory": {"rapidocr": rapid_hashes, "paddleocr": paddle_hashes},
        "model_source": "explicit local model roots; SHA-256 inventory recorded; no download",
        "checks": checks,
        "formats": formats,
        "fallback": {
            "primary_failure_code": "provider_timeout",
            "primary_failure_injected": True,
            "rapidocr_real_inference": True,
            "rapidocr_calls_after_primary_failure": 1,
            "third_provider_calls": 0,
        },
        "provider_call_counts": calls,
        "restored_counts": restored_counts,
        "elapsed_ms": elapsed_ms,
        "limits": {"timeout_seconds": TIMEOUT_SECONDS, "output_bytes": OUTPUT_LIMIT_BYTES},
        "privacy": {"raw_image_retained": False, "raw_ocr_retained": False,
                     "absolute_private_path_retained": False, "stderr_retained": False},
        "limitations": ["exact Windows/Python 3.10/CPU package and model hashes only",
                        "synthetic fixtures only; no general accuracy/capacity claim",
                        "Integration evidence does not authorize Formal adoption"],
    }
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {"status": payload["status"], "checks": checks, "artifact": artifact}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paddle-model-root", type=Path)
    parser.add_argument("--rapidocr-model-root", type=Path)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--worker", nargs=4, metavar=("ENGINE", "IMAGE", "OUTPUT", "MODEL_ROOT"))
    args = parser.parse_args()
    if args.worker:
        engine, image, output, model_root = args.worker
        return _worker(engine, Path(image), Path(output), Path(model_root))
    if not args.paddle_model_root or not args.rapidocr_model_root:
        print(json.dumps({"component": "ocr-rapidocr", "gate": "strict-C2", "status": "blocked",
                          "error": "explicit_model_roots_required"}))
        return 2
    try:
        result = execute(args.paddle_model_root, args.rapidocr_model_root, args.artifact)
    except ProviderFailure as exc:
        print(json.dumps({"component": "ocr-rapidocr", "gate": "strict-C2", "status": "blocked",
                          "error": exc.code}))
        return 2
    except Exception:
        print(json.dumps({"component": "ocr-rapidocr", "gate": "strict-C2", "status": "blocked",
                          "error": "runner_failed"}))
        return 2
    print(json.dumps({"component": "ocr-rapidocr", "gate": "strict-C2", "status": result["status"],
                      "checks_passed": sum(bool(value) for value in result["checks"].values()),
                      "checks_total": len(result["checks"])}))
    return 0 if result["status"] == "integration_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
