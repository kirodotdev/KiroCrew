"""Ship a repeatable real BYO ACP process diagnostic, without touching Crew."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from kiro_byo.acp_selftest import _metadata_config, check_acp


def _config(path: Path) -> Path:
    raw = {
        "credential_schema_version": 1,
        "mode": {"id": "tiga", "type": "multi"},
        "providers": [{
            "id": "deepseek", "name": "DeepSeek", "preset_id": "deepseek",
            "type": "deepseek-responses", "base_url": "https://api.deepseek.com",
            "credential_ref": "secret-key-ref-do-not-use", "api_key": "do-not-send-secret",
            "headers": {"Authorization": "Bearer hidden"},
            "models": [
                {"id": "deepseek-v4.1-flash", "name": "DeepSeek-V4.1-Flash"},
                {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro"},
            ],
        }],
    }
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_metadata_sanitizer_never_copies_credentials(tmp_path):
    source = _config(tmp_path / "providers.json")
    result = _metadata_config(source)
    text = json.dumps(result)
    assert "secret-key-ref-do-not-use" not in text
    assert "do-not-send-secret" not in text
    assert "Bearer hidden" not in text
    assert result["providers"][0]["models"][0]["name"] == "DeepSeek-V4.1-Flash"


def test_true_stdio_both_modes_and_deepseek_effort(tmp_path):
    source = _config(tmp_path / "providers.json")
    report = check_acp(models=("DeepSeek-V4.1-Flash", "DeepSeek-V4-Pro"), config_file=source)
    assert report["ok"], report
    assert len(report["modes"]) == 2
    for mode in report["modes"]:
        assert mode["session_load"] is True
        assert mode["prompt_denied_by_preflight"] is True
        assert len(mode["fixed_models_tested"]) == 2
        flash = next(x for x in mode["fixed_models_tested"] if x["model"] == "DeepSeek-V4.1-Flash")
        assert flash["effort_levels"] == ["high", "low", "max"]
        assert flash["effort_round_trip"] is True
    assert report["crew_host_registration"] == "not_verified"


def test_explicit_missing_model_fails_closed(tmp_path):
    report = check_acp(models=("not-installed",), config_file=_config(tmp_path / "providers.json"))
    assert not report["ok"]
    assert "requested models not advertised" in report["error"]


def test_cli_json_published_with_artifact(tmp_path):
    path = _config(tmp_path / "providers.json")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-m", "kiro_byo", "verify-acp", "--json", "--config-file", str(path),
         "--model", "DeepSeek-V4.1-Flash"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=40,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert {entry["mode"] for entry in report["modes"]} == {"tiga", "dyna"}
