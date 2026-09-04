"""Tests for the ACP harness registry: bundled descriptors, aliases, availability.

The registry is what every selection surface reads, so the pins here are mostly
about what must NOT happen: a capability nobody granted, an executable probed at
boot, an operator descriptor that names Python code, or a harness definition an
agent could author.
"""

from __future__ import annotations

import inspect
import json
import os
import stat

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kiro_crew.acp import harness_registry
from kiro_crew.acp.harness_descriptor import (
    ADAPTER_CLAUDE,
    ADAPTER_KAS,
    ADAPTER_KIRO,
    ADAPTERS,
    CAPABILITY_NAMES,
    MCP_DELIVERY_FILE_FED,
    MCP_DELIVERY_WIRE_FED,
    CapabilitySet,
    validate_descriptor,
)
from kiro_crew.acp.harness_registry import (
    BUNDLED_DESCRIPTORS,
    DEFAULT_HARNESS,
    HARNESS_CLAUDE,
    HARNESS_CODEX,
    HARNESS_KAS,
    HARNESS_KIRO,
    OPERATOR_HARNESSES_LEAF,
    HarnessRegistry,
    HarnessUnavailable,
    UnknownHarness,
    registry,
    unserviceable_reason,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
    selectable_backends,
)

# The legacy ``agent.acp_backend`` value each bundled harness corresponds to.
# Codex has no legacy identifier in ``acp/types.py`` (upstream adds one), so it is
# absent here rather than mapped to something it is not.
_LEGACY_BACKEND = {
    HARNESS_KIRO: ACP_BACKEND_KIRO,
    HARNESS_KAS: ACP_BACKEND_KAS,
    HARNESS_CLAUDE: ACP_BACKEND_CLAUDE,
}

# Each behavior capability paired with the legacy ``acp_backend`` membership it
# shipped with. The ``ACP_BACKENDS_*`` views these were transcribed from are
# retired (wave-2 T5), so the membership is written out as LITERALS here — which
# is the whole point of a transcription pin: a table derived from the descriptors
# it checks pins nothing. These are the exact sets that shipped before the rekey
# (kiro full; KAS steer+runtime+identity; claude none of the five).
_SHIPPED_MEMBERS = {
    "session_sharing": frozenset({ACP_BACKEND_KIRO}),
    "steer": frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS}),
    "internal_sandbox": frozenset({ACP_BACKEND_KIRO}),
    "acp_runtime_pool": frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS}),
    "kiro_identity_store": frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS}),
}


def _write_config(payload: dict) -> None:
    """Write ``config.json`` under this test's pinned data home."""
    from kiro_crew.config.loader import config_dir, config_path

    config_dir().mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(payload), encoding="utf-8")


def _write_harnesses(payload: object) -> None:
    """Write ``harnesses.json`` under this test's pinned data home.

    The operator-descriptor file the registry reads — a dedicated leaf, not a
    config key, so the write-protection fence can cover it without gating
    config reads (see the module docstring of ``harness_registry``).
    """
    from kiro_crew.config.loader import config_dir

    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / OPERATOR_HARNESSES_LEAF).write_text(json.dumps(payload), encoding="utf-8")


def _fresh_registry(harnesses: dict | None = None, default_harness: str = "") -> HarnessRegistry:
    """A registry reading a ``harnesses.json`` that declares *harnesses*."""
    if harnesses is not None:
        _write_harnesses(harnesses)
    agent: dict = {}
    if default_harness:
        agent["default_harness"] = default_harness
    _write_config({"agent": agent})
    return HarnessRegistry()


def _executable(path) -> str:
    """Create a non-empty executable file at *path* and return it."""
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _operator_descriptor(executable: str, **overrides) -> dict:
    payload = {"executable": executable, "argv": ["{executable}", "acp"]}
    payload.update(overrides)
    return payload


# ── Bundled descriptors ──


def test_every_bundled_descriptor_validates():
    for descriptor in BUNDLED_DESCRIPTORS:
        assert validate_descriptor(descriptor) == [], descriptor.id


def test_bundled_ids_are_unique():
    ids = [d.id for d in BUNDLED_DESCRIPTORS]
    assert len(ids) == len(set(ids))


def test_kiro_is_the_default_harness_and_is_listed_first():
    # An operator who configures nothing gets Kiro (harness-parity H1).
    assert DEFAULT_HARNESS == HARNESS_KIRO
    assert BUNDLED_DESCRIPTORS[0].id == HARNESS_KIRO
    assert _fresh_registry().default().id == HARNESS_KIRO


