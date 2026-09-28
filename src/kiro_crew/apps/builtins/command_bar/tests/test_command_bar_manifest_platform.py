"""Windows admission for the Command Bar app.

Command Bar is overlay-only: the manifest declares no page, no backend entry point and
no cron, and every line of behaviour lives in the browser bundle under
``website/src/apps/command-bar/``. ``platform.os`` is a published capability
label, not an enable gate: ``apps/routes.py`` and ``apps/registry.py`` only call
``supports_platform`` for a ``platform.installMode == "client"`` app, which no
builtin -- including this one -- sets, so the declared OS list never actually
gated anything here.

Pinned because the failure it guarded against was silent even though the
mechanism was misdiagnosed. ``platform.os`` defaults to ``["macos", "linux"]``
(``kiro_crew/apps/manifest.py``), and this app is default-ON and ``replaces``
quick-search rather than adding a surface of its own. Declaring the omitted
default on a Windows host would have misreported the app as unsupported on the
App Store detail page, while this same manifest's ``configuration`` copy
promised "press Cmd+K or Ctrl+K". Ctrl+K is the Windows/Linux binding, so the
copy advertised Windows while the stale declaration would have denied it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

_APP_JSON = Path(__file__).resolve().parents[1] / "app.json"

_DECLARED_OS = ["macos", "linux", "windows"]


def _manifest() -> dict[str, Any]:
    return json.loads(_APP_JSON.read_text(encoding="utf-8"))


def test_windows_admission_rests_on_the_app_contributing_no_host_side_code() -> None:
    """Why declaring ``windows`` is safe rather than optimistic.

    Enabling this app spawns no process and imports no module on the gateway: the app
    directory carries no Python, and the manifest declares no backend entry point, no
    cron and no agent. There is thus no Windows failure surface to port -- and, since
    Kiro Crew has no native OS sandbox backend on Windows, nothing here that would
    fail-close on ``SandboxUnavailableError`` either.

    If a backend ever lands, this test is the place that says the Windows claim must be
    re-argued instead of inherited.
    """
    manifest = _manifest()
    # `__init__.py` is exempt BY CONTENT, not by name. The app directory has to be a
    # package: `setup.cfg` sets `testpaths = test src/kiro_crew/apps/builtins`, so this
    # app's own `tests/` is collected, and several apps ship identically-named test
    # modules — without the package marker those basenames collide and pytest fails
    # collection repo-wide. So the marker is required, and the claim this test defends
    # is that it stays INERT: parsing it must yield no statement that runs. A real
    # module (an import, an assignment, a `register_routes` re-export) fails here, which
    # is the surface the docstring above is about.
    init_py = _APP_JSON.parent / "__init__.py"
    if init_py.exists():
        tree = ast.parse(init_py.read_text(encoding="utf-8"))
        executable = [
            node
            for node in tree.body
            if not (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            )  # a bare docstring is inert; comments never reach the AST
        ]
        assert executable == [], (
            "command_bar/__init__.py must stay an inert package marker (comments or a "
            "docstring only). It gained executable code, so the app now contributes "
            f"gateway-side Python: {[type(n).__name__ for n in executable]}"
        )
    stray_python = sorted(p.name for p in _APP_JSON.parent.glob("*.py") if p.name != "__init__.py")
    assert stray_python == [], (
        "Command Bar gained gateway-side Python; re-examine the platform.os claim "
        f"before shipping it on Windows: {stray_python}"
    )
    assert not manifest.get("backend", {}).get("entryPoint", "")
    assert not manifest.get("crons")
    assert not manifest.get("agents")
    assert manifest.get("ui", {}).get("pages", []) == []


def test_configuration_copy_and_platform_declaration_agree() -> None:
    """The Ctrl+K promise in the copy is only true once ``windows`` is declared.

    Guards the contradiction in both directions: dropping ``windows`` from the gate, or
    dropping the Windows/Linux binding from the copy, must fail here.
    """
    manifest = _manifest()
    assert manifest["defaultEnabled"] is True
    assert [(o.get("id"), o.get("replaces")) for o in manifest["ui"]["overlays"]] == [
        ("command-bar", "quick-search")
    ]
    copy = " ".join(manifest["configuration"])
    assert "Ctrl+K" in copy
    assert "windows" in manifest["platform"]["os"]
