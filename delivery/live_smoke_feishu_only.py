"""Separate Feishu-only and SMTP-only smoke tests with enhanced diagnostics.

Usage:
  python live_smoke_feishu_only.py  # Test Feishu only
  python live_smoke_smtp_only.py    # Test SMTP only with detailed error
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "results" / "delivery-b4-live-smoke" / "feishu-only.json"
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


def _send_feishu(url: str, key: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "open.feishu.cn" or not parsed.path.startswith("/open-apis/bot/v2/hook/"):
        raise SmokeError("feishu_webhook_not_allowed")
    if len(parsed.path.rsplit("/", 1)[-1]) < 20:
        raise SmokeError("feishu_webhook_invalid")
    body = json.dumps({"msg_type": "text", "content": {"text": SYNTHETIC_REPORT}}, separators=(",", ":")).encode()
    request = Request(url, data=body, method="POST", headers={"Content-Type": "application/json", "Idempotency-Key": key})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310: allowlist validated above
            if response.status < 200 or response.status >= 300:
                raise SmokeError(f"feishu_http_{response.status}")
            response.read(128)
    except SmokeError:
        raise
    except (OSError, HTTPError, URLError) as e:
        raise SmokeError(f"feishu_network_error_{type(e).__name__}") from None


def main() -> int:
    started = time.perf_counter()
    env = _load_env(ROOT / "results" / ".env.local")
    webhook = _value(env, "FEISHU_WEBHOOK_URL")
    result: dict[str, object] = {
        "schema_version": 1,
        "component": "delivery-b4-feishu-only",
        "gate": "B4-integration-feishu-smoke",
        "status": "blocked",
        "sent": False,
        "network": {"real_targets_called": False},
        "privacy": {"webhook_written": False, "raw_responses_written": False},
        "limitations": ["synthetic content only", "Feishu only; SMTP not tested", "not Formal acceptance"],
    }
    try:
        if _value(env, "LIVE_SMOKE") != "1":
            raise SmokeError("live_smoke_disabled_set_LIVE_SMOKE_1")
        if _value(env, "LIVE_SMOKE_CONFIRM") != CONFIRMATION:
            raise SmokeError("explicit_confirmation_required")
        if not webhook:
            raise SmokeError("feishu_webhook_url_missing")
        feishu_key = "studybuddy-live-smoke-feishu-only-1"
        _send_feishu(webhook, feishu_key)
        result["sent"] = True
        result["network"] = {"real_targets_called": True}
        result["status"] = "feishu_smoke_passed"
    except SmokeError as error:
        result["error_code"] = str(error)
    finally:
        result["measurements"] = {"wall_time_ms": round((time.perf_counter() - started) * 1000, 3)}
        RESULT.parent.mkdir(parents=True, exist_ok=True)
        RESULT.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"component": result["component"], "status": result["status"], "sent": result["sent"], "error_code": result.get("error_code")}, ensure_ascii=True))
    return 0 if result["status"] == "feishu_smoke_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