def test_kiro_descriptor_carries_todays_spawn_conventions():
    kiro = registry().get(HARNESS_KIRO)
    # AcpRuntime._resolve_spawn_argv builds [bin, "acp", "--agent", agent] and
    # appends ["--model", model] only when a model is pinned.
    assert kiro.argv == ("{executable}", "acp")
    assert kiro.agent_args == ("--agent", "{agent}")
    assert kiro.model_args == ("--model", "{model}")
    # kiro-cli reads MCP servers from its own agent spec, not from session/new.
    assert kiro.mcp_delivery == MCP_DELIVERY_FILE_FED
    assert kiro.adapter == ADAPTER_KIRO


@pytest.mark.parametrize("harness_id", sorted(_LEGACY_BACKEND))
@pytest.mark.parametrize("capability", sorted(_SHIPPED_MEMBERS))
def test_bundled_capability_matches_its_frozenset(harness_id, capability):
    """A bundled flag says exactly what the legacy membership set said.

    This is the transcription pin: the ``ACP_BACKENDS_*`` frozensets it once read
    are retired (wave-2 T5), so the behaviour shipping before the rekey is written
    out as literals in ``_SHIPPED_MEMBERS``. A descriptor that disagrees with one
    silently grants or withdraws a capability, which is exactly what this catches.
    """
    backend = _LEGACY_BACKEND[harness_id]
    expected = backend in _SHIPPED_MEMBERS[capability]
    assert registry().capabilities(harness_id).has(capability) is expected


def test_kas_declares_no_tool_search_or_effort():
    # Withdrawn deliberately: the old gates were spelled ``is_claude_backend``, so
    # KAS received kiro-cli's cli.json overlay writes as a consequence of a
    # negative test rather than of anything it demonstrated (harness-parity H6).
    # The descriptor states the positive posture, and the consequences are pinned
    # where they take effect (test_acp_tool_search.py's KAS-skips cases).
    caps = registry().capabilities(HARNESS_KAS)
    assert caps.has("mcp_tool_search") is False
    assert caps.has("reasoning_effort") is False
    assert registry().get(HARNESS_KAS).adapter == ADAPTER_KAS


def test_kiro_declares_tool_search_and_effort():
    caps = registry().capabilities(HARNESS_KIRO)
    assert caps.has("mcp_tool_search") is True
    assert caps.has("reasoning_effort") is True


def test_codex_is_data_only_with_every_capability_off():
    codex = registry().get(HARNESS_CODEX)
    assert codex.adapter == ""
    assert codex.mcp_delivery == MCP_DELIVERY_WIRE_FED
    assert codex.capabilities == CapabilitySet()


def test_codex_descriptor_launches_the_acp_adapter_not_the_cli():
    """Review finding (2026-09-02): ``codex acp`` is NOT an ACP server.

    The ``codex`` CLI reads ``acp`` as a prompt and never answers initialize, so
    a descriptor spawning it would hang every selected session at handshake. The
    real server is the ``codex-acp`` npm adapter — the same binary the dormant
    seam's own spawn path resolves (``_resolve_codex_acp_bin``: "The 'codex' CLI
    alone does not serve ACP") — spawned bare and driven over the pipe. Model
    selection travels over ``session/set_config_option`` (codex is in the
    config-option channel), so there are no model_args either.
    """
    codex = registry().get(HARNESS_CODEX)
    assert codex.executable == "codex-acp"
    assert codex.argv == ("{executable}",)
    assert codex.model_args == ()


