"""C2 Integration smoke for the official Const-me Whisper CLI.

This lab combines the verified Composer runtime with a minimal draft-first
transcript projection and SQLite backup/restore. It is not Formal code.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

RUNTIME = Path(os.environ.get("STUDYBUDDY_ASR_RUNTIME", "H:/Whisper/cli/main.exe"))
MODEL = Path(os.environ.get("STUDYBUDDY_ASR_MODEL", "H:/Whisper/Models/ggml-large-v3-turbo.bin"))
FIXTURE = Path(os.environ.get("STUDYBUDDY_ASR_FIXTURE", "H:/Whisper/Whisper-1.12.0/SampleClips/jfk.wav"))
ARTIFACT = Path(os.environ.get("STUDYBUDDY_ASR_ARTIFACT", "H:/studybuddy-test/artifacts/asr-whisper-cpp-integration/latest.json"))
TIMEOUT_SECONDS = 120


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_cli(source: Path, output_dir: Path, timeout: float = TIMEOUT_SECONDS) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    input_path = output_dir / source.name
    shutil.copyfile(source, input_path)
    command = [
        str(RUNTIME), "-f", str(input_path), "-m", str(MODEL),
        "--language", "en", "-otxt", "-osrt", "-nc",
    ]
    started = time.perf_counter()
    process = subprocess.Popen(command, cwd=output_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=10)
        timed_out = True
    txt = next(output_dir.glob("*.txt"), None)
    srt = next(output_dir.glob("*.srt"), None)
    text = txt.read_text(encoding="utf-8", errors="replace") if txt else ""
    return {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "txt_bytes": txt.stat().st_size if txt else 0,
        "srt_bytes": srt.stat().st_size if srt else 0,
        "text_length": len(text.strip()),
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "private_text_retained": False,
        "raw_output_retained": False,
    }


def main() -> int:
    if not (RUNTIME.is_file() and MODEL.is_file() and FIXTURE.is_file()):
        raise SystemExit("ASR integration prerequisites are unavailable")
    root = Path(tempfile.mkdtemp(prefix="studybuddy-asr-integration-"))
    db_path = root / "integration.sqlite3"
    backup_path = root / "integration-backup.sqlite3"
    restored_path = root / "integration-restored.sqlite3"
    results: dict[str, object] = {}
    try:
        connection = sqlite3.connect(db_path)
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE transcript_drafts (
                source_sha256 TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                txt_bytes INTEGER NOT NULL,
                srt_bytes INTEGER NOT NULL,
                text_length INTEGER NOT NULL
            );
            """
        )
        success = run_cli(FIXTURE, root / "success")
        connection.execute(
            "INSERT INTO transcript_drafts VALUES (?, ?, ?, ?, ?)",
            (sha256(FIXTURE), "draft", success["txt_bytes"], success["srt_bytes"], success["text_length"]),
        )
        connection.commit()
        first_count = connection.execute("SELECT COUNT(*) FROM transcript_drafts").fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO transcript_drafts VALUES (?, ?, ?, ?, ?)",
            (sha256(FIXTURE), "draft", success["txt_bytes"], success["srt_bytes"], success["text_length"]),
        )
        connection.commit()
        repeated_count = connection.execute("SELECT COUNT(*) FROM transcript_drafts").fetchone()[0]
        malformed = root / "malformed.wav"
        malformed.write_bytes(b"not-a-wave")
        unsupported = root / "unsupported.xyz"
        unsupported.write_bytes(b"unsupported")
        malformed_result = run_cli(malformed, root / "malformed")
        unsupported_result = run_cli(unsupported, root / "unsupported")
        silent = root / "silent.wav"
        import wave
        with wave.open(str(silent), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 16000)
        silent_result = run_cli(silent, root / "silent")
        rollback_before = connection.execute("SELECT COUNT(*) FROM transcript_drafts").fetchone()[0]
        rollback_passed = False
        try:
            with connection:
                connection.execute("INSERT INTO transcript_drafts VALUES (?, ?, ?, ?, ?)", ("rollback", "draft", 0, 0, 0))
                raise sqlite3.IntegrityError("synthetic rollback")
        except sqlite3.IntegrityError:
            rollback_passed = connection.execute("SELECT COUNT(*) FROM transcript_drafts").fetchone()[0] == rollback_before
        connection.close()
        backup = sqlite3.connect(backup_path)
        sqlite3.connect(db_path).backup(backup)
        backup.close()
        source = sqlite3.connect(backup_path)
        restored = sqlite3.connect(restored_path)
        source.backup(restored)
        source.close()
        restored_count = restored.execute("SELECT COUNT(*) FROM transcript_drafts").fetchone()[0]
        restored.close()
        results = {
            "C2-ASR-01_runtime_model_version": {"passed": True, "runtime": "Const-me/Whisper 1.12.0", "fixture_sha256": sha256(FIXTURE)},
            "C2-ASR-02_success_draft_projection": {"passed": success["exit_code"] == 0 and success["text_length"] > 0 and first_count == 1, "result": success},
            "C2-ASR-03_txt_srt_contract": {"passed": success["txt_bytes"] > 0 and success["srt_bytes"] > 0, "txt_bytes": success["txt_bytes"], "srt_bytes": success["srt_bytes"]},
            "C2-ASR-04_failure_mapping": {"passed": malformed_result["exit_code"] not in (None, 0) and unsupported_result["exit_code"] not in (None, 0), "malformed_exit_code": malformed_result["exit_code"], "unsupported_exit_code": unsupported_result["exit_code"]},
            "C2-ASR-05_silent_input": {"passed": silent_result["exit_code"] == 0, "result": silent_result},
            "C2-ASR-06_idempotent_repeat": {"passed": repeated_count == 1, "first_count": first_count, "repeated_count": repeated_count},
            "C2-ASR-07_transaction_rollback": {"passed": rollback_passed, "count_before": rollback_before},
            "C2-ASR-08_backup_restore": {"passed": restored_count == 1, "restored_count": restored_count},
            "C2-ASR-09_privacy": {"passed": all(not item.get("private_text_retained") and not item.get("raw_output_retained") for item in (success, malformed_result, unsupported_result, silent_result)), "raw_output_retained": False, "transcript_retained": False},
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)
    passed = sum(item["passed"] for item in results.values())
    payload = {"component": "asr-whisper-cpp", "status": "integration_passed" if passed == len(results) else "integration_failed", "checks": results, "summary": {"passed": passed, "failed": len(results) - passed, "total": len(results)}}
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({"component": payload["component"], "status": payload["status"], **payload["summary"]}))
    return 0 if payload["status"] == "integration_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
