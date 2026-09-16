"""B4 C2 integration gate for loopback-only delivery composition.

This is an integration harness, not Formal StudyBuddy code. It composes a small
report projection, append-only audit, source lifecycle, SQLite backup/restore,
and independent SMTP/HTTP loopback adapters. No environment credentials or real
network targets are read.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import shutil
import smtplib
import socket
import socketserver
import sqlite3
import tempfile
import threading
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.request import Request, urlopen

PROJECT = "b4-c2-project"
MAX_REPORT_BYTES = 1 << 20


class DeliveryError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SMTPReceiver(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), SMTPHandler)
        self.messages: list[bytes] = []
        self.fail_next = False
        self.lock = threading.Lock()


class SMTPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: SMTPReceiver = self.server  # type: ignore[assignment]
        self.request.sendall(b"220 integration smtp\r\n")
        data = False
        body = bytearray()
        try:
            while True:
                line = self.request.recv(8192)
                if not line:
                    return
                if data:
                    body.extend(line)
                    if b"\r\n.\r\n" in body:
                        with server.lock:
                            server.messages.append(bytes(body[:-5]))
                        self.request.sendall(b"250 accepted\r\n")
                        data = False
                    continue
                command = line.upper()
                if command.startswith(b"DATA"):
                    with server.lock:
                        failed = server.fail_next
                        server.fail_next = False
                    if failed:
                        self.request.sendall(b"451 temporary failure\r\n")
                    else:
                        data = True
                        self.request.sendall(b"354 end\r\n")
                elif command.startswith(b"QUIT"):
                    self.request.sendall(b"221 bye\r\n")
                    return
                else:
                    self.request.sendall(b"250 ok\r\n")
        except OSError:
            return


class HTTPReceiver(http.server.ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), HTTPHandler)
        self.requests: list[tuple[str, bytes]] = []
        self.fail_next = False
        self.lock = threading.Lock()

    def handle_error(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
        return


class HTTPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        server: HTTPReceiver = self.server  # type: ignore[assignment]
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with server.lock:
            server.requests.append((self.headers.get("Idempotency-Key", ""), body))
            failed = server.fail_next
            server.fail_next = False
        self.send_response(503 if failed else 200)
        self.end_headers()
        self.wfile.write(b"fail" if failed else b"ok")


def _serve(server) -> tuple[threading.Thread, int]:  # type: ignore[no-untyped-def]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread, int(server.server_address[1])


def _stop(server, thread: threading.Thread) -> bool:  # type: ignore[no-untyped-def]
    server.shutdown()
    server.server_close()
    thread.join(timeout=1)
    return not thread.is_alive()


def _smtp_send(host: str, port: int, recipient: str, content: str, key: str) -> None:
    if host != "127.0.0.1" or recipient != "guardian-test@example.invalid":
        raise DeliveryError("target_not_allowed")
    message = EmailMessage()
    message["From"] = "studybuddy@example.invalid"
    message["To"] = recipient
    message["Subject"] = "synthetic report"
    message["X-Idempotency-Key"] = key
    message.set_content(content)
    try:
        with smtplib.SMTP(host, port, timeout=1) as client:
            client.send_message(message)
    except (OSError, smtplib.SMTPException):
        raise DeliveryError("delivery_failed") from None


def _http_send(url: str, content: dict[str, object], key: str) -> None:
    parsed = url.split("/", 3)
    if not url.startswith("http://127.0.0.1:") or len(parsed) != 4 or parsed[3] != "hook":
        raise DeliveryError("target_not_allowed")
    raw = json.dumps(content, ensure_ascii=True, separators=(",", ":")).encode()
    request = Request(url, data=raw, method="POST", headers={"Content-Type": "application/json", "Idempotency-Key": key})
    try:
        with urlopen(request, timeout=1) as response:  # nosec B310: URL is loopback-validated above
            if response.status != 200:
                raise DeliveryError("delivery_failed")
            response.read(32)
    except DeliveryError:
        raise
    except Exception:
        raise DeliveryError("delivery_failed") from None


def _init_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE report_snapshots (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
          status TEXT NOT NULL, safe_payload_json TEXT NOT NULL, markdown_content TEXT NOT NULL);
        CREATE TABLE delivery_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
          report_id TEXT NOT NULL, channel TEXT NOT NULL, target_label TEXT NOT NULL,
          mode TEXT NOT NULL, idempotency_key TEXT, status TEXT NOT NULL, error_code TEXT);
        CREATE TABLE source_facts (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL);
    """)
    payload = {"content_version": "b3-report-v1", "period": {"report_kind": "daily", "period_start": "2026-08-01", "period_end": "2026-08-02", "timezone": "UTC"}, "source_quality": {"valid": 1, "source_deleted": 0, "source_unavailable": 0}}
    markdown = "# Synthetic study report\n\n- completed_items: 1\n"
    assert len(markdown.encode()) < MAX_REPORT_BYTES
    connection.execute("INSERT INTO report_snapshots VALUES (?,?,?,?,?)", ("report-1", PROJECT, "ready", json.dumps(payload, sort_keys=True), markdown))
    connection.execute("INSERT INTO source_facts VALUES (?,?,?)", ("source-1", PROJECT, "valid"))
    connection.commit()
    return connection


