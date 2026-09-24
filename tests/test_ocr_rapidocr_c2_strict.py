from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

RUNNER_PATH = Path(__file__).resolve().parents[1] / "ocr-rapidocr" / "run_integration.py"
SPEC = importlib.util.spec_from_file_location("rapidocr_c2_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _success(text: str = "synthetic result") -> dict[str, object]:
    return {"records": [{"text": text, "confidence": 0.9}]}


def test_primary_success_does_not_call_fallback():
    calls = {"primary": 0, "rapidocr": 0}

    def primary():
        return _success()

    def fallback():
        raise AssertionError("fallback must not run")

    result = runner.run_with_fallback(primary, fallback, calls)

    assert result["provider_id"] == "paddleocr"
    assert result["fallback_reason"] is None
    assert calls == {"primary": 1, "rapidocr": 0}


def test_primary_timeout_calls_rapidocr_exactly_once():
    calls = {"primary": 0, "rapidocr": 0}

    def primary():
        raise runner.ProviderFailure("provider_timeout")

    result = runner.run_with_fallback(primary, _success, calls)

    assert result["provider_id"] == "rapidocr"
    assert result["fallback_reason"] == "provider_timeout"
    assert calls == {"primary": 1, "rapidocr": 1}


@pytest.mark.parametrize("code", ["payload_too_large", "capture_asset_too_large", "capture_source_unavailable"])
def test_non_provider_boundary_failure_does_not_fallback(code: str):
    calls = {"primary": 0, "rapidocr": 0}

    def primary():
        raise runner.ProviderFailure(code)

    with pytest.raises(runner.ProviderFailure, match=code):
        runner.run_with_fallback(primary, _success, calls)

    assert calls == {"primary": 1, "rapidocr": 0}


def test_both_provider_failures_do_not_attempt_third_provider():
    calls = {"primary": 0, "rapidocr": 0, "third": 0}

    def primary():
        raise runner.ProviderFailure("provider_timeout")

    def fallback():
        raise runner.ProviderFailure("transcription_failed")

    with pytest.raises(runner.ProviderFailure, match="transcription_failed"):
        runner.run_with_fallback(primary, fallback, calls)

    assert calls == {"primary": 1, "rapidocr": 1, "third": 0}
