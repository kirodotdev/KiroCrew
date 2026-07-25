"""Parity gate for the desktop home-resolution mirror (issue-#483 fix class).

``website/electron/home-dir.js`` reimplements the backend's data-home
decision tree (``config/paths.py``) in a second language so Electron can
read ``config.json`` (port derivation) and ``.local_secret`` without
spawning Python first. Two-language mirrors drift silently, so this gate
pins them to one shared contract: ``test/fixtures/home-resolution-cases.json``.

Each case lays out a temp $HOME (legacy dir / canonical dir / completion
marker / env override), writes a DISTINCT sentinel ``config.json`` into
every home that exists, then:

  1. captures the sentinel at the fixture's ``expected_read_home`` -- the
     content Electron would read PRE-SPAWN via home-dir.js resolveHome();
  2. runs the REAL backend resolver (``config.paths.config_dir``), which
     performs the actual migration;
  3. asserts the ``config.json`` content at the backend's resolved home
     POST-MIGRATION equals the pre-spawn capture.

That equality is the real product contract: the bytes Electron reads before
spawning the gateway are the bytes the gateway serves after boot. If
``paths.py`` semantics change, step 3 fails here; updating the fixture then
fails ``website/electron/test/home-dir.test.js`` until ``home-dir.js``
follows. Neither side can drift alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.config import paths

FIXTURE = Path(__file__).parent / "fixtures" / "home-resolution-cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _lay_out_case(home: Path, case: dict, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Materialize one fixture case under ``home``; return home-kind -> dir."""
    dirs: dict[str, Path] = {}
    legacy = home / ".kirocrew"
    canonical = home / ".kiro" / "crew"
    if case["legacy"]:
        legacy.mkdir(parents=True)
        (legacy / "config.json").write_text('{"sentinel": "legacy"}', encoding="utf-8")
        dirs["legacy"] = legacy
    if case["canonical"]:
        canonical.mkdir(parents=True)
        (canonical / "config.json").write_text('{"sentinel": "canonical"}', encoding="utf-8")
        dirs["canonical"] = canonical
    if case["marker"]:
        assert case["canonical"], "fixture invariant: marker lives inside the canonical home"
        (canonical / paths.MIGRATION_MARKER_NAME).write_text("", encoding="utf-8")
    if case["env_override"]:
        override = home / "custom-home"
        override.mkdir(parents=True)
        (override / "config.json").write_text('{"sentinel": "override"}', encoding="utf-8")
        dirs["override"] = override
        monkeypatch.setenv("KIROCREW_HOME", str(override))
    return dirs


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_backend_serves_what_electron_read_prespawn(
    case: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(paths, "_resolved_home", None)
    monkeypatch.delenv("KIROCREW_HOME", raising=False)

    dirs = _lay_out_case(tmp_path, case, monkeypatch)

    # (1) What Electron's resolveHome() answers pre-spawn, per the shared
    # fixture. A "canonical" answer with no dir on disk yet (fresh install)
    # means "no config to read" -- Electron falls back to the default port,
    # and the backend writes a fresh home; both sides agree by construction.
    expected_kind = case["expected_read_home"]
    pre_spawn = dirs.get(expected_kind)
    pre_spawn_content = (
        (pre_spawn / "config.json").read_text(encoding="utf-8") if pre_spawn else None
    )

    # (2) The real backend resolution (runs the actual migration).
    resolved = paths.config_dir()

    # (3) Post-boot the backend must serve exactly what Electron read.
    post_boot = resolved / "config.json"
    if pre_spawn_content is None:
        assert (
            not post_boot.exists()
        ), "fresh install: backend must not conjure config content Electron never saw"
    else:
        assert post_boot.read_text(encoding="utf-8") == pre_spawn_content, (
            f"drift: Electron read {expected_kind!r} pre-spawn but the backend "
            f"serves different content from {resolved} post-migration -- "
            "home-dir.js and config/paths.py disagree; fix the mirror and/or "
            "update test/fixtures/home-resolution-cases.json"
        )
