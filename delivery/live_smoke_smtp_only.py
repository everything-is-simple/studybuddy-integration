"""SMTP-only smoke test with enhanced diagnostics (passwords never logged).

This script attempts QQ SMTP connection with detailed error classification to help
diagnose authentication, network, or configuration issues without exposing secrets.
"""
from __future__ import annotations

import json
import os
import smtplib
import socket
import time
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "results" / "delivery-b4-live-smoke" / "smtp-only.json"
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


def _send_email(host: str, user: str, auth_code: str, recipient: str, key: str) -> tuple[bool, str]:
    """Returns (success, diagnostic_code). Diagnostic never contains passwords."""
    allowed_hosts = {"smtp.qq.com", "smtp.163.com"}
    if host not in allowed_hosts:
        return False, "smtp_host_not_allowed"
    if "@" not in user or "@" not in recipient:
        return False, "email_address_invalid"
    if not auth_code or len(auth_code) < 10:
        return False, "auth_code_looks_invalid"
    
    message = EmailMessage()
    message["From"] = user
    message["To"] = recipient
    message["Subject"] = "StudyBuddy live smoke test"
    message["X-Idempotency-Key"] = key
    message.set_content(SYNTHETIC_REPORT)
    
    try:
        with smtplib.SMTP_SSL(host, 465, timeout=TIMEOUT_SECONDS) as client:
            client.login(user, auth_code)
            client.send_message(message)
        return True, "smtp_sent"
    except smtplib.SMTPAuthenticationError as e:
        # Authentication failed - wrong user/password combination
        return False, f"smtp_auth_failed_code_{e.smtp_code}"
    except smtplib.SMTPSenderRefused as e:
        # Sender address rejected
        return False, f"smtp_sender_refused_code_{e.smtp_code}"
    except smtplib.SMTPRecipientsRefused:
        # Recipient address rejected
        return False, "smtp_recipient_refused"
    except smtplib.SMTPException as e:
        # Other SMTP protocol errors
        return False, f"smtp_protocol_error_{type(e).__name__}"
    except socket.timeout:
        # Connection timeout
        return False, "smtp_connection_timeout"
    except socket.gaierror:
        # DNS resolution failed
        return False, "smtp_dns_resolution_failed"
    except ConnectionRefusedError:
        # Port 465 refused
        return False, "smtp_connection_refused_port_465"
    except OSError as e:
        # Network or SSL errors
        return False, f"smtp_network_error_{type(e).__name__}"


def main() -> int:
    started = time.perf_counter()
    env = _load_env(ROOT / ".env.local")
    config = {k: _value(env, k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_AUTH_CODE", "SMTP_TO")}
    
    result: dict[str, object] = {
        "schema_version": 1,
        "component": "delivery-b4-smtp-only",
        "gate": "B4-integration-smtp-smoke",
        "status": "blocked",
        "sent": False,
        "network": {"real_targets_called": False},
        "privacy": {
            "credentials_loaded": bool(config["SMTP_AUTH_CODE"]),
            "auth_code_length": len(config["SMTP_AUTH_CODE"]),
            "secrets_written": False,
        },
        "diagnostics": {
            "smtp_host": config["SMTP_HOST"],
            "smtp_user_domain": config["SMTP_USER"].split("@")[-1] if "@" in config["SMTP_USER"] else "invalid",
            "smtp_to_domain": config["SMTP_TO"].split("@")[-1] if "@" in config["SMTP_TO"] else "invalid",
        },
        "limitations": ["synthetic content only", "SMTP only; Feishu not tested", "not Formal acceptance"],
    }
    
    try:
        if _value(env, "LIVE_SMOKE") != "1":
            raise SmokeError("live_smoke_disabled_set_LIVE_SMOKE_1")
        if _value(env, "LIVE_SMOKE_CONFIRM") != CONFIRMATION:
            raise SmokeError("explicit_confirmation_required")
        
        for key in ("SMTP_HOST", "SMTP_USER", "SMTP_AUTH_CODE", "SMTP_TO"):
            if not config[key]:
                raise SmokeError(f"missing_{key.lower()}")
        
        email_key = "studybuddy-live-smoke-smtp-only-1"
        success, diagnostic = _send_email(
            config["SMTP_HOST"],
            config["SMTP_USER"],
            config["SMTP_AUTH_CODE"],
            config["SMTP_TO"],
            email_key
        )
        
        result["network"] = {"real_targets_called": True}
        result["diagnostics"]["smtp_diagnostic"] = diagnostic  # type: ignore[index]
        
        if success:
            result["sent"] = True
            result["status"] = "smtp_smoke_passed"
        else:
            result["status"] = "blocked"
            result["error_code"] = diagnostic
            
    except SmokeError as error:
        result["error_code"] = str(error)
    finally:
        result["measurements"] = {"wall_time_ms": round((time.perf_counter() - started) * 1000, 3)}
        RESULT.parent.mkdir(parents=True, exist_ok=True)
        RESULT.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    
    print(json.dumps({
        "component": result["component"],
        "status": result["status"],
        "sent": result["sent"],
        "error_code": result.get("error_code"),
        "diagnostic": result.get("diagnostics", {}).get("smtp_diagnostic")
    }, ensure_ascii=True))
    return 0 if result["status"] == "smtp_smoke_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
