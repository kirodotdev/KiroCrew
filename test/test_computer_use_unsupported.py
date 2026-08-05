"""Computer use on an unsupported platform — import safety and clean refusals.

Two claims are asserted here, and they are what let this whole package ship in a
wheel that installs on Linux and Windows:

* **Import safety (structural).** Importing ANY module in the package — including
  ``macos_ffi`` — must succeed off macOS, because no ``CDLL`` / ``find_library``
  runs at module scope. This is the second of the two mechanisms that keep CI off
  the native path (the first is the suite-wide fake registration, pinned in
  ``test_computer_use_backend.py``).
* **Clean refusals.** With ``IS_MACOS`` monkeypatched False, every tool returns a
  coherent ``"Error: ..."`` string naming the platform instead of raising — the same
  posture ``dashboard/handlers/terminal.py`` takes for the Windows PTY.

The whole file runs on every shard: the platform is simulated by flipping
``platform_compat`` flags, never by needing that OS.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.computer_use import backend as backend_mod
from kiro_crew.computer_use import enable_state
from kiro_crew.computer_use import service as service_mod
from kiro_crew.computer_use import tools as tools_mod
from kiro_crew.computer_use.backend import (
    UnsupportedBackend,
    register_computer_use_backend,
    reset_shared_backend,
    select_default_backend,
)
from kiro_crew.computer_use.types import (
    ALL_TOOLS,
    ERROR_PREFIX,
    PERMISSION_UNSUPPORTED,
    PLATFORM_LINUX,
    PLATFORM_WINDOWS,
    STATE_KEY_ENABLED,
    ComputerUseUnsupported,
)
from kiro_crew.config import loader as config_loader
from kiro_crew.testing.fake_computer_use import FakeComputerUseBackend

_PACKAGE_ROOT = Path(inspect.getfile(backend_mod)).parent
# Every module in the package, by dotted name — enumerated from disk so a module
# added later is covered automatically rather than needing this list edited.
_MODULE_NAMES = tuple(
    f"kiro_crew.computer_use.{path.stem}"
    for path in sorted(_PACKAGE_ROOT.glob("*.py"))
    if path.stem != "__init__"
)


# ──────────────────────────────────────────────────────────────────────────
# Import safety
# ──────────────────────────────────────────────────────────────────────────
class TestImportSafety:
    @pytest.mark.parametrize("module_name", _MODULE_NAMES)
    def test_every_module_imports_on_any_platform(self, module_name):
        """Including ``macos_ffi``.

        ``import ctypes`` at module scope is fine (AUTOSDE's top-level-imports rule
        is about statements, not about LOADING a library); what must not happen is a
        module-scope ``CDLL``, which would fail — or worse, half-succeed — on a Linux
        runner and break collection of every test that transitively touches
        ``kiro_crew``.
        """
        assert importlib.import_module(module_name) is not None

    def test_no_module_scope_native_library_load(self):
        """The structural half of the CI guarantee, by AST.

        A behavioral test cannot prove this: on a macOS dev box a module-scope
        ``CDLL`` would succeed and nothing would fail until the Linux shard ran. So
        the assertion is that the CALL does not appear at module level at all.
        """
        loaders = {"CDLL", "find_library", "LoadLibrary", "PyDLL", "WinDLL"}
        offenders: list[str] = []
        for path in sorted(_PACKAGE_ROOT.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:  # MODULE level only — inside a function is fine
                for inner in ast.walk(node):
                    if not isinstance(inner, ast.Call):
                        continue
                    func = inner.func
                    name = getattr(func, "attr", None) or getattr(func, "id", None)
                    if name in loaders and not isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        offenders.append(f"{path.name}:{inner.lineno} {name}")
        assert offenders == [], offenders

    def test_package_import_does_not_touch_the_filesystem_or_a_driver(self):
        # A side-effect-free import is what lets the dashboard render a Settings row
        # on a machine with no driver at all.
        source = (_PACKAGE_ROOT / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [
            node
            for node in tree.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        ]
        assert calls == []

    def test_macos_ffi_frameworks_refuse_off_macos(self, monkeypatch):
        """The load guard lives INSIDE the loader function, and it fails loudly.

        A typed ``ComputerUseUnsupported`` is what the backend converts into a
        refusal; a bare ``OSError`` from ``CDLL`` would escape as an internal error.
        """
        from kiro_crew.computer_use import macos_ffi

        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(macos_ffi, "_libs", None)
        with pytest.raises(ComputerUseUnsupported):
            macos_ffi.frameworks()

    def test_package_contains_no_subprocess_spawn(self):
        """No spawn node in the package, with ONE audited exception.

        The in-process ImageIO capture replaced the only ``screencapture``
        shell-out, so nothing in the observation/input path shells out — and a
        reintroduced spawn (e.g. an ``osascript`` shortcut) would be an ungoverned
        shell plane inside the feature that exists to avoid one.

        ``overlay.py`` is the exception, and the exemption is narrow by
        construction. It launches the Cursor Motion renderer, which MUST be a
        separate process: AppKit needs a main-thread run loop and the gateway's main
        thread is the asyncio loop. The spawn is pinned by
        ``test_overlay_spawn_is_a_fixed_module_launch`` below to a fixed
        ``sys.executable -m <module>`` argv with no shell, no agent-supplied
        argument, and no PATH lookup. See also its ``BENIGN_SPAWNS`` entry in
        ``test_spawn_audit.py``.
        """
        # ``os.system`` / ``os.popen`` invoke a SHELL and are forbidden everywhere in
        # the package, overlay included — there is no legitimate use for either.
        offenders: list[str] = []
        for path in sorted(_PACKAGE_ROOT.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for token in ("os.system(", "os.popen(", "shell=True"):
                if token in source:
                    offenders.append(f"{path.name}: {token}")
            if path.name == "overlay.py":
                continue
            for token in ("subprocess.", "create_subprocess_"):
                if token in source:
                    offenders.append(f"{path.name}: {token}")
        assert offenders == [], offenders

    def test_overlay_spawn_is_a_fixed_module_launch(self):
        """The one permitted spawn carries nothing agent-supplied.

        Asserted structurally: the argv list literal must be exactly
        ``[sys.executable, "-m", OVERLAY_MODULE]``. The only agent-influenced values
        in the whole overlay subsystem are numeric coordinates, and they travel as
        JSON on the child's stdin — a spawn that started interpolating anything into
        its argv would be a new, unreviewed injection surface.
        """
        tree = ast.parse((_PACKAGE_ROOT / "overlay.py").read_text(encoding="utf-8"))
        argvs = [
            ast.unparse(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "argv" for t in node.targets)
        ]
        assert argvs == ["[sys.executable, '-m', OVERLAY_MODULE]"], argvs


# ──────────────────────────────────────────────────────────────────────────
# Refusals with no driver
# ──────────────────────────────────────────────────────────────────────────
@pytest.fixture
def unsupported_backend(monkeypatch):
    """Pin the process to a driverless platform and install its backend."""
    monkeypatch.setattr(platform_compat, "IS_MACOS", False)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    register_computer_use_backend(None)
    reset_shared_backend()
    service_mod.reset_shared_service()
    yield select_default_backend()
    # Restore the suite-wide fake for the rest of this module.
    register_computer_use_backend(FakeComputerUseBackend)
    reset_shared_backend()
    service_mod.reset_shared_service()


@pytest.fixture
def enabled_keystone(tmp_path, monkeypatch):
    """Turn the primary enable ON so a refusal must come from the PLATFORM.

    Without this the dispatcher's first check (the keystone) would short-circuit and
    the test would prove nothing about platform support.
    """
    import json

    path = tmp_path / "computer_use.json"
    path.write_text(json.dumps({STATE_KEY_ENABLED: True}), encoding="utf-8")
    monkeypatch.setattr(config_loader, "computer_use_state_path", lambda: path)
    assert enable_state.is_enabled() is True
    return path


class TestRefusalsOnUnsupportedPlatform:
    def test_windows_selects_the_windows_placeholder(self, unsupported_backend):
        assert isinstance(unsupported_backend, UnsupportedBackend)
        assert unsupported_backend.platform_id == PLATFORM_WINDOWS

    def test_status_is_unsupported_with_an_actionable_reason(self, unsupported_backend):
        status = unsupported_backend.status()
        assert status.supported is False
        # Concrete rather than "not supported": a user should learn what is missing
        # and a maintainer should find the next implementation step named.
        assert "UI Automation" in status.reason
        assert "macOS-only" in status.reason

    def test_linux_reason_names_the_wayland_capture_problem(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        driver = select_default_backend()
        assert driver.platform_id == PLATFORM_LINUX
        assert "Wayland" in driver.status().reason

    def test_permission_probe_reports_unsupported_not_missing(self, unsupported_backend):
        # "missing" would send the user to a System Settings pane that cannot help.
        probe = unsupported_backend.probe_permissions()
        assert probe.accessibility == PERMISSION_UNSUPPORTED
        assert probe.screen_recording == PERMISSION_UNSUPPORTED

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ALL_TOOLS)
    async def test_every_tool_refuses_without_raising(
        self, tool_name, unsupported_backend, enabled_keystone
    ):
        """All nine tools, one uniform refusal, no traceback.

        Args are deliberately minimal-but-valid so the refusal comes from the
        platform rather than from schema validation.
        """
        args = _minimal_args(tool_name)
        result = await tools_mod.dispatch(tool_name, args, session_key="dashboard:slot1")
        assert isinstance(result, str)
        if tool_name == "computer_end_turn":
            # The control-plane tool touches no other application, so it legitimately
            # succeeds with no driver: it only drops KiroCrew's OWN cached snapshots.
            assert not result.startswith(ERROR_PREFIX), result
            return
        assert result.startswith(ERROR_PREFIX), result
        # No internal traceback, no raw exception text.
        assert "Traceback" not in result

    @pytest.mark.asyncio
    async def test_refusal_names_the_platform(self, unsupported_backend, enabled_keystone):
        result = await tools_mod.dispatch("computer_list_apps", {}, session_key="dashboard:slot1")
        assert PLATFORM_WINDOWS in result

    @pytest.mark.asyncio
    async def test_disabled_keystone_beats_the_platform_refusal(
        self, unsupported_backend, tmp_path, monkeypatch
    ):
        """Order matters: the primary enable is checked FIRST.

        A user who has not opted in should be told to opt in, not told their OS is
        unsupported — and, more importantly, a disabled feature must not reach the
        driver at all.
        """
        monkeypatch.setattr(
            config_loader, "computer_use_state_path", lambda: tmp_path / "absent.json"
        )
        result = await tools_mod.dispatch("computer_list_apps", {}, session_key="dashboard:slot1")
        assert result.startswith(ERROR_PREFIX)
        assert "disabled" in result
        assert PLATFORM_WINDOWS not in result

    @pytest.mark.asyncio
    async def test_unknown_tool_is_refused_before_validation(
        self, unsupported_backend, enabled_keystone
    ):
        # An unregistered tool would pass RAW through validation, and a
        # ValidationError raised deeper inside a handler would escape the stdio loop
        # and kill the server.
        result = await tools_mod.dispatch("computer_teleport", {}, session_key="dashboard:slot1")
        assert result.startswith(ERROR_PREFIX)

    @pytest.mark.asyncio
    async def test_an_empty_session_key_is_refused_for_the_PLATFORM_reason(
        self, unsupported_backend, enabled_keystone
    ):
        """An unresolved identity is no longer a refusal of its own.

        The unattended-surface rule is gone, so a call with no session key reaches
        the driver and is refused for the reason that actually applies here — the
        platform has no driver. Pinned because the refusal must still name a cause
        the operator can act on, rather than degrading to a generic error.
        """
        result = await tools_mod.dispatch("computer_list_apps", {}, session_key="")
        assert result.startswith(ERROR_PREFIX)
        assert "not supported" in result


def _minimal_args(tool_name: str) -> dict:
    """The smallest schema-valid argument set for *tool_name*."""
    if tool_name in ("computer_list_apps", "computer_end_turn"):
        return {}
    args: dict = {"app": "Preview"}
    if tool_name == "computer_get_state":
        return args
    if tool_name == "computer_press_key":
        return {**args, "key": "cmd+s"}
    if tool_name == "computer_type_text":
        return {**args, "text": "hello"}
    if tool_name == "computer_drag":
        # Coordinate-only, and all four required: a drag has no element form.
        return {**args, "from_x": 10, "from_y": 20, "to_x": 30, "to_y": 40}
    args["element_index"] = 0
    if tool_name == "computer_set_value":
        args["value"] = "x"
    if tool_name == "computer_scroll":
        args["direction"] = "down"
    if tool_name == "computer_perform_action":
        args["action"] = "AXPress"
    return args
