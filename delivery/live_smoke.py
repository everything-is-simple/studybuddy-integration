"""Opt-in, one-shot live smoke for QQ SMTP and Feishu webhook.

This remains Integration evidence only. It is intentionally disabled unless the
operator sets LIVE_SMOKE=1 and confirms the exact configured targets with
LIVE_SMOKE_CONFIRM=I_UNDERSTAND_REAL_SEND. It sends only a synthetic report and
never writes secrets, URLs, recipients, or provider response bodies to output.
"""
from __future__ import annotations

import hashlib
import json
import os
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "results" / "delivery-b4-live-smoke" / "live-smoke.json"
CONFIRMATION = "I_UNDERSTAND_REAL_SEND"
SYNTHETIC_REPORT = "StudyBuddy live smoke test. Synthetic content only. No study data."
TIMEOUT_SECONDS = 10


class SmokeError(Exception):
    """Expected, safe-to-report smoke failure."""


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _value(env: dict[str, str], key: str) -> str:
    return os.environ.get(key, env.get(key, "")).strip()


def _safe_target_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _validate(config: dict[str, str]) -> None:
    required = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_AUTH_CODE", "SMTP_TO", "FEISHU_WEBHOOK_URL")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise SmokeError("missing_required_configuration")
    if config["SMTP_HOST"] not in {"smtp.qq.com", "smtp.163.com"}:
        raise SmokeError("smtp_host_not_allowed")
    if config["SMTP_PORT"] != "465":
        raise SmokeError("smtp_port_not_allowed")
    if "@" not in config["SMTP_USER"] or "@" not in config["SMTP_TO"]:
        raise SmokeError("email_address_invalid")
    parsed = urlparse(config["FEISHU_WEBHOOK_URL"])
    if parsed.scheme != "https" or parsed.netloc != "open.feishu.cn" or not parsed.path.startswith("/open-apis/bot/v2/hook/"):
        raise SmokeError("feishu_webhook_not_allowed")
    if len(parsed.path.rsplit("/", 1)[-1]) < 20:
        raise SmokeError("feishu_webhook_invalid")


def _send_email(config: dict[str, str], key: str) -> None:
    message = EmailMessage()
    message["From"] = config["SMTP_USER"]
    message["To"] = config["SMTP_TO"]
    message["Subject"] = "StudyBuddy live smoke test"
    message["X-Idempotency-Key"] = key
    message.set_content(SYNTHETIC_REPORT)
    try:
        with smtplib.SMTP_SSL(config["SMTP_HOST"], 465, timeout=TIMEOUT_SECONDS) as client:
            client.login(config["SMTP_USER"], config["SMTP_AUTH_CODE"])
            client.send_message(message)
    except (OSError, smtplib.SMTPException):
        raise SmokeError("smtp_delivery_failed") from None


def _send_feishu(config: dict[str, str], key: str) -> None:
    body = json.dumps({"msg_type": "text", "content": {"text": SYNTHETIC_REPORT}}, separators=(",", ":")).encode()
    request = Request(config["FEISHU_WEBHOOK_URL"], data=body, method="POST", headers={"Content-Type": "application/json", "Idempotency-Key": key})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310: allowlist validated above
            if response.status < 200 or response.status >= 300:
                raise SmokeError("feishu_delivery_failed")
            response.read(128)
    except SmokeError:
        raise
    except (OSError, HTTPError, URLError):
        raise SmokeError("feishu_delivery_failed") from None


def _write_result(result: dict[str, object]) -> None:
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    started = time.perf_counter()
    env = _load_env(ROOT / "results" / ".env.local")
    config = {key: _value(env, key) for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_AUTH_CODE", "SMTP_TO", "FEISHU_WEBHOOK_URL")}
    attempted = {"smtp": False, "feishu": False}
    result: dict[str, object] = {
        "schema_version": 1,
        "component": "delivery-b4-live-smoke",
        "gate": "B4-integration-live-smoke",
        "status": "blocked",
        "sent": {"smtp": False, "feishu": False},
        "network": {"real_targets_called": False},
        "privacy": {"credentials_loaded": bool(config["SMTP_AUTH_CODE"]), "secrets_written": False, "raw_responses_written": False},
        "limitations": ["synthetic content only", "one-shot smoke; no automatic retry", "not Formal acceptance"],
    }
    try:
        if _value(env, "LIVE_SMOKE") != "1":
            raise SmokeError("live_smoke_disabled_set_LIVE_SMOKE_1")
        if _value(env, "LIVE_SMOKE_CONFIRM") != CONFIRMATION:
            raise SmokeError("explicit_confirmation_required")
        _validate(config)
        email_key = "studybuddy-live-smoke-email-1"
        feishu_key = "studybuddy-live-smoke-feishu-1"
        attempted["smtp"] = True
        _send_email(config, email_key)
        result["sent"]["smtp"] = True  # type: ignore[index]
        attempted["feishu"] = True
        _send_feishu(config, feishu_key)
        result["sent"]["feishu"] = True  # type: ignore[index]
        result["network"] = {"real_targets_called": True, "targets": {"smtp_to": _safe_target_fingerprint(config["SMTP_TO"]), "feishu_webhook": _safe_target_fingerprint(config["FEISHU_WEBHOOK_URL"])}}
        result["status"] = "live_smoke_passed"
    except SmokeError as error:
        result["error_code"] = str(error)
    finally:
        result["network"] = {"real_targets_called": any(attempted), "attempted": attempted}
        result["measurements"] = {"wall_time_ms": round((time.perf_counter() - started) * 1000, 3)}
        _write_result(result)
    print(json.dumps({"component": result["component"], "status": result["status"], "error_code": result.get("error_code")}, ensure_ascii=True))
    return 0 if result["status"] == "live_smoke_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
