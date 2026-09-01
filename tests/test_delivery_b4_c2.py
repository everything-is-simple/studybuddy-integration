"""Regression tests for the isolated B4-C2 delivery integration harness."""
from __future__ import annotations

import json
import sys
from pathlib import Path


INTEGRATION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INTEGRATION_ROOT / "delivery"))

from run_integration import run  # noqa: E402


EXPECTED_CHECKS = {
    "report_export",
    "dry_run_no_network",
    "smtp_loopback_payload",
    "smtp_idempotency",
    "smtp_failure_retry",
    "feishu_loopback_payload",
    "feishu_idempotency",
    "feishu_failure_retry",
    "url_allowlist",
    "source_lifecycle",
    "backup_restore_non_repair",
    "restore_does_not_send",
    "audit_append_only",
}


def test_b4_c2_isolated_integration_passes() -> None:
    result = run()

    assert result["schema_version"] == 1
    assert result["component"] == "delivery-b4"
    assert result["gate"] == "B4-C2"
    assert result["status"] == "integration_passed"
    assert set(result["checks"]) == EXPECTED_CHECKS
    assert all(result["checks"].values())


def test_b4_c2_never_uses_real_targets_or_credentials() -> None:
    result = run()

    assert result["network"] == {"real_targets_called": False, "loopback_adapters": 2}
    assert result["privacy"] == {
        "credentials_loaded": False,
        "raw_report_in_evidence": False,
        "private_paths_in_evidence": False,
    }
    assert "no real QQ/Feishu endpoint or recipient" in result["limitations"]


def test_b4_c2_result_artifact_is_sanitized_and_reproducible() -> None:
    result = run()
    serialized = json.dumps(result, ensure_ascii=True)

    assert "127.0.0.1" not in serialized
    assert "studybuddy-b4-c2-" not in serialized
    assert result["measurements"]["smtp_messages"] == 2
    assert result["measurements"]["http_requests"] == 3
