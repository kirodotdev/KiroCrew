"""``platform.discovery`` — the fail-closed edges of companion discovery.

``test_platform_admission.TestDiscoveryGate`` pins the two ordinary outcomes (a
banned plugin is refused before ``load``, an admitted one composes). The edges it
leaves are all cases where getting the answer wrong silently downgrades the
edition or aborts boot:

* ``plugin_entry_points`` on the pre-``select`` metadata API, and its
  swallow-everything guard — a raise there would abort boot on an unrelated
  metadata problem;
* the standalone short-circuit, which must return ``None`` without consulting
  any entry point at all;
* the ambiguity refusal (more than one companion registered);
* the SEL emit around an admission DENY, which is best-effort and must not
  replace ``PluginAdmissionError`` with whatever SEL raised;
* the three composition-root outcomes: a ``PlatformCompositionError`` passes
  through unwrapped, any other exception is NORMALIZED into one (or ``cli.main``
  would swallow it and run degraded), and a ``None`` context is refused.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from kiro_crew.config.validation import extension_config_keys, replace_extension_config_keys
from kiro_crew.platform import discovery as discovery_mod
from kiro_crew.platform.admission import MODE_OPEN, AdmissionPolicy, PluginManifest
from kiro_crew.platform.context import PROFILE_STANDALONE, PlatformCompositionError
from kiro_crew.platform.discovery import (
    PLUGIN_GROUP,
    PluginAdmissionError,
    discover_companion_context,
    plugin_entry_points,
    refresh_installed_plugin_config_keys,
    refresh_plugin_config_keys,
)

#: The companion builder never touches the config, so no real one is needed.
#: Typed ``Any`` rather than passed as a bare ``None`` so the call sites stay
#: type-clean while still exercising the real signature.
_NO_CFG: Any = None


class _FakeEntryPoint:
    def __init__(self, name: str = "zibble", loaded: Any = None) -> None:
        self.name = name
        self.value = "m:build"
        self.group = PLUGIN_GROUP
        self._loaded = loaded

    def load(self) -> Any:
        return self._loaded


@pytest.fixture
def open_policy(monkeypatch: pytest.MonkeyPatch) -> AdmissionPolicy:
    """An open policy plus a stub manifest reader, so admission never touches disk."""
    monkeypatch.setattr(
        "kiro_crew.platform.admission._read_plugin_manifest",
        lambda ep: PluginManifest(name=ep.name, publisher="p13n", version="1"),
    )
    return AdmissionPolicy(mode=MODE_OPEN)


class TestPluginEntryPoints:
    def test_pre_select_metadata_api_selects_the_group_from_a_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The old API returns ``{group: [ep, ...]}``. Without the dict branch a host
        on that API sees zero plugins and then fails closed at boot."""
        ep = _FakeEntryPoint()
        monkeypatch.setattr(
            discovery_mod.importlib.metadata,
            "entry_points",
            lambda: {PLUGIN_GROUP: [ep]},
        )
        assert plugin_entry_points() == [ep]

    def test_pre_select_api_with_no_such_group_is_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            discovery_mod.importlib.metadata, "entry_points", lambda: {"other": [1]}
        )
        assert plugin_entry_points() == []

    def test_a_metadata_failure_degrades_to_no_plugins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> Any:
            raise RuntimeError("distribution metadata is unreadable")

        monkeypatch.setattr(discovery_mod.importlib.metadata, "entry_points", _boom)
        assert plugin_entry_points() == []


class TestStandaloneAndAmbiguity:
    def test_standalone_returns_none_without_looking_for_a_companion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _must_not_run() -> List[Any]:
            raise AssertionError("standalone must not enumerate entry points")

        monkeypatch.setattr(discovery_mod, "plugin_entry_points", _must_not_run)
        assert discover_companion_context(PROFILE_STANDALONE, _NO_CFG) is None

    def test_no_entry_point_on_a_non_standalone_profile_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(discovery_mod, "plugin_entry_points", lambda: [])
        with pytest.raises(PlatformCompositionError, match="fail-closed"):
            discover_companion_context("zibble", _NO_CFG)

    def test_two_registered_companions_are_refused_as_ambiguous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            discovery_mod,
            "plugin_entry_points",
            lambda: [_FakeEntryPoint("one"), _FakeEntryPoint("two")],
        )
        with pytest.raises(PlatformCompositionError) as excinfo:
            discover_companion_context("zibble", _NO_CFG)
        assert "ambiguous" in str(excinfo.value)
        assert "one" in str(excinfo.value) and "two" in str(excinfo.value)


def test_plugin_config_keys_refresh_uses_admitted_manifests_without_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("plugin config-key publication repeated discovery")

    monkeypatch.setattr(discovery_mod, "plugin_entry_points", _must_not_run)
    monkeypatch.setattr(discovery_mod, "evaluate_admission", _must_not_run)
    replace_extension_config_keys("plugins", ())
    try:
        manifest = PluginManifest(name="zibble", config_keys=["aim_update"])
        assert refresh_plugin_config_keys([manifest]) == frozenset({"aim_update"})
        assert extension_config_keys("plugins") == frozenset({"aim_update"})
    finally:
        replace_extension_config_keys("plugins", ())


