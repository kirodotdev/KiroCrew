"""Windows support for the Auto-Improvement app is a manifest-level claim.

The app's own Python runs inside the gateway process (`backend` declares only
`routes`, no `entryPoint`), so what decides whether a Windows operator can
enable it at all is the `platform.os` list in `app.json`.
"""

import json
from pathlib import Path

import kiro_crew.apps.builtins.auto_improvement as app_pkg

_MANIFEST = Path(app_pkg.__file__).parent / "app.json"


def _manifest() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def test_manifest_declares_every_platform_the_app_runs_on():
    """`platform.os` must name windows explicitly.

    Pinned because both narrower answers misinform: omitting the block falls
    back to the implicit ``["macos", "linux"]`` default, which silently drops
    Windows, and a shorter list reads as "does not run there".
    """
    assert _manifest()["platform"]["os"] == ["macos", "linux", "windows"]


def test_declared_platforms_all_resolve_to_a_real_sys_platform():
    """Every declared name must map to a sys.platform value.

    An unmapped name is silently accepted into the list and then never matches,
    so a declaration can claim a platform the gate rejects.
    """
    from kiro_crew.apps.manifest import PlatformConfig

    cfg = PlatformConfig(os=_manifest()["platform"]["os"])
    for sys_platform in ("darwin", "linux", "win32"):
        assert cfg.supports_platform(sys_platform), sys_platform