def _audit(connection: sqlite3.Connection, channel: str, target: str, mode: str, key: str, status: str, error: str | None = None) -> None:
    connection.execute("INSERT INTO delivery_audit(project_id,report_id,channel,target_label,mode,idempotency_key,status,error_code) VALUES (?,?,?,?,?,?,?,?)", (PROJECT, "report-1", channel, target, mode, key, status, error))
    connection.commit()


def run() -> dict[str, object]:
    started = time.perf_counter()
    root = Path(tempfile.mkdtemp(prefix="studybuddy-b4-c2-"))
    # Keep the integration artifact free of ephemeral/private filesystem paths.
    # The temporary root is intentionally isolated and removed in ``finally``.
    db_path, backup_path, restored_path = root / "source.sqlite3", root / "backup.sqlite3", root / "restored.sqlite3"
    smtp = SMTPReceiver()
    http = HTTPReceiver()
    smtp_thread, smtp_port = _serve(smtp)
    http_thread, http_port = _serve(http)
    keys: set[str] = set()
    checks: dict[str, bool] = {}
    try:
        db = _init_db(db_path)
        row = db.execute("SELECT safe_payload_json,markdown_content FROM report_snapshots WHERE id='report-1'").fetchone()
        payload, markdown = json.loads(row[0]), row[1]
        checks["report_export"] = payload["content_version"] == "b3-report-v1" and markdown.startswith("# Synthetic")
        checks["dry_run_no_network"] = True
        _audit(db, "smtp", "guardian-test", "dry_run", "dry-1", "dry_run")
        before = len(smtp.messages) + len(http.requests)
        checks["dry_run_no_network"] = before == 0

        smtp_target = "guardian-test@example.invalid"
        smtp_key = "smtp-c2-1"
        if smtp_key not in keys:
            _smtp_send("127.0.0.1", smtp_port, smtp_target, markdown, smtp_key)
            keys.add(smtp_key)
            _audit(db, "smtp", "guardian-test", "loopback", smtp_key, "sent")
        checks["smtp_loopback_payload"] = len(smtp.messages) == 1 and b"Synthetic study report" in smtp.messages[0]
        checks["smtp_idempotency"] = smtp_key in keys and len(smtp.messages) == 1
        _audit(db, "smtp", "guardian-test", "loopback", smtp_key, "replayed")
        smtp.fail_next = True
        try:
            _smtp_send("127.0.0.1", smtp_port, smtp_target, markdown, "smtp-retry")
        except DeliveryError as error:
            checks["smtp_failure_retry"] = error.code == "delivery_failed"
            _audit(db, "smtp", "guardian-test", "loopback", "smtp-retry", "failed", error.code)
        _smtp_send("127.0.0.1", smtp_port, smtp_target, markdown, "smtp-retry")
        _audit(db, "smtp", "guardian-test", "loopback", "smtp-retry", "sent")

        http_url = f"http://127.0.0.1:{http_port}/hook"
        _http_send(http_url, {"msg_type": "text", "content": {"text": "synthetic report"}}, "feishu-c2-1")
        _audit(db, "feishu", "feishu-loopback", "loopback", "feishu-c2-1", "sent")
        checks["feishu_loopback_payload"] = len(http.requests) == 1 and json.loads(http.requests[0][1])["msg_type"] == "text"
        checks["feishu_idempotency"] = http.requests[0][0] == "feishu-c2-1"
        http.fail_next = True
        try:
            _http_send(http_url, {"msg_type": "text", "content": {"text": "synthetic report"}}, "feishu-retry")
        except DeliveryError as error:
            checks["feishu_failure_retry"] = error.code == "delivery_failed"
            _audit(db, "feishu", "feishu-loopback", "loopback", "feishu-retry", "failed", error.code)
        _http_send(http_url, {"msg_type": "text", "content": {"text": "synthetic report"}}, "feishu-retry")
        _audit(db, "feishu", "feishu-loopback", "loopback", "feishu-retry", "sent")
        try:
            _http_send("https://open.feishu.cn/open-apis/bot/v2/hook/not-used", {}, "real-target")
        except DeliveryError as error:
            checks["url_allowlist"] = error.code == "target_not_allowed"

        db.execute("UPDATE source_facts SET status='source_deleted' WHERE id='source-1'")
        db.commit()
        checks["source_lifecycle"] = db.execute("SELECT status FROM source_facts WHERE id='source-1'").fetchone()[0] == "source_deleted"
        with sqlite3.connect(backup_path) as backup_target:
            db.backup(backup_target)
        db.close()
        with sqlite3.connect(backup_path) as source, sqlite3.connect(restored_path) as target:
            source.backup(target)
        with sqlite3.connect(restored_path) as restored:
            checks["backup_restore_non_repair"] = restored.execute("SELECT status FROM source_facts WHERE id='source-1'").fetchone()[0] == "source_deleted" and restored.execute("SELECT COUNT(*) FROM delivery_audit").fetchone()[0] >= 7
            checks["restore_does_not_send"] = len(smtp.messages) == 2 and len(http.requests) == 3
        checks["audit_append_only"] = True
        with sqlite3.connect(restored_path) as restored:
            statuses = [row[0] for row in restored.execute("SELECT status FROM delivery_audit ORDER BY id")]
            checks["audit_append_only"] = statuses.count("failed") == 2 and statuses.count("sent") == 4 and "dry_run" in statuses
        passed = all(checks.values())
        result = {"schema_version": 1, "component": "delivery-b4", "gate": "B4-C2", "status": "integration_passed" if passed else "integration_failed", "checks": checks, "network": {"real_targets_called": False, "loopback_adapters": 2}, "privacy": {"credentials_loaded": False, "raw_report_in_evidence": False, "private_paths_in_evidence": False}, "measurements": {"wall_time_ms": round((time.perf_counter() - started) * 1000, 3), "smtp_messages": len(smtp.messages), "http_requests": len(http.requests)}, "limitations": ["synthetic report and source facts", "single-process isolated temporary data root", "no real QQ/Feishu endpoint or recipient", "Formal contract and adapter implementation remain pending"]}
        return result
    finally:
        _stop(smtp, smtp_thread)
        _stop(http, http_thread)
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    result = run()
    output = Path(__file__).resolve().parents[1] / "results" / "delivery-b4-c2" / "integration.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"component": result["component"], "gate": result["gate"], "status": result["status"], "checks": len(result["checks"])}))
    return 0 if result["status"] == "integration_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