def test_startup_plugin_refresh_publishes_only_admitted_manifest_without_loading_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_point = _FakeEntryPoint(
        "zibble",
        loaded=lambda _cfg: (_ for _ in ()).throw(AssertionError("plugin code loaded")),
    )
    manifest = PluginManifest(name="zibble", config_keys=["aim_update"])
    monkeypatch.setattr(discovery_mod, "plugin_entry_points", lambda: [entry_point])
    monkeypatch.setattr(discovery_mod, "read_policy_trust_root", lambda: AdmissionPolicy())
    monkeypatch.setattr("kiro_crew.platform.admission._read_plugin_manifest", lambda _ep: manifest)
    replace_extension_config_keys("plugins", ())
    try:
        assert refresh_installed_plugin_config_keys() == frozenset({"aim_update"})
        assert extension_config_keys("plugins") == frozenset({"aim_update"})

        monkeypatch.setattr(
            discovery_mod,
            "read_policy_trust_root",
            lambda: AdmissionPolicy(banned=["zibble"]),
        )
        assert refresh_installed_plugin_config_keys() == frozenset()
        assert extension_config_keys("plugins") == frozenset()
    finally:
        replace_extension_config_keys("plugins", ())


def test_companion_discovery_refreshes_from_existing_entry_point_snapshot(
    monkeypatch: pytest.MonkeyPatch, open_policy: AdmissionPolicy
) -> None:
    entry_point = _FakeEntryPoint("zibble", loaded=lambda _cfg: object())
    scans: list[int] = []

    def _entry_points() -> List[Any]:
        scans.append(1)
        return [entry_point]

    monkeypatch.setattr(discovery_mod, "plugin_entry_points", _entry_points)
    manifest_reads: list[str] = []

    def _read_manifest(ep: Any) -> PluginManifest:
        manifest_reads.append(ep.name)
        return PluginManifest(name=ep.name, config_keys=["aim_update"])

    monkeypatch.setattr("kiro_crew.platform.admission._read_plugin_manifest", _read_manifest)
    replace_extension_config_keys("plugins", ())
    try:
        assert discover_companion_context("zibble", _NO_CFG, policy=open_policy) is not None
        assert extension_config_keys("plugins") == frozenset({"aim_update"})
        assert scans == [1]
        assert manifest_reads == ["zibble"]
    finally:
        replace_extension_config_keys("plugins", ())


def test_rejected_plugin_does_not_publish_its_config_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_point = _FakeEntryPoint("zibble", loaded=lambda _cfg: object())
    monkeypatch.setattr(discovery_mod, "plugin_entry_points", lambda: [entry_point])
    monkeypatch.setattr(
        "kiro_crew.platform.admission._read_plugin_manifest",
        lambda ep: PluginManifest(name=ep.name, config_keys=["rejected_key"]),
    )
    replace_extension_config_keys("plugins", ())
    try:
        policy = AdmissionPolicy(mode=MODE_OPEN, banned=["zibble"])
        with pytest.raises(PluginAdmissionError):
            discover_companion_context("zibble", _NO_CFG, policy=policy)
        assert extension_config_keys("plugins") == frozenset()

        assert (
            discover_companion_context("zibble", _NO_CFG, policy=AdmissionPolicy(mode=MODE_OPEN))
            is not None
        )
        assert extension_config_keys("plugins") == frozenset({"rejected_key"})
    finally:
        replace_extension_config_keys("plugins", ())


class TestDenyAudit:
    def test_a_sel_emit_failure_does_not_replace_the_admission_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The DENY audit is best-effort: it runs on the bootstrap path where SEL
        may not be wired, and its failure must not mask the refusal."""
        import kiro_crew.sel as sel_mod

        def _boom() -> Any:
            raise RuntimeError("SEL is not wired yet")

        monkeypatch.setattr(sel_mod, "sel", _boom)
        monkeypatch.setattr(
            discovery_mod, "plugin_entry_points", lambda: [_FakeEntryPoint("zibble")]
        )
        monkeypatch.setattr(
            "kiro_crew.platform.admission._read_plugin_manifest",
            lambda ep: PluginManifest(name="zibble", publisher="p13n", version="1"),
        )
        policy = AdmissionPolicy(mode=MODE_OPEN, banned=["zibble"])
        with pytest.raises(PluginAdmissionError, match="rejected by admission policy"):
            discover_companion_context("zibble", _NO_CFG, policy=policy)


class TestCompositionRootFailures:
    def test_a_composition_error_passes_through_unwrapped(
        self, monkeypatch: pytest.MonkeyPatch, open_policy: AdmissionPolicy
    ) -> None:
        sentinel = PlatformCompositionError("companion said no")

        def _build(_cfg: Any) -> Any:
            raise sentinel

        monkeypatch.setattr(
            discovery_mod,
            "plugin_entry_points",
            lambda: [_FakeEntryPoint(loaded=_build)],
        )
        with pytest.raises(PlatformCompositionError) as excinfo:
            discover_companion_context("zibble", _NO_CFG, policy=open_policy)
        assert excinfo.value is sentinel

    def test_any_other_exception_is_normalized_to_a_composition_error(
        self, monkeypatch: pytest.MonkeyPatch, open_policy: AdmissionPolicy
    ) -> None:
        """A plain ValueError would otherwise reach ``cli.main``, which only
        re-raises PlatformCompositionError — silently downgrading the edition."""

        def _build(_cfg: Any) -> Any:
            raise ValueError("bad wiring")

        monkeypatch.setattr(
            discovery_mod,
            "plugin_entry_points",
            lambda: [_FakeEntryPoint(loaded=_build)],
        )
        with pytest.raises(PlatformCompositionError) as excinfo:
            discover_companion_context("zibble", _NO_CFG, policy=open_policy)
        assert "failed to compose a context" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, ValueError)

    def test_a_none_context_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, open_policy: AdmissionPolicy
    ) -> None:
        monkeypatch.setattr(
            discovery_mod,
            "plugin_entry_points",
            lambda: [_FakeEntryPoint(loaded=lambda _cfg: None)],
        )
        with pytest.raises(PlatformCompositionError, match="returned no PlatformContext"):
            discover_companion_context("zibble", _NO_CFG, policy=open_policy)
