"""Regression tests for Round-26 findings (KiroCrew PR #6).

F1: recall gets the same preview-binding as destroy (R25) — BE verifies
    expected ids at confirm; FE runs the two-call flow.
F2: config/verify responses route through the _sanitize_response chokepoint.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_f1_backend_recall_verifies_expected_resource_ids():
    src = (_ROOT / "src/kiro_crew/deploy/handlers.py").read_text()
    seg = src.split("async def _do_recall", 1)[1].split("\nasync def ", 1)[0]
    assert "expected_distribution_id" in seg
    assert "resource ids changed since preview" in seg
    # Verification precedes engine.recall.
    assert seg.index("engine.find_site_by_tag") < seg.index("engine.recall")


def test_f1_frontend_two_call_recall():
    src = (_ROOT / "website/src/pages/ArtifactDeployPage.tsx").read_text()
    # The single-shot recall (confirm: true as the only call) is gone.
    m = re.search(
        r"jsend<any>\('/recall', \{ site_id: s\.site_id, confirm: true, "
        r"profile: s\.profile \|\| '' \}\)", src)
    assert m is None
    # The confirmed call carries the previewed ids.
    seg = src.split("const recallMut", 1)[1].split("const destroyMut", 1)[0]
    assert "expected_bucket" in seg
    assert "expected_distribution_id" in seg


def test_f2_no_raw_json_response_result_in_deploy_handlers():
    src = (_ROOT / "src/kiro_crew/deploy/handlers.py").read_text()
    assert "web.json_response(result)" not in src
    assert 'web.json_response({**result, "profile": profile})' not in src