def test_claude_is_registered_and_serviceable_and_the_unserviceable_barrier_still_works(
    monkeypatch,
):
    """Claude is a registered, serviceable public backend (#7301), and the
    build-refusal barrier still works for a row that IS declared unserviceable.

    #7301 made Claude Code selectable in the public build, so the old
    ``_UNSERVICEABLE`` refusal posture is gone: claude is registered (a persisted
    ``acp_backend: "claude"`` is recognized, not silently swapped) and its
    availability is now driven by executable resolution — the claude-agent-acp
    adapter binary being present or absent — not by a build-level refusal.

    The regression barrier the old test protected is the ``_UNSERVICEABLE``
    mechanism itself, not claude's membership in it. So this asserts BOTH: claude
    now serves when its binary resolves, AND a synthetic row injected into
    ``_UNSERVICEABLE`` still refuses end to end (availability False with the
    reason, ``require_available`` raises, the listing shows it False with the
    reason). An edition whose build genuinely cannot serve a bundled harness is
    what would populate that map, and this proves the seam it flows through is
    intact.
    """
    reg = registry()
    claude = reg.get(HARNESS_CLAUDE)
    assert claude.adapter == ADAPTER_CLAUDE
    # Serviceable now: no build-level refusal. Availability is whatever the
    # adapter's executable resolution reports on this machine — never a hardcoded
    # "not serviceable" refusal.
    available, reason = reg.availability(HARNESS_CLAUDE)
    if not available:
        assert "not serviceable" not in reason
    assert unserviceable_reason(HARNESS_CLAUDE) == ""
    # The barrier still bites for a row that IS declared unserviceable: inject a
    # synthetic bundled-style row and confirm it refuses through every seam.
    monkeypatch.setitem(
        harness_registry._UNSERVICEABLE, HARNESS_CLAUDE, "build cannot serve claude"
    )
    available, reason = reg.availability(HARNESS_CLAUDE)
    assert available is False
    assert reason == "build cannot serve claude"
    with pytest.raises(HarnessUnavailable) as excinfo:
        reg.require_available(HARNESS_CLAUDE)
    assert excinfo.value.harness_id == HARNESS_CLAUDE
    assert excinfo.value.reason == "build cannot serve claude"
    # Visible in the listing with its reason (R6.2), never silently dropped.
    row = next(r for r in reg.list() if r.id == HARNESS_CLAUDE)
    assert (row.available, row.reason) == (False, "build cannot serve claude")


def test_only_bundled_descriptors_may_name_an_adapter():
    named = {d.adapter for d in BUNDLED_DESCRIPTORS if d.adapter}
    assert named <= ADAPTERS


def test_the_unselectable_set_is_transcribed_from_the_legacy_vocabulary():
    """What cannot be selected is exactly what ``selectable_backends()`` excludes.

    KNOWN minus selectable is the set of backends the code understands but cannot
    serve a session with; the registry keeps them registered and refuses
    selection, so the posture survives the migration instead of a harness
    quietly becoming selectable.
    """
    unselectable_backends = ACP_BACKENDS_KNOWN - selectable_backends()
    expected = {
        harness_id
        for harness_id, backend in _LEGACY_BACKEND.items()
        if backend in unselectable_backends
    }
    assert set(harness_registry._UNSERVICEABLE) == expected


# ── Legacy alias resolution (Property 4) ──


def test_the_empty_backend_resolves_to_kiro():
    # ACP_BACKEND_KIRO is spelled "", which is unusable as an id — the alias is
    # what keeps an existing config working unchanged (R1.6).
    assert registry().resolve_alias(ACP_BACKEND_KIRO) == HARNESS_KIRO


def test_kas_and_codex_resolve_to_their_own_descriptors():
    assert registry().resolve_alias(ACP_BACKEND_KAS) == HARNESS_KAS
    assert registry().resolve_alias(HARNESS_CODEX) == HARNESS_CODEX


def test_a_serviceable_backend_resolves_to_its_own_harness(caplog):
    # #7301: claude is serviceable now, so its legacy backend resolves to the
    # claude harness rather than degrading to the default. No "cannot serve"
    # warning is emitted for it.
    with caplog.at_level("WARNING"):
        assert registry().resolve_alias(ACP_BACKEND_CLAUDE) == HARNESS_CLAUDE
    assert not any("cannot serve a session" in r.getMessage() for r in caplog.records)


def test_a_known_but_unserviceable_backend_degrades_to_the_default(monkeypatch, caplog):
    # The degrade-on-unserviceable mechanism is unchanged; only claude left the
    # map. Inject a synthetic unserviceable row for a real backend id and confirm
    # resolve_alias consults _UNSERVICEABLE and degrades to the default with the
    # warning — the barrier the old claude row exercised, proven on a live row.
    monkeypatch.setitem(
        harness_registry._UNSERVICEABLE, ACP_BACKEND_KAS, "build cannot serve this backend"
    )
    with caplog.at_level("WARNING"):
        assert registry().resolve_alias(ACP_BACKEND_KAS) == DEFAULT_HARNESS
    assert any("cannot serve a session" in r.getMessage() for r in caplog.records)


