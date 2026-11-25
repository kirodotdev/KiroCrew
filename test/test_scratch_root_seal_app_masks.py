"""An app's declared credential masks beside the scratch root sealed around its windows.

An app's ``credentialMasks`` (``apps.credential_masks``) name FILES under its own data
directory, ``apps/<app>/data/<entry>`` and the lifecycle's ``apps/.<app>-data-tmp/<entry>``,
at every spelling a core hidden leaf has. ``_live_plan_host`` appends them after the core
leaves, so they come after the scratch root in the ``$HOME`` and the relocated spellings
alike; none contains the root or sits inside it or one of its windows, and no declaration
reaches ``PlanHost.sealed_mask_dirs``. These cases run the production planner and then the
launcher's own stages with the renaming fake libc of ``test_sandbox_launcher_program``, so a
change that made a declared mask seal, move or cover the root fails here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from test_sandbox_launcher_program import CoveringLibc, launch, payload
from test_scratch_root_seal import (
    _LINUX_ONLY,
    _read_only_remounts,
    read_back_read_only_remounts,
)

from kiro_crew import sandbox
from kiro_crew import sandbox_launcher_program as program
from kiro_crew import sandbox_plan
from kiro_crew.apps import credential_masks
from kiro_crew.sandbox_plan import BACKEND_NAMESPACE

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")

_PREFIXES = (".kiro/crew", ".kirocrew")


@pytest.fixture(autouse=True)
def read_only_remounts(_floor_monkeypatch: pytest.MonkeyPatch) -> set[tuple[int, int]]:
    """What the fake libc remounted read-only reads back as read-only (``confirm_sealed_roots``)."""
    return read_back_read_only_remounts(_floor_monkeypatch)


@pytest.fixture(name="crew")
def crew_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A live data home under *tmp_path*, read by the planner and by the mask reader.

    Every path the live adapter reads is under *tmp_path*, so the launcher stages below,
    which rename what they mask, never reach the host's own tree.
    """
    home = tmp_path / "home"
    crew = home / ".kiro" / "crew"
    crew.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    for name in ("KIROCREW_POD", "KIROCREW_OS_HOME", "KIRO_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sandbox, "config_dir", lambda: crew)
    monkeypatch.setattr(credential_masks, "config_dir", lambda: crew)
    monkeypatch.setattr(sandbox, "kiro_agents_dir", lambda: home / ".kiro" / "agents")
    run = crew / "run"
    voice = run / "voice-runtime"
    monkeypatch.setattr(
        sandbox,
        "_voice_runtime_paths_cache",
        (str(crew), str(voice), (str(voice),), (str(run),), (str(run), str(crew))),
    )
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: False)
    credential_masks.clear_cache()
    yield crew
    credential_masks.clear_cache()


def _declare(crew: Path, *entries: str) -> Path:
    """Install app ``demo`` declaring *entries*, with its first entry present; return the file."""
    app = crew / "apps" / "demo"
    (app / "data").mkdir(parents=True)
    manifest = {"name": "demo", "version": "1.0.0", "credentialMasks": list(entries)}
    (app / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    secret = app / "data" / entries[0]
    secret.write_text("token", encoding="utf-8")
    return secret


def _windows(crew: Path) -> tuple[Path, Path, Path]:
    """The scratch root and its two windows, on disk."""
    root = crew / "scratch"
    own, tree = root / "runtime-own", root / "runtime-tree"
    own.mkdir(parents=True)
    tree.mkdir()
    return root, own, tree


@pytest.mark.parametrize("tier", ["standard", "strict", "cc"])
def test_a_declared_mask_is_masked_after_the_root_and_never_sealed(crew: Path, tier: str) -> None:
    root, own, tree = _windows(crew)
    windows = (str(own), str(tree))
    before = sandbox._spawn_plan(BACKEND_NAMESPACE, tier, extra_private_dirs=windows)
    secret = _declare(crew, "secrets.json", "tokens/oauth.json")
    plan = sandbox._spawn_plan(BACKEND_NAMESPACE, tier, extra_private_dirs=windows)

    home = str(Path.home())
    declared = [
        os.path.join(home, f"{prefix}/apps/{folder}/{entry}")
        for prefix in _PREFIXES
        for folder in ("demo/data", ".demo-data-tmp")
        for entry in ("secrets.json", "tokens/oauth.json")
    ]
    dirs = list(plan.sensitive_dirs)
    assert str(secret) in declared and set(declared) <= set(dirs)
    assert plan.seal_windowed_masks == before.seal_windowed_masks
    assert str(root) in plan.seal_windowed_masks
    assert not set(declared) & set(plan.seal_windowed_masks)
    last_root = max(dirs.index(sealed) for sealed in plan.seal_windowed_masks)
    assert min(dirs.index(path) for path in declared) > last_root
    assert plan.windows == windows


@pytest.mark.parametrize("tier", ["standard", "strict", "cc"])
def test_the_apps_own_backend_lift_leaves_the_seal_in_place(crew: Path, tier: str) -> None:
    """The backend gets its declared files back; the scratch root stays sealed for it too."""
    root, own, _tree = _windows(crew)
    secret = _declare(crew, "secrets.json")
    lifted = sandbox.app_declared_mask_targets("demo", builtin=False)
    assert str(secret) in lifted
    plan = sandbox._spawn_plan(
        BACKEND_NAMESPACE, tier, extra_private_dirs=(str(own),), extra_visible_dirs=lifted
    )
    assert str(secret) not in plan.sensitive_dirs
    assert str(root) in plan.sensitive_dirs and str(root) in plan.seal_windowed_masks


@_LINUX_ONLY
def test_the_launcher_seals_the_root_and_masks_the_declared_file(
    crew: Path, tmp_path: Path
) -> None:
    root, own, tree = _windows(crew)
    secret = _declare(crew, "secrets.json")
    plan = sandbox._spawn_plan(
        BACKEND_NAMESPACE, "standard", extra_private_dirs=(str(own), str(tree))
    )
    wire = sandbox_plan.namespace_payload(plan)
    lists = ("sensitive_dirs", "sensitive_files", "private_dirs", "seal_windowed_masks")
    for key in lists:
        outside = [p for p in wire[key] if not p.startswith(str(tmp_path) + os.sep)]
        assert not outside, f"{key} reaches the host: {outside}"
    libc = CoveringLibc()
    run = launch(tmp_path, payload(**{key: wire[key] for key in lists}), libc=libc)

    assert program._mask_order(run) == list(run.sensitive_dirs)
    program.place_masks(run)
    program.retire_stand_in_stage(run)
    program.confirm_sealed_roots(run)

    seals = [libc.calls[i] for i in _read_only_remounts(libc)]
    assert [call.target_path for call in seals] == [str(root)]
    assert seals[0].target_id == run.masked_names[str(root)]
    assert sorted(os.listdir(root)) == ["runtime-own", "runtime-tree"]
    for window in (own, tree):
        (window / "kept").write_text("kept")
    assert secret.read_text(encoding="utf-8") == ""
    assert str(secret) in libc.covered
