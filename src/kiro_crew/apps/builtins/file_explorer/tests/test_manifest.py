"""Manifest platform-declaration tests for the File Explorer builtin app."""

import json
from pathlib import Path

APP_JSON = Path(__file__).resolve().parents[1] / "app.json"


def _manifest() -> dict:
    return json.loads(APP_JSON.read_text(encoding="utf-8"))


# --- manifest platform declaration ---
def test_manifest_declares_every_platform_the_app_runs_on():
    """`platform.os` summarises the whole app, and every surface it exposes —
    the directory tree, tabbed reads, markdown rendering, and folder search —
    is ordinary stdlib filesystem work that runs anywhere.

    Pinned because both narrower answers misinform: omitting the block falls
    back to the implicit ``["macos", "linux"]`` default, which silently drops
    Windows, and a hand-narrowed list reads as "does not run there" for a
    platform the code has no trouble on.
    """
    manifest = _manifest()
    assert manifest["platform"]["os"] == ["macos", "linux", "windows"]

    # The reachable-root set is carried in the UI copy, not the manifest gate,
    # and it must track reality: the roots are home + the system temp dir on
    # every platform, with /home and /opt added on POSIX only. The old copy
    # ("home plus /tmp and /opt") became false for Windows the moment the
    # declaration landed, since there is no C:\tmp and no C:\opt — the app
    # serves %TEMP% instead. This guards the copy against lying either way.
    reach = [h for h in manifest["highlights"] if "Reachable paths" in h]
    assert len(reach) == 1, manifest["highlights"]
    assert "%TEMP%" in reach[0], "the highlight must name the Windows temp dir"
    assert "on macOS and Linux" in reach[0], "/home and /opt must be marked POSIX-only"


def test_declared_platforms_all_resolve_to_a_real_sys_platform():
    """Every declared name must map to a sys.platform value.

    An unmapped name is silently accepted into the list and then never matches,
    so a declaration can claim a platform the gate rejects. `windows` was in
    exactly that state until the mapping row landed.
    """
    from kiro_crew.apps.manifest import PlatformConfig

    manifest = _manifest()
    cfg = PlatformConfig(os=manifest["platform"]["os"])
    for sys_platform in ("darwin", "linux", "win32"):
        assert cfg.supports_platform(sys_platform), sys_platform