def test_an_unserviceable_backend_degrades_even_when_it_is_also_an_alias(monkeypatch, caplog):
    """Unserviceable is consulted before the alias table, and the order matters.

    Nothing structurally keeps the two mappings disjoint: an alias row is what a
    harness gains when it acquires a legacy ``acp_backend`` identifier, and its
    unserviceable row lives elsewhere in the module. With the alias consulted
    first, an id in both resolves to a harness that cannot serve a session —
    which is the one outcome the unserviceable row exists to prevent.

    Claude is serviceable now (#7301) so it can no longer stand in for the
    unserviceable side; a synthetic row on a real backend id that is ALSO wired
    into ``_ALIASES`` proves the same ordering invariant on a live id.
    """
    monkeypatch.setitem(harness_registry._UNSERVICEABLE, ACP_BACKEND_KAS, "build cannot serve this")
    monkeypatch.setitem(harness_registry._ALIASES, ACP_BACKEND_KAS, HARNESS_KAS)
    with caplog.at_level("WARNING"):
        assert registry().resolve_alias(ACP_BACKEND_KAS) == DEFAULT_HARNESS
    assert any("cannot serve a session" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("garbage", ["nope", "KIRO", "kiro-cli", "  ", None, 7, [], {"a": 1}, True])
def test_an_unresolvable_backend_degrades_without_raising(garbage):
    assert registry().resolve_alias(garbage) == DEFAULT_HARNESS


_ALIAS_VALUES = st.one_of(
    st.sampled_from(
        sorted(ACP_BACKENDS_KNOWN | selectable_backends() | {HARNESS_CODEX, HARNESS_KIRO})
    ),
    st.text(max_size=12),
    st.none(),
    st.integers(),
    st.booleans(),
    st.lists(st.text(max_size=3), max_size=2),
)


@given(_ALIAS_VALUES)
def test_alias_resolution_is_total_and_lands_on_a_bundled_descriptor(value):
    """Property 4: resolution never errors and never leaves the bundled set."""
    reg = registry()
    resolved = reg.resolve_alias(value)
    descriptor = reg.get(resolved)  # raises UnknownHarness if unregistered
    assert descriptor.id == resolved
    assert descriptor.id in {d.id for d in BUNDLED_DESCRIPTORS}


@given(_ALIAS_VALUES)
def test_alias_resolution_matches_the_normalizers_degrade_posture(value):
    """Property 4: anything the normalizer degrades also degrades here.

    ``codex`` is the one deliberate divergence and is excluded: the normalizer
    does not know it yet (it is absent from ``ACP_BACKENDS_KNOWN``) while R1.6
    requires it to alias its bundled descriptor. Every other value the normalizer
    sends to Kiro must land on the default harness here too.
    """
    from kiro_crew.config.loader import _normalize_acp_backend

    if value == HARNESS_CODEX:
        return
    normalized = _normalize_acp_backend(value)
    resolved = registry().resolve_alias(value)
    if normalized == ACP_BACKEND_KIRO:
        assert resolved == DEFAULT_HARNESS
    else:
        assert resolved == registry().resolve_alias(normalized)


# ── Operator descriptors ──


def test_a_valid_operator_descriptor_is_registered_and_listed(tmp_path):
    exe = _executable(tmp_path / "my-acp-tool")
    reg = _fresh_registry({"my-tool": _operator_descriptor(exe, display_name="My ACP Tool")})
    row = next(r for r in reg.list() if r.id == "my-tool")
    assert (row.display_name, row.available, row.reason, row.bundled) == (
        "My ACP Tool",
        True,
        "",
        False,
    )
    # Every capability off unless the descriptor enabled it (R2.5).
    assert reg.capabilities("my-tool") == CapabilitySet()


def test_operator_harnesses_are_listed_after_the_bundled_ones(tmp_path):
    exe = _executable(tmp_path / "tool")
    reg = _fresh_registry(
        {"zz-tool": _operator_descriptor(exe), "aa-tool": _operator_descriptor(exe)}
    )
    ids = [r.id for r in reg.list()]
    bundled = [d.id for d in BUNDLED_DESCRIPTORS]
    assert ids[: len(bundled)] == bundled
    assert ids[len(bundled) :] == ["aa-tool", "zz-tool"]


def test_an_invalid_operator_descriptor_is_excluded_and_reported(tmp_path):
    exe = _executable(tmp_path / "tool")
    reg = _fresh_registry(
        {
            "broken": {"executable": exe, "argv": ["{executable}", "--home={home}"]},
            "good": _operator_descriptor(exe),
        }
    )
    # Excluded from every selection surface (R2.3): not in the listing…
    assert "broken" not in {r.id for r in reg.list()}
    # …and not gettable either, so nothing can hand it to a session.
    with pytest.raises(UnknownHarness):
        reg.get("broken")
    # The reason is retained so Settings can show what is wrong (R6.2).
    invalid = {r.id: r for r in reg.invalid()}
    assert "{home}" in invalid["broken"].reason
    assert invalid["broken"].valid is False
    # Every other harness is unaffected.
    assert "good" in {r.id for r in reg.list()}
    assert reg.get(DEFAULT_HARNESS).id == DEFAULT_HARNESS


def test_an_operator_descriptor_cannot_shadow_a_bundled_harness(tmp_path):
    exe = _executable(tmp_path / "tool")
    reg = _fresh_registry({HARNESS_KIRO: _operator_descriptor(exe)})
    assert reg.get(HARNESS_KIRO).adapter == ADAPTER_KIRO
    assert "already registered" in {r.id: r.reason for r in reg.invalid()}[HARNESS_KIRO]


def test_the_literal_adapter_key_is_refused_on_the_operator_path(tmp_path):
    """Configuration must never be able to select a Python entry point.

    The parse path rejects the LITERAL key, not merely a typo of it, so extending
    ``DESCRIPTOR_KEYS`` for a bundled-only field cannot quietly open this door.
    """
    from kiro_crew.acp.harness_descriptor import DESCRIPTOR_KEYS, descriptor_from_mapping

    assert "adapter" not in DESCRIPTOR_KEYS
    descriptor, reasons = descriptor_from_mapping(
        {"executable": "x", "argv": ["{executable}"], "adapter": ADAPTER_KIRO},
        harness_id="x",
    )
    assert descriptor is None
    assert any("'adapter'" in r for r in reasons)

    exe = _executable(tmp_path / "tool")
    reg = _fresh_registry({"sneaky": _operator_descriptor(exe, adapter=ADAPTER_KAS)})
    assert "sneaky" not in {r.id for r in reg.list()}
    assert "'adapter'" in {r.id: r.reason for r in reg.invalid()}["sneaky"]


def test_a_new_operator_harness_appears_after_the_config_changes(tmp_path):
    exe = _executable(tmp_path / "tool")
    reg = _fresh_registry({})
    assert "later" not in {r.id for r in reg.list()}
    _write_harnesses({"later": _operator_descriptor(exe)})
    reg.reload()
    assert "later" in {r.id for r in reg.list()}


def test_a_config_load_failure_degrades_to_bundled_only(monkeypatch, caplog):
    import kiro_crew.config.loader as loader

    def _explode(*_a, **_kw):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(_explode))
    with caplog.at_level("WARNING"):
        ids = {r.id for r in HarnessRegistry().list()}
    assert ids == {d.id for d in BUNDLED_DESCRIPTORS}
    assert any("config unreadable" in r.getMessage() for r in caplog.records)


