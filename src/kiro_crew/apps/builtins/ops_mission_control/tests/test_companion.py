"""Tests for companion adapter discovery.

Ordered by what would hurt most if broken:

1. **Rejected code never runs.** Admission is evaluated BEFORE ``ep.load()``. If
   that order inverts, a banned package executes and the gate is decorative.
2. **A broken evaluator denies.** "The gate broke" must never read as "yes".
3. **ADD-only still holds.** A companion cannot shadow a core adapter id.
4. **A bad companion is never fatal.** It only adds signal sources; aborting the
   gateway over one would take down a working public install.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from kiro_crew.apps.builtins.ops_mission_control.backend import companion, registry


class _FakeEP:
    """Stands in for importlib.metadata.EntryPoint."""

    def __init__(self, name: str, loaded: Any = None, *, raises: Exception | None = None) -> None:
        self.name = name
        self.value = f"fake_pkg.{name}:register_adapters"
        self._loaded = loaded
        self._raises = raises
        self.load_called = False

    def load(self) -> Any:
        self.load_called = True
        if self._raises is not None:
            raise self._raises
        return self._loaded


class _FakeSource:
    """Minimal SignalSource-shaped adapter."""

    def __init__(self, source_id: str = "internal-tickets") -> None:
        self._id = source_id

    @property
    def id(self) -> str:
        return self._id

    @property
    def display_name(self) -> str:
        return f"Fake {self._id}"

    def configured(self) -> bool:
        return True

    async def poll(self, *a: Any, **kw: Any) -> list[Any]:
        return []


def _find_signal(reg: Any, source_id: str) -> Any:
    """Look up a signal source by id.

    The registry exposes signal sources as a LIST (only action sinks have a
    singular accessor), so this does the lookup the tests need without adding a
    method to the production class purely for testing.
    """
    return next((s for s in reg.signal_sources() if s.id == source_id), None)


def _allow(_ep: Any, _policy: Any) -> Any:
    return mock.Mock(allowed=True, reason="allowlisted")


def _deny(_ep: Any, _policy: Any) -> Any:
    return mock.Mock(allowed=False, reason="not in fleet allowlist")


class _AdmissionPatch:
    """Patch the platform admission functions AT THEIR USE SITE in companion.py.

    `companion` imports these at module scope, so it binds its own names
    (`companion.evaluate_admission`, …); patching the source module
    `kiro_crew.platform.admission` would not affect the already-bound references. Patch where
    the name is looked up, which is the module doing the calling.
    """

    def __init__(self, evaluator: Any) -> None:
        self._patches = [
            mock.patch.object(companion, "evaluate_admission", evaluator),
            mock.patch.object(companion, "load_admission_policy", lambda: mock.Mock()),
            mock.patch.object(companion, "seed_default_policy", lambda: True),
        ]

    def __enter__(self) -> None:
        for p in self._patches:
            p.start()

    def __exit__(self, *exc: object) -> None:
        for p in self._patches:
            p.stop()


class TestAdmissionGate(unittest.TestCase):
    def test_rejected_companion_is_never_loaded(self) -> None:
        """The whole point: decide before importing, or the gate does nothing."""
        ep = _FakeEP("banned", loaded=lambda reg: None)
        reg = registry.OpsProviderRegistry()
        with mock.patch.object(companion, "provider_entry_points", return_value=[ep]):
            with _AdmissionPatch(_deny):
                count = companion.install_companion_adapters(reg)
        self.assertEqual(count, 0)
        self.assertFalse(ep.load_called, "rejected plugin code must never execute")

    def test_admitted_companion_registers(self) -> None:
        called: list[Any] = []

        def register(reg: Any) -> None:
            called.append(reg)
            reg.register_signal_source(_FakeSource())

        ep = _FakeEP("internal", loaded=register)
        reg = registry.OpsProviderRegistry()
        with mock.patch.object(companion, "provider_entry_points", return_value=[ep]):
            with _AdmissionPatch(_allow):
                count = companion.install_companion_adapters(reg)
        self.assertEqual(count, 1)
        self.assertEqual(len(called), 1)
        self.assertIsNotNone(_find_signal(reg, "internal-tickets"))

    def test_broken_evaluator_denies(self) -> None:
        """A gate that errors must not admit. This is the one fail-CLOSED path."""

        def explode(_ep: Any, _policy: Any) -> Any:
            raise RuntimeError("policy file corrupt")

        ep = _FakeEP("x", loaded=lambda reg: None)
        reg = registry.OpsProviderRegistry()
        with mock.patch.object(companion, "provider_entry_points", return_value=[ep]):
            with _AdmissionPatch(explode):
                count = companion.install_companion_adapters(reg)
        self.assertEqual(count, 0)
        self.assertFalse(ep.load_called)

    def test_decisions_are_audited(self) -> None:
        """A silent skip makes 'rejected' indistinguishable from 'not installed'."""
        ep = _FakeEP("banned", loaded=lambda reg: None)
        reg = registry.OpsProviderRegistry()
        with mock.patch.object(companion, "provider_entry_points", return_value=[ep]):
            with _AdmissionPatch(_deny):
                with mock.patch.object(companion, "sel") as fake_sel:
                    companion.install_companion_adapters(reg)
        self.assertTrue(fake_sel.called, "an admission denial must reach the audit trail")


class TestFailOpen(unittest.TestCase):
    """A companion only ADDS sources; it must never take the gateway down."""

    def _install(self, ep: _FakeEP) -> int:
        reg = registry.OpsProviderRegistry()
        with mock.patch.object(companion, "provider_entry_points", return_value=[ep]):
            with _AdmissionPatch(_allow):
                return companion.install_companion_adapters(reg)

    def test_import_failure_is_survived(self) -> None:
        self.assertEqual(self._install(_FakeEP("x", raises=ImportError("no module"))), 0)

    def test_non_callable_target_is_survived(self) -> None:
        self.assertEqual(self._install(_FakeEP("x", loaded={"not": "callable"})), 0)

    def test_raising_register_is_survived(self) -> None:
        def boom(_reg: Any) -> None:
            raise ValueError("companion bug")

        self.assertEqual(self._install(_FakeEP("x", loaded=boom)), 0)

    def test_no_entry_points_is_a_quiet_zero(self) -> None:
        """The overwhelmingly common case: a public install with no companion."""
        reg = registry.OpsProviderRegistry()
        with mock.patch.object(companion, "provider_entry_points", return_value=[]):
            self.assertEqual(companion.install_companion_adapters(reg), 0)

    def test_one_bad_companion_does_not_block_a_good_one(self) -> None:
        def boom(_reg: Any) -> None:
            raise ValueError("bad")

        def good(reg: Any) -> None:
            reg.register_signal_source(_FakeSource("good-src"))

        reg = registry.OpsProviderRegistry()
        eps = [_FakeEP("bad", loaded=boom), _FakeEP("good", loaded=good)]
        with mock.patch.object(companion, "provider_entry_points", return_value=eps):
            with _AdmissionPatch(_allow):
                count = companion.install_companion_adapters(reg)
        self.assertEqual(count, 1)
        self.assertIsNotNone(_find_signal(reg, "good-src"))

    def test_discovery_failure_does_not_break_get_registry(self) -> None:
        """Registry construction must survive a broken discovery path entirely."""
        registry.reset_registry()
        with mock.patch.object(
            companion, "provider_entry_points", side_effect=RuntimeError("metadata broken")
        ):
            reg = registry.get_registry()
        # Public adapters still present — the app works without any companion.
        self.assertIsNotNone(_find_signal(reg, "cloudwatch"))
        registry.reset_registry()


class TestAddOnlyStillHolds(unittest.TestCase):
    def test_companion_cannot_shadow_a_core_adapter(self) -> None:
        """If a companion could take `cloudwatch`, auditing the core would require
        auditing every companion too."""

        def register(reg: Any) -> None:
            reg.register_signal_source(_FakeSource("cloudwatch"))

        registry.reset_registry()
        ep = _FakeEP("evil", loaded=register)
        with mock.patch.object(companion, "provider_entry_points", return_value=[ep]):
            with _AdmissionPatch(_allow):
                reg = registry.get_registry()
        core = _find_signal(reg, "cloudwatch")
        self.assertIsNotNone(core)
        self.assertNotIsInstance(core, _FakeSource, "core adapter must win")
        registry.reset_registry()

    def test_public_adapters_are_installed_before_companions(self) -> None:
        """Order is what makes ADD-only meaningful, so pin it directly."""
        order: list[str] = []
        registry.reset_registry()
        with mock.patch.object(
            registry, "_install_public_adapters", side_effect=lambda r: order.append("public")
        ):
            with mock.patch.object(
                registry, "_install_companions", side_effect=lambda r: order.append("companion")
            ):
                registry.get_registry()
        self.assertEqual(order, ["public", "companion"])
        registry.reset_registry()


class TestGroupSeparation(unittest.TestCase):
    def test_ops_group_is_not_the_platform_plugin_group(self) -> None:
        """Contributing an ops adapter must not imply platform-edition authority."""
        from kiro_crew.platform.discovery import PLUGIN_GROUP

        self.assertNotEqual(companion.PROVIDER_GROUP, PLUGIN_GROUP)

    def test_summary_does_not_load_plugin_code(self) -> None:
        """The Settings readout reports what is INSTALLED, without executing it."""
        ep = _FakeEP("internal", loaded=lambda reg: None)
        with mock.patch.object(companion, "provider_entry_points", return_value=[ep]):
            rows = companion.companion_summary()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "internal")
        self.assertFalse(ep.load_called)


if __name__ == "__main__":
    unittest.main()
