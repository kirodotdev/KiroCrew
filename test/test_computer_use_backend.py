"""Computer use — the backend seam contract (ABC + registry + one platform branch).

Everything here runs on every CI shard, including the Linux and Windows ones,
because the whole point of the seam is that no native framework is loaded until
:func:`select_default_backend` is actually called — and the Windows / Linux
degradation paths are exercised by flipping a ``platform_compat`` flag rather than
by needing that OS.

The headline tripwire is ``test_ci_never_selects_a_native_backend``: the suite-wide
autouse fixture in ``conftest.py`` must keep ``get_shared_backend()`` on the fake,
so no test anywhere can capture a real window or touch a real application.
"""

from __future__ import annotations

import threading

import pytest

from kiro_crew import platform_compat
from kiro_crew.computer_use import backend as backend_mod
from kiro_crew.computer_use import index as index_mod
from kiro_crew.computer_use.backend import (
    LINUX_REASON,
    UNKNOWN_PLATFORM_REASON,
    WINDOWS_REASON,
    ComputerUseBackend,
    UnsupportedBackend,
    get_shared_backend,
    platform_id_for_current_os,
    register_computer_use_backend,
    reset_shared_backend,
    select_default_backend,
    unsupported_snapshot,
)
from kiro_crew.computer_use.types import (
    PERMISSION_UNSUPPORTED,
    PLATFORM_FAKE,
    PLATFORM_LINUX,
    PLATFORM_MACOS,
    PLATFORM_UNSUPPORTED,
    PLATFORM_WINDOWS,
    AppRef,
    ClickRequest,
    DragRequest,
    ElementRec,
    SnapshotRequest,
)
from kiro_crew.testing.fake_computer_use import FAKE_FILES_APP, FakeComputerUseBackend

# Every abstract method a driver must implement, as (name, args) pairs the
# ``UnsupportedBackend`` uniformity test drives.
_APP = AppRef(name="Probe", pid=1, bundle_id="dev.kirocrew.probe")
_REC = ElementRec(index=0, role="AXButton")
_DRIVER_CALLS = (
    ("list_apps", ()),
    ("resolve_app", ("Probe",)),
    ("snapshot", (_APP, SnapshotRequest())),
    ("click", (_APP, _REC, ClickRequest())),
    ("drag", (_APP, DragRequest(start=(10.0, 20.0), end=(30.0, 40.0)))),
    ("type_text", (_APP, _REC, "hello")),
    ("press_key", (_APP, _REC, "cmd+s")),
    ("set_value", (_APP, _REC, "value")),
    ("scroll", (_APP, _REC, "down", 1.0)),
    ("perform_action", (_APP, _REC, "AXPress")),
)


@pytest.fixture
def restore_registry():
    """Restore the suite-wide fake registration after a test overrides it.

    The module-scoped autouse fixture in ``conftest.py`` installs the fake once per
    module; a test that swaps it must put it back, or every later test in the same
    module silently runs against a different backend.
    """
    yield
    register_computer_use_backend(FakeComputerUseBackend)
    reset_shared_backend()


# ──────────────────────────────────────────────────────────────────────────
# The CI tripwire
# ──────────────────────────────────────────────────────────────────────────
def test_ci_never_selects_a_native_backend():
    """No test anywhere may reach a real accessibility API.

    Two independent mechanisms keep CI off the native path; this asserts the first
    (the suite-wide fake registration). The second is structural — no module-scope
    ``CDLL`` in the package — and is asserted in ``test_computer_use_unsupported.py``.
    """
    assert get_shared_backend().platform_id == PLATFORM_FAKE


def test_package_import_is_side_effect_free():
    """Importing the package must not build a backend or a snapshot index.

    A module-scope construction would load a native framework on every machine that
    merely imports ``kiro_crew``, breaking collection on the Linux fleet.
    """
    assert backend_mod._shared_backend is None or isinstance(
        backend_mod._shared_backend, ComputerUseBackend
    )
    # The registry's own state is a plain module global, never eagerly populated by
    # an import: only get_shared_backend() may construct.
    reset_shared_backend()
    assert backend_mod._shared_backend is None
    assert index_mod._shared_index is None