# ── Default harness ──


def test_the_configured_default_harness_is_honoured(tmp_path):
    exe = _executable(tmp_path / "tool")
    reg = _fresh_registry({"mine": _operator_descriptor(exe)}, default_harness="mine")
    assert reg.default().id == "mine"


def test_an_unknown_default_harness_degrades_to_kiro(caplog):
    reg = _fresh_registry({}, default_harness="ghost")
    with caplog.at_level("WARNING"):
        assert reg.default().id == HARNESS_KIRO
    assert any("default_harness" in r.getMessage() for r in caplog.records)


def test_an_unavailable_default_harness_degrades_to_kiro(tmp_path, caplog):
    missing = str(tmp_path / "never-installed")
    reg = _fresh_registry({"mine": _operator_descriptor(missing)}, default_harness="mine")
    with caplog.at_level("WARNING"):
        assert reg.default().id == HARNESS_KIRO
    assert any(missing in r.getMessage() for r in caplog.records)


# ── Availability ──


def test_nothing_is_probed_until_a_caller_asks(monkeypatch):
    """R6.1: registration is free; the executables are not touched at boot."""
    calls: list[str] = []

    def _counting(descriptor):
        calls.append(descriptor.id)
        return descriptor.executable, ""

    monkeypatch.setattr(harness_registry, "resolve_executable", _counting)
    reg = HarnessRegistry()
    assert calls == []
    # Even loading the descriptors probes nothing — only availability does.
    reg.get(HARNESS_KIRO)
    assert calls == []
    reg.list()
    assert calls


def test_availability_never_executes_a_candidate():
    # A listing must not become N subprocess spawns, and an unauthenticated
    # harness must not be reported as a missing binary. Pinned structurally: this
    # module has no way to run anything.
    source = inspect.getsource(harness_registry)
    for forbidden in ("subprocess", "os.system", "asyncio.create_subprocess"):
        assert forbidden not in source


def test_a_missing_executable_is_unavailable_with_a_reason_naming_it(tmp_path):
    missing = str(tmp_path / "not-installed")
    reg = _fresh_registry({"mine": _operator_descriptor(missing)})
    available, reason = reg.availability("mine")
    assert available is False
    assert missing in reason


def test_a_bare_name_off_path_is_unavailable(tmp_path):
    reg = _fresh_registry({"mine": _operator_descriptor("definitely-not-on-path-xyz")})
    available, reason = reg.availability("mine")
    assert available is False
    assert "not found on PATH" in reason


