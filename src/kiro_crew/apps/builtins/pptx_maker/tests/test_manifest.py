"""Platform declaration tests for the PPTX Maker app manifest."""

import json
from pathlib import Path

_MANIFEST = Path(__file__).resolve().parent.parent / "app.json"


def _manifest() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


# --- manifest platform declaration ---
def test_manifest_declares_every_platform_the_app_runs_on():
    """`platform.os` summarises the whole app, and every part of it runs anywhere.

    The app has no ``backend.entryPoint`` — only ``backend.routes`` — so its code
    runs inside the gateway process rather than in a spawned child. Deck
    authoring is filesystem + HTTP work, and the engine is a pinned Python
    package, so nothing in the path is POSIX-only.

    Pinned because both narrower answers misinform: ``["macos", "linux"]`` reads
    as "does not run on Windows", and omitting the block falls back to the
    implicit ``["macos", "linux"]`` default, which silently drops Windows too.
    """
    manifest = _manifest()
    assert manifest["platform"]["os"] == ["macos", "linux", "windows"]


def test_declared_platforms_all_resolve_to_a_real_sys_platform():
    """Every declared name must map to a sys.platform value.

    An unmapped name is silently accepted into the list and then never matches,
    so a declaration can claim a platform the gate rejects.
    """
    from kiro_crew.apps.manifest import PlatformConfig

    cfg = PlatformConfig(os=_manifest()["platform"]["os"])
    for sys_platform in ("darwin", "linux", "win32"):
        assert cfg.supports_platform(sys_platform), sys_platform