# ──────────────────────────────────────────────────────────────────────────
# Registry semantics
# ──────────────────────────────────────────────────────────────────────────
class TestRegistry:
    def test_register_overrides_and_none_restores(self, restore_registry):
        sentinel = UnsupportedBackend("sentinel", "probe")
        register_computer_use_backend(lambda: sentinel)
        reset_shared_backend()
        assert get_shared_backend() is sentinel
        # ``None`` restores the PLATFORM default (not the fake) — which is why the
        # fixture re-registers the fake afterwards.
        register_computer_use_backend(None)
        reset_shared_backend()
        assert get_shared_backend() is not sentinel

    def test_get_shared_backend_is_a_singleton(self):
        assert get_shared_backend() is get_shared_backend()

    def test_singleton_is_stable_under_concurrent_access(self, restore_registry):
        """One instance per process, even when threads race for it.

        Load-bearing rather than cosmetic: a driver holds cached framework handles
        and an event source, and the MCP loop dispatches on a worker thread while
        the main thread reads stdin.
        """
        built: list[int] = []

        def _factory():
            built.append(1)
            return FakeComputerUseBackend()

        register_computer_use_backend(_factory)
        reset_shared_backend()
        seen: list[ComputerUseBackend] = []
        barrier = threading.Barrier(8)

        def _worker():
            barrier.wait()
            seen.append(get_shared_backend())

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(built) == 1
        assert len({id(instance) for instance in seen}) == 1

    def test_reset_closes_the_previous_backend(self, restore_registry):
        instance = FakeComputerUseBackend()
        register_computer_use_backend(lambda: instance)
        reset_shared_backend()
        assert get_shared_backend() is instance
        reset_shared_backend()
        assert instance.closed is True

    def test_reset_drops_the_snapshot_index(self, restore_registry):
        """Dropping the cache is CORRECTNESS, not housekeeping.

        Element indices are only meaningful against the walk that produced them, so
        a new backend inheriting the previous one's snapshots could resolve an index
        to a completely different element.
        """
        fake = get_shared_backend()
        result = fake.snapshot(FAKE_FILES_APP, SnapshotRequest(want_image=False))
        assert result.snapshot is not None
        index_mod.get_shared_index().put(result.snapshot, session_key="dashboard:main")
        assert len(index_mod.get_shared_index()) == 1
        reset_shared_backend()
        # EVERY session's entries, not just one: a backend swap invalidates indices
        # for all surfaces, because the walk that produced them is gone.
        assert len(index_mod.get_shared_index()) == 0

    def test_close_failure_does_not_block_the_swap(self, restore_registry):
        """A driver that cannot release its handles must not pin itself in place.

        Leaving the old instance installed would be worse than leaking whatever it
        held.
        """

        class _Stubborn(FakeComputerUseBackend):
            def close(self) -> None:
                raise OSError("handle stuck")

        stubborn = _Stubborn()
        register_computer_use_backend(lambda: stubborn)
        reset_shared_backend()
        assert get_shared_backend() is stubborn
        reset_shared_backend()  # must not raise
        register_computer_use_backend(FakeComputerUseBackend)
        reset_shared_backend()
        assert get_shared_backend() is not stubborn


# ──────────────────────────────────────────────────────────────────────────
# The ABC payoff
# ──────────────────────────────────────────────────────────────────────────
class TestAbcContract:
    def test_incomplete_subclass_cannot_be_instantiated(self):
        """The reason the seam is an ``abc.ABC`` and not a duck-typed protocol.

        A half-written driver fails LOUDLY at construction rather than at the first
        tool call — which, for a security-relevant method like the secure-subrole
        read, would mean discovering the gap in production.
        """

        class _Half(ComputerUseBackend):
            @property
            def platform_id(self) -> str:
                return "half"

        with pytest.raises(TypeError):
            _Half()  # type: ignore[abstract]

    def test_every_abstract_method_is_declared(self):
        # A method missing from ``__abstractmethods__`` is a method a driver can
        # silently forget.
        expected = {
            "platform_id",
            "status",
            "probe_permissions",
            "list_apps",
            "resolve_app",
            "snapshot",
            "click",
            "drag",
            "type_text",
            "press_key",
            "set_value",
            "scroll",
            "perform_action",
            "close",
        }
        assert set(ComputerUseBackend.__abstractmethods__) == expected

    def test_fake_backend_satisfies_the_contract(self):
        # The shipped fake must stay a complete implementation: it is the reference
        # the whole suite runs against.
        assert isinstance(FakeComputerUseBackend(), ComputerUseBackend)


