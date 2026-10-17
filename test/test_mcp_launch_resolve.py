"""Launch resolution uses a managed probe directory outside agent-writable overlays."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from kiro_crew.mcp_gateway import launch_resolve, rewriter

ENFORCE_LAUNCH_APPROVAL = True


def test_launch_probe_ignores_a_planted_overlay_symlink(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    real_overlay = home / "mcp-gateway" / "agents"
    old_scratch = real_overlay.parent / ".launch-probe-planted"
    old_scratch.mkdir(parents=True)
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (old_scratch / "agents").symlink_to(attacker, target_is_directory=True)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **_kwargs: str(old_scratch))
    monkeypatch.setattr(
        launch_resolve,
        "rewrite_kwargs",
        lambda _cfg, _stubs: {"overlay_dir": real_overlay},
    )
    observed: dict[str, Path] = {}
    expected_root = home / "run" / "mcp-tmp"

    def fake_rewrite_agents(**kwargs):
        overlay = Path(kwargs["overlay_dir"])
        observed["overlay"] = overlay
        overlay.relative_to(expected_root)
        overlay.mkdir(parents=True)
        (overlay / "probe.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(rewriter, "rewrite_agents", fake_rewrite_agents)
    cfg = SimpleNamespace(mcp_gateway=SimpleNamespace(stub_servers=frozenset()))

    assert "srv" not in launch_resolve.resolve_launches(["srv"], cfg=cfg, refresh_specs=False)
    assert list(attacker.iterdir()) == []
    assert not observed["overlay"].parent.exists()