def test_a_zero_byte_executable_is_refused(tmp_path):
    path = tmp_path / "truncated"
    path.write_text("", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    reg = _fresh_registry({"mine": _operator_descriptor(str(path))})
    available, reason = reg.availability("mine")
    assert available is False
    assert "zero-byte" in reason


@pytest.mark.skipif(os.name == "nt", reason="Windows has no execute bit to clear")
def test_a_non_executable_file_is_refused(tmp_path):
    path = tmp_path / "not-executable"
    path.write_text("data\n", encoding="utf-8")
    path.chmod(0o644)
    reg = _fresh_registry({"mine": _operator_descriptor(str(path))})
    available, reason = reg.availability("mine")
    assert available is False
    assert "not an executable file" in reason


def test_a_path_that_does_not_exist_says_so_rather_than_blaming_permissions(tmp_path):
    """The reason has to name the actual defect to be actionable.

    An absent path and a present-but-unexecutable one need different fixes
    ("install it" vs "chmod it"), and the executable-file probe answers False for
    both — so absence is checked first.
    """
    missing = str(tmp_path / "never-installed")
    reg = _fresh_registry({"mine": _operator_descriptor(missing)})
    available, reason = reg.availability("mine")
    assert available is False
    assert reason == f"{missing} does not exist"


def test_a_zero_byte_kiro_cli_is_refused_on_every_platform(tmp_path, monkeypatch):
    """kiro-cli's own candidate filter checks emptiness only on Windows.

    A truncated ``kiro-cli`` (an interrupted install) keeps its execute bit on
    POSIX, so without the registry's own check it would list available and then
    exec into a process that exits with no ACP frame.
    """
    truncated = tmp_path / "kiro-cli"
    truncated.write_text("", encoding="utf-8")
    truncated.chmod(truncated.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.resolve_kiro_cli", lambda *a, **k: str(truncated), raising=True
    )
    available, reason = _fresh_registry({}).availability(HARNESS_KIRO)
    assert available is False
    assert reason == f"{truncated} is a zero-byte file"


def test_kas_availability_follows_kiro_cli_because_the_relay_is_kiro_cli(tmp_path, monkeypatch):
    """KAS is reached as ``kiro-cli acp --agent-engine v3``, so it resolves that binary.

    Not an incidental sharing: an operator who overrode the kiro-cli path did so
    for every harness the CLI serves, and a second resolution chain here is how
    KAS would report available while the binary it execs is a different file.
    """
    resolved = _executable(tmp_path / "kiro-cli")
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.resolve_kiro_cli", lambda *a, **k: resolved, raising=True
    )
    assert _fresh_registry({}).availability(HARNESS_KAS) == (True, "")


def test_kas_is_unavailable_when_kiro_cli_cannot_be_found(monkeypatch):
    """The relay has nothing to relay THROUGH, and the reason says which binary."""
    monkeypatch.setattr("kiro_crew.kiro_cli.resolve_kiro_cli", lambda *a, **k: "", raising=True)
    available, reason = _fresh_registry({}).availability(HARNESS_KAS)
    assert available is False
    assert "kiro-cli" in reason


def test_a_zero_byte_kiro_cli_is_refused_for_kas_too(tmp_path, monkeypatch):
    """The shared candidate check covers both harnesses, not just the kiro one.

    Same defect as ``test_a_zero_byte_kiro_cli_is_refused_on_every_platform``,
    asserted through KAS: a truncated binary keeps its execute bit on POSIX, and
    the two harnesses must agree about it or the same file is spawnable under one
    name and not the other.
    """
    truncated = tmp_path / "kiro-cli"
    truncated.write_text("", encoding="utf-8")
    truncated.chmod(truncated.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.resolve_kiro_cli", lambda *a, **k: str(truncated), raising=True
    )
    available, reason = _fresh_registry({}).availability(HARNESS_KAS)
    assert available is False
    assert reason == f"{truncated} is a zero-byte file"


def test_installing_the_binary_heals_the_listing_without_a_restart(tmp_path):
    """R6.5: recovery is reflected on the next listing, no gateway restart."""
    path = tmp_path / "later-installed"
    reg = _fresh_registry({"mine": _operator_descriptor(str(path))})
    assert next(r for r in reg.list() if r.id == "mine").available is False
    _executable(path)
    row = next(r for r in reg.list() if r.id == "mine")
    assert (row.available, row.reason) == (True, "")


