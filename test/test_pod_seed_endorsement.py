"""A pod's seed and restart rewrite of its own ``config.json`` is endorsed in the pod home.

The restart step runs in the operator's process, so without an endorsement in the POD's
data home the pod's gateway would read its own restart as an edit made outside Kiro
Crew. A raw edit made in the pod home before a restart must stay on disk and pending.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.config import loosening_provenance as lp
from kiro_crew.platform_compat import IS_POSIX
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig

pytestmark = pytest.mark.skipif(not IS_POSIX, reason="pods require POSIX descriptor traversal")


def _seeded_pod_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PodConfig, Path]:
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    cfg = PodConfig.load()
    assert rt.seed_home_from_scenario(cfg, "wt", "minimal") is True
    return cfg, cfg.home_dir("wt")


def _gateway_save_with_tunnel_on(home: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Save ``config.json`` the way the pod's own gateway does: endorsed, trailing newline."""
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    doc = json.loads((home / "config.json").read_text(encoding="utf-8"))
    doc["tunnel"]["enabled"] = True
    saved = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
    (home / "config.json").write_bytes(saved)
    lp.record(lp.LEAF_CONFIG, saved, strict=True)
    return saved


def _restart_from_the_operator_home(
    cfg: PodConfig, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restart the pod the way its service boot does: from the operator's own data home."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "operator"))
    assert rt.seed_home_from_scenario(cfg, "wt", "minimal") is False
    monkeypatch.setenv("KIROCREW_HOME", str(home))


def _copy(home: Path) -> bytes:
    return (home / lp.RECORD_DIR_LEAF / lp.ENDORSED_SUBDIR / lp.LEAF_CONFIG).read_bytes()


@pytest.fixture
def enforcing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)


@pytest.mark.usefixtures("enforcing")
def test_a_plain_restart_is_not_pending_and_the_copy_is_floored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, home = _seeded_pod_home(tmp_path, monkeypatch)
    _gateway_save_with_tunnel_on(home, monkeypatch)
    assert lp.pending_changes() == []

    _restart_from_the_operator_home(cfg, home, tmp_path, monkeypatch)

    assert lp.pending_changes() == []
    assert (home / "config.json").read_bytes() == _copy(home)
    assert json.loads(_copy(home))["tunnel"]["enabled"] is False


@pytest.mark.usefixtures("enforcing")
def test_a_raw_edit_survives_a_restart_and_stays_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, home = _seeded_pod_home(tmp_path, monkeypatch)
    saved = _gateway_save_with_tunnel_on(home, monkeypatch)
    raw = json.loads(saved)
    raw["dashboard"]["port"] = 4242
    raw_bytes = json.dumps(raw).encode("utf-8")
    (home / "config.json").write_bytes(raw_bytes)

    _restart_from_the_operator_home(cfg, home, tmp_path, monkeypatch)

    assert (home / "config.json").read_bytes() == raw_bytes
    assert [p["leaf"] for p in lp.pending_changes()] == [lp.LEAF_CONFIG]
    copy = json.loads(_copy(home))
    assert copy["tunnel"]["enabled"] is False
    assert copy["dashboard"]["port"] != 4242


def test_off_enforcement_a_restart_floors_the_file_and_writes_no_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, home = _seeded_pod_home(tmp_path, monkeypatch)
    doc = json.loads((home / "config.json").read_text(encoding="utf-8"))
    doc["tunnel"]["enabled"] = True
    (home / "config.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    assert rt.seed_home_from_scenario(cfg, "wt", "minimal") is False

    written = (home / "config.json").read_bytes()
    floored = json.loads(written)
    assert floored["tunnel"]["enabled"] is False
    assert written == json.dumps(floored, indent=2).encode("utf-8")
    assert not (home / lp.RECORD_DIR_LEAF).exists()