# ──────────────────────────────────────────────────────────────────────────
# UnsupportedBackend — uniform refusal, nothing raises
# ──────────────────────────────────────────────────────────────────────────
class TestUnsupportedBackend:
    @pytest.mark.parametrize("method,args", _DRIVER_CALLS, ids=[c[0] for c in _DRIVER_CALLS])
    def test_every_method_refuses_identically(self, method, args):
        driver = UnsupportedBackend("probeos", "no driver here")
        result = getattr(driver, method)(*args)
        assert result.ok is False
        # A coherent refusal naming the platform, NOT a traceback — the same posture
        # ``dashboard/handlers/terminal.py`` takes for the Windows PTY.
        assert "probeos" in result.text
        assert "no driver here" in result.text
        # The reason carries NO ``Error: `` prefix; the dispatch layer adds it once.
        assert not result.text.startswith("Error: ")

    def test_status_reports_unsupported_with_the_reason(self):
        driver = UnsupportedBackend("probeos", "no driver here")
        status = driver.status()
        assert status.supported is False
        assert status.platform_id == "probeos"
        assert status.reason == "no driver here"

    def test_permission_probe_reports_unsupported(self):
        probe = UnsupportedBackend("probeos", "x").probe_permissions()
        assert probe.accessibility == PERMISSION_UNSUPPORTED
        assert probe.screen_recording == PERMISSION_UNSUPPORTED

    def test_close_is_idempotent(self):
        driver = UnsupportedBackend("probeos", "x")
        driver.close()
        driver.close()

    def test_unsupported_snapshot_is_safe_by_construction(self):
        # A caller needing a ``Snapshot`` object must never hand-build one and leave
        # ``has_secure`` unset.
        snap = unsupported_snapshot(_APP)
        assert snap.elements == ()
        assert snap.has_secure is False
        assert snap.image_jpeg == b""
        assert snap.captured_at == 0.0