def test_a_recorded_spawn_failure_marks_only_that_harness(tmp_path):
    exe = _executable(tmp_path / "tool")
    reg = _fresh_registry({"mine": _operator_descriptor(exe), "other": _operator_descriptor(exe)})
    reg.note_probe_failure("mine", "exited during ACP initialize (rc=1)")
    available, reason = reg.availability("mine")
    assert available is False
    assert "ACP initialize" in reason
    # R6.4: another harness's availability is untouched.
    assert reg.availability("other") == (True, "")
    reg.clear_probe_failure("mine")
    assert reg.availability("mine") == (True, "")


def test_a_recorded_failure_expires_so_a_repaired_harness_heals(tmp_path, monkeypatch):
    """A failed attempt is a snapshot, not a verdict.

    Nothing would clear a permanent one short of a gateway restart, which R6.5
    exists to avoid — so the record ages out and the next listing re-asks.
    """
    exe = _executable(tmp_path / "tool")
    reg = _fresh_registry({"mine": _operator_descriptor(exe)})
    reg.note_probe_failure("mine", "not signed in")
    assert reg.availability("mine")[0] is False
    monkeypatch.setattr(harness_registry, "_PROBE_FAILURE_TTL_SECS", 0.0)
    assert reg.availability("mine") == (True, "")


def test_require_available_refuses_rather_than_substituting(tmp_path):
    missing = str(tmp_path / "gone")
    reg = _fresh_registry({"mine": _operator_descriptor(missing)})
    with pytest.raises(HarnessUnavailable) as excinfo:
        reg.require_available("mine")
    assert excinfo.value.harness_id == "mine"
    assert missing in str(excinfo.value)
    with pytest.raises(UnknownHarness):
        reg.require_available("no-such-harness")


def test_unknown_harness_error_names_what_is_registered():
    with pytest.raises(UnknownHarness) as excinfo:
        _fresh_registry({}).get("ghost")
    assert HARNESS_KIRO in str(excinfo.value)


# ── Write guard: harness definitions are operator-only ──


def test_the_file_holding_harness_definitions_is_write_protected_from_agent_tools():
    """R2.4 on the tool path.

    Operator descriptors live in ``harnesses.json``, which is on the write-only
    protected tier — the agent's file-edit tool is refused there while reads stay
    allowed. A descriptor names a binary Kiro Crew spawns, so an agent that could
    author one would have arbitrary code execution in the gateway's identity.
    """
    from kiro_crew import security
    from kiro_crew.config.loader import config_dir

    assert security.is_sensitive_write_path(str(config_dir() / OPERATOR_HARNESSES_LEAF))
    # Pinned against the LIST, not just the predicate: removing harnesses.json
    # from the write-protected tier must fail here rather than silently open the
    # file, and the leaf spelling must match the one the registry reads.
    tails = {entry.rsplit("/", 1)[-1] for entry in security.write_protected_home_paths()}
    assert OPERATOR_HARNESSES_LEAF in tails


def test_harness_definitions_are_not_on_the_settings_patch_allowlist():
    """R2.4 on the API path.

    The generic config PATCH surface is an allowlist; a harness definition is not
    a scalar preference and must not be writable through it.
    """
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    assert "agent.harnesses" not in _EDITABLE_CONFIG
    assert not any(key.startswith("agent.harnesses.") for key in _EDITABLE_CONFIG)


# ── Write guard: the shell path ──


