"""R28: no static/source/child evidence is mislabeled as a Crew E2E result."""
from pathlib import Path
import importlib.util
import sys


def _matrix():
    script = Path(__file__).resolve().parents[1] / "scripts" / "crew_byo_acceptance.py"
    spec = importlib.util.spec_from_file_location("_r28_matrix", script)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_no_upstream_source_is_explicitly_unverified(tmp_path):
    matrix = _matrix()
    missing = matrix.source_evidence(None, "0.12.0-rc.6")
    invalid = matrix.source_evidence(tmp_path, "0.12.0-rc.6")
    assert missing["crewSourceContract"] == "not_verified"
    assert missing["crewWorkLedger"] == "not_verified"
    assert invalid["crewSourceContract"] == "refused"
    assert invalid["crewWorkLedger"] == "refused"


def test_source_preflight_never_claims_host_runtime_certification():
    matrix = _matrix()
    assert "crewGatewayE2E" in Path(matrix.__file__).read_text()
    assert '"certified": False' in Path(matrix.__file__).read_text()