# ──────────────────────────────────────────────────────────────────────────
# select_default_backend — the ONLY platform branch
# ──────────────────────────────────────────────────────────────────────────
class TestPlatformSelection:
    @staticmethod
    def _pin(monkeypatch, *, macos=False, windows=False, linux=False):
        monkeypatch.setattr(platform_compat, "IS_MACOS", macos)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        monkeypatch.setattr(platform_compat, "IS_LINUX", linux)

    def test_windows_degradation_on_any_runner(self, monkeypatch):
        """How the Windows path is tested on a Linux runner: flip one flag.

        The branch reads ``platform_compat.IS_*`` rather than ``sys.platform``
        precisely so this is possible.
        """
        self._pin(monkeypatch, windows=True)
        driver = select_default_backend()
        assert driver.platform_id == PLATFORM_WINDOWS
        assert driver.status().supported is False
        assert driver.status().reason == WINDOWS_REASON

    def test_linux_degradation_on_any_runner(self, monkeypatch):
        self._pin(monkeypatch, linux=True)
        driver = select_default_backend()
        assert driver.platform_id == PLATFORM_LINUX
        assert driver.status().reason == LINUX_REASON

    def test_unknown_platform_degrades(self, monkeypatch):
        self._pin(monkeypatch)
        driver = select_default_backend()
        assert driver.platform_id == PLATFORM_UNSUPPORTED
        assert driver.status().reason == UNKNOWN_PLATFORM_REASON

    def test_macos_driver_import_failure_degrades_instead_of_raising(self, monkeypatch):
        """A partial install must disable ONE capability, not crash the process.

        The import is deferred into this function, so a broken framework surfaces
        here — where it can be converted into a typed refusal — rather than at
        package import time on every machine.
        """
        import builtins

        real_import = builtins.__import__

        def _fail(name, *args, **kwargs):
            if name == "kiro_crew.computer_use.macos_driver":
                raise ImportError("ApplicationServices unavailable")
            return real_import(name, *args, **kwargs)

        self._pin(monkeypatch, macos=True)
        monkeypatch.setattr(builtins, "__import__", _fail)
        driver = select_default_backend()
        assert driver.platform_id == PLATFORM_MACOS
        assert driver.status().supported is False
        assert "ApplicationServices unavailable" in driver.status().reason

    @pytest.mark.parametrize(
        "flags,expected",
        [
            ({"macos": True}, PLATFORM_MACOS),
            ({"windows": True}, PLATFORM_WINDOWS),
            ({"linux": True}, PLATFORM_LINUX),
            ({}, PLATFORM_UNSUPPORTED),
        ],
    )
    def test_platform_id_for_current_os_needs_no_driver(self, monkeypatch, flags, expected):
        # The Settings row must render on any OS without importing a driver.
        self._pin(monkeypatch, **flags)
        assert platform_id_for_current_os() == expected

    def test_platform_flags_are_read_only_where_documented(self):
        """The platform BRANCH is centralized: exactly one module may select.

        ``backend.py`` owns backend SELECTION and is the only module allowed to read
        all three flags — it is the exhaustive branch. Every other reader is a local
        macOS-only bail-out that may read ``IS_MACOS`` and nothing else, because
        branching on ``IS_WINDOWS``/``IS_LINUX`` outside the selector would be a
        second, competing selector that a future refactor could make disagree with
        the first.

        The permitted bail-outs, each reached WITHOUT going through the selector:

        * ``macos_ffi.py`` guards its framework load (raising
          ``ComputerUseUnsupported`` off macOS) — the one place a native load must be
          refused regardless of what the selector chose;
        * ``permissions.py`` is reached directly by the dashboard's ``doctor --json``
          probe, whose whole point is learning the TCC state WITHOUT loading a
          driver, so it must answer ``unsupported`` on its own;
        * ``overlay.py`` / ``overlay_proc.py`` are Cursor Motion, a COSMETIC
          subsystem that never consults the driver seam at all (it draws a fake
          pointer; it reads no window and captures no pixels), so it cannot learn
          "is this macOS?" from a backend it never asks. The supervisor declines to
          spawn and the renderer declines to build a window.

        An extra reader anywhere else fails here. Matched by AST attribute access,
        not substring: the driver docstrings NAME these flags to explain how the
        degradation path is tested, and prose must not trip a structural assertion.
        """
        import ast
        import inspect
        from pathlib import Path

        flags = {"IS_MACOS", "IS_WINDOWS", "IS_LINUX"}
        package_root = Path(inspect.getfile(backend_mod)).parent
        readers: dict[str, set[str]] = {}
        for path in sorted(package_root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found = {
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and node.attr in flags
            }
            found |= {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
                if alias.name in flags
            }
            if found:
                readers[path.name] = found
        # Modules allowed to read a platform flag directly. Each one guards a
        # NATIVE surface that must refuse rather than crash off its platform, which
        # is a different job from ``backend.py``'s selection branch.
        # ``macos_skylight.py`` is here for the sharpest version of that reason: it
        # dlopens a PRIVATE framework, so it has to answer "am I even on macOS"
        # before reporting a capability.
        guards = {
            "macos_ffi.py",
            "macos_skylight.py",
            "permissions.py",
            "overlay.py",
            "overlay_proc.py",
        }
        assert set(readers) <= {"backend.py"} | guards, readers
        # The selector must read all three (it is the exhaustive branch).
        assert readers.get("backend.py") == flags
        # Every other reader is a local "not my platform" bail-out, so IS_MACOS is
        # all it may read. Iterating the FOUND readers (not the allowlist) keeps the
        # rule applying to any new bail-out that gets added to the allowlist above.
        for guard in sorted(set(readers) & guards):
            assert readers[guard] <= {"IS_MACOS"}, (guard, readers[guard])