def test_harnesses_json_shell_writes_are_denied_and_reads_stay_allowed(tmp_path, monkeypatch):
    """R2.4 on the bash path.

    A shell redirect is the write path no file-edit gate sees, so the leaf is
    fenced in ``_WRITE_PROTECTED_BASH_LEAVES`` — protected on one path only is
    not protected. The bash matcher is verb-INDEPENDENT (naming the leaf at all
    is refused, so no novel write verb slips through), which takes bash ``cat``
    with it — the same trade the playwright-cli-config leaf already made.
    Reads survive on the paths that matter: the registry reads the file
    in-process, and the agent's file-READ tool is ungated (only the file-EDIT
    tier lists the leaf).
    """
    from pathlib import Path

    from kiro_crew import security

    assert OPERATOR_HARNESSES_LEAF in security._WRITE_PROTECTED_BASH_LEAVES
    # Also on the bare-token tier: every anchored pattern above is defeated by
    # one ``cd`` (``cd ~/.kiro/crew; printf … > harnesses.json`` names no home
    # and no prefix), and for a file that IS the execution grant that gap is
    # not a residual to accept. Membership here routes the leaf through the
    # anchor-independent matcher and its shared test grid.
    assert OPERATOR_HARNESSES_LEAF in security._BARE_TOKEN_PROTECTED_LEAVES
    assert (
        security.is_sensitive_bash_command(
            f"cd ~/.kiro/crew && printf '{{}}' > {OPERATOR_HARNESSES_LEAF}"
        )
        is not None
    )

    for prefix in security.crew_home_prefixes():
        for record in (
            f"~/{prefix}/{OPERATOR_HARNESSES_LEAF}",
            f"$HOME/{prefix}/{OPERATOR_HARNESSES_LEAF}",
            str(Path.home() / prefix / OPERATOR_HARNESSES_LEAF),
        ):
            for cmd in (
                f"echo '{{}}' > {record}",
                f"echo '{{}}' >> {record}",
                f"tee {record}",
                f"touch {record}",
                f"rm {record}",
                f"mv /tmp/forged.json {record}",
                f"cp /tmp/forged.json {record}",
                # The verb-independent matcher catches reads too; the sanctioned
                # read paths are the file-READ tool and the registry itself.
                f"cat {record}",
            ):
                assert security.is_sensitive_bash_command(cmd) is not None, cmd
        # Unrelated writes under the crew home stay allowed.
        assert security.is_sensitive_bash_command(f"touch ~/{prefix}/sessions.db") is None
    # A DIFFERENT file whose name merely ends with the leaf's is not fenced.
    assert security.is_sensitive_bash_command(f"touch my-{OPERATOR_HARNESSES_LEAF}") is None
    # The file-READ tool path stays open: the leaf is write-protected, not
    # read+write sensitive (it holds no secret).
    from kiro_crew.config.loader import config_dir

    assert not security.is_sensitive_path(str(config_dir() / OPERATOR_HARNESSES_LEAF))


# ── Config keys ──


def test_the_registry_reads_operator_descriptors_from_harnesses_json(tmp_path):
    """The store is the dedicated file, named by its pinned leaf constant."""
    from kiro_crew.config.loader import config_dir

    exe = _executable(tmp_path / "tool")
    _write_config({"agent": {"default_harness": "mine"}})
    _write_harnesses({"mine": _operator_descriptor(exe)})
    assert (config_dir() / OPERATOR_HARNESSES_LEAF).is_file()

    reg = HarnessRegistry()
    assert reg.get("mine").executable == exe
    assert reg.default().id == "mine"


@pytest.mark.parametrize("garbage", [7, None, [], {"a": 1}, True])
def test_a_malformed_default_harness_reads_as_absent(garbage):
    from kiro_crew.config.loader import KiroCrewConfig

    _write_config({"agent": {"default_harness": garbage}})
    assert KiroCrewConfig.load().agent.default_harness == ""


def test_a_non_object_harness_entry_is_served_as_an_invalid_row(caplog):
    """A non-object entry reaches the registry instead of vanishing at load.

    The file is read un-coerced so the registry's reason channel —
    the only surface an operator sees — can say ``descriptor must be an object``
    under the entry's own id. Dropping it at read would leave Settings with no
    row and no reason for a harness the operator plainly configured.
    """
    _write_harnesses({"mine": "kiro-cli acp"})

    with caplog.at_level("WARNING"):
        reg = HarnessRegistry()
        invalid = {r.id: r for r in reg.invalid()}
    assert "must be an object" in invalid["mine"].reason
    assert invalid["mine"].valid is False
    # Excluded from every selection surface, like any other invalid descriptor.
    assert "mine" not in {r.id for r in reg.list()}
    with pytest.raises(UnknownHarness):
        reg.get("mine")
    assert any("ignoring operator harness" in r.getMessage() for r in caplog.records)


def test_a_non_object_harnesses_file_reads_as_empty():
    _write_harnesses(["mine"])
    reg = HarnessRegistry()
    assert {r.id for r in reg.list()} == {d.id for d in BUNDLED_DESCRIPTORS}


def test_an_unparseable_harnesses_file_costs_only_the_operator_rows(caplog):
    """A corrupt file must not take the bundled harnesses down with it."""
    from kiro_crew.config.loader import config_dir

    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / OPERATOR_HARNESSES_LEAF).write_text("{not json", encoding="utf-8")

    with caplog.at_level("WARNING"):
        reg = HarnessRegistry()
    assert {r.id for r in reg.list()} == {d.id for d in BUNDLED_DESCRIPTORS}
    assert any(OPERATOR_HARNESSES_LEAF in r.getMessage() for r in caplog.records)


def test_capability_vocabulary_covers_every_bundled_flag():
    for descriptor in BUNDLED_DESCRIPTORS:
        assert set(descriptor.capabilities.as_dict()) == set(CAPABILITY_NAMES)
