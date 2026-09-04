"""Tests for the generic ACP adapter (Wave 2, T2 / R1.2, R2).

The generic adapter is what every adapter-less descriptor resolves to — bundled
Codex and every operator harness — so a new provider's ACP server is config, not
a code PR. These tests pin three things: its argv is pure template rendering with
no hardcoded flags, its executable resolution honours an absolute path and falls
back to the augmented PATH the kiro chain uses, and adapter RESOLUTION sends an
adapter-less descriptor here while leaving kiro/kas/claude on their bespoke
adapters.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew.acp.harness_adapters import (
    ClaudeAdapter,
    GenericAdapter,
    KasAdapter,
    KiroAdapter,
    adapter_for,
)
from kiro_crew.acp.harness_descriptor import (
    ADAPTER_GENERIC,
    HarnessDescriptor,
)
from kiro_crew.acp.harness_registry import (
    HARNESS_CLAUDE,
    HARNESS_CODEX,
    HARNESS_KAS,
    HARNESS_KIRO,
    registry,
)


def _operator_descriptor(**overrides) -> HarnessDescriptor:
    """An adapter-less descriptor shaped like an operator's own ACP server."""
    fields = dict(
        id="agy",
        display_name="AGY ACP",
        executable="agy-acp",
        argv=("{executable}", "acp"),
    )
    fields.update(overrides)
    return HarnessDescriptor(**fields)


# ── render_argv matrix (pure template, no hardcoded flags) ──


def test_render_argv_emits_neither_block_without_agent_or_model():
    descriptor = _operator_descriptor(
        agent_args=("--agent", "{agent}"),
        model_args=("--model", "{model}"),
    )
    argv = GenericAdapter().render_argv(descriptor, executable="/opt/agy-acp")
    assert argv == ["/opt/agy-acp", "acp"]


def test_render_argv_appends_agent_block_only_when_agent_passed():
    descriptor = _operator_descriptor(
        agent_args=("--agent", "{agent}"),
        model_args=("--model", "{model}"),
    )
    argv = GenericAdapter().render_argv(descriptor, executable="/opt/agy-acp", agent="kirocrew")
    assert argv == ["/opt/agy-acp", "acp", "--agent", "kirocrew"]


def test_render_argv_appends_model_block_only_when_model_pinned():
    descriptor = _operator_descriptor(
        agent_args=("--agent", "{agent}"),
        model_args=("--model", "{model}"),
    )
    argv = GenericAdapter().render_argv(descriptor, executable="/opt/agy-acp", model="fast-1")
    assert argv == ["/opt/agy-acp", "acp", "--model", "fast-1"]


def test_render_argv_appends_both_blocks_when_agent_and_model_given():
    descriptor = _operator_descriptor(
        agent_args=("--agent", "{agent}"),
        model_args=("--model", "{model}"),
    )
    argv = GenericAdapter().render_argv(
        descriptor, executable="/opt/agy-acp", agent="kirocrew", model="fast-1"
    )
    assert argv == ["/opt/agy-acp", "acp", "--agent", "kirocrew", "--model", "fast-1"]


def test_render_argv_ignores_agent_when_descriptor_declares_no_agent_args():
    """A descriptor with no ``agent_args`` block gets no agent flag even when an
    agent is passed — the generic path hardcodes nothing."""
    descriptor = _operator_descriptor(model_args=("--model", "{model}"))
    argv = GenericAdapter().render_argv(descriptor, executable="/opt/agy-acp", agent="kirocrew")
    assert argv == ["/opt/agy-acp", "acp"]


# ── resolve_executable ──


def test_absolute_executable_is_honored_when_it_is_a_runnable_file(tmp_path):
    tool = tmp_path / "agy-acp"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    descriptor = _operator_descriptor(executable=str(tool))
    resolved, reason = GenericAdapter().resolve_executable(descriptor)
    assert reason == ""
    assert resolved == str(tool)


def test_absolute_executable_missing_is_refused_with_a_reason(tmp_path):
    missing = tmp_path / "not-here"
    descriptor = _operator_descriptor(executable=str(missing))
    resolved, reason = GenericAdapter().resolve_executable(descriptor)
    assert resolved == ""
    assert str(missing) in reason
    assert "does not exist" in reason


def test_bare_name_resolves_through_augmented_path(tmp_path, monkeypatch):
    """A PATH name resolves via the augmented PATH the kiro chain uses.

    The tool is placed in ``~/.local/bin`` — a directory ``augmented_path``
    prepends but a non-login PATH omits — and the process PATH is emptied, so a
    plain ``shutil.which`` would miss it and only the augmentation finds it.
    """
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    # ``shutil.which`` on Windows only matches a PATHEXT-suffixed name, so the
    # planted tool carries one there; the descriptor still says the bare
    # ``agy-acp`` and which() adds the extension — the same shape a real
    # Windows install of a provider's launcher has.
    tool = local_bin / ("agy-acp.bat" if os.name == "nt" else "agy-acp")
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # expanduser's key on Windows
    monkeypatch.setenv("PATH", "")

    descriptor = _operator_descriptor(executable="agy-acp")
    resolved, reason = GenericAdapter().resolve_executable(descriptor)
    assert reason == ""
    # normcase: Windows which() may report the PATHEXT extension in the
    # case PATHEXT spells it (.BAT), and the filesystem is case-insensitive.
    assert os.path.normcase(resolved) == os.path.normcase(str(tool))


def test_bare_name_not_found_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "")
    descriptor = _operator_descriptor(executable="definitely-not-installed-xyz")
    resolved, reason = GenericAdapter().resolve_executable(descriptor)
    assert resolved == ""
    assert "not found on PATH" in reason


def test_relative_path_executable_is_refused_with_an_honest_reason():
    """A relative path (separator, not absolute) is refused, not silently
    resolved against the gateway cwd — shutil.which would discard the augmented
    PATH for it, so the reason must name the real rule, not a PATH miss."""
    for candidate in ("./bin/agy-acp", "bin/agy-acp"):
        descriptor = _operator_descriptor(executable=candidate)
        resolved, reason = GenericAdapter().resolve_executable(descriptor)
        assert resolved == ""
        assert "relative path" in reason
        assert "not found on PATH" not in reason


def test_empty_executable_is_refused():
    descriptor = _operator_descriptor(executable="")
    resolved, reason = GenericAdapter().resolve_executable(descriptor)
    assert resolved == ""
    assert reason == "no executable is declared"


def test_absolute_non_executable_file_is_refused(tmp_path):
    """The interrupted-install branch: a present but non-executable file.

    The defect class is the POSIX execute BIT missing, so the test probes the
    mechanism instead of guessing the platform: on Windows ``chmod(0o644)`` is
    a no-op and every regular file is spawnable, so the refusal this test pins
    cannot occur there and the test skips itself (conftest's probe-not-guess
    rule for symlinks, applied to the exec bit).
    """
    tool = tmp_path / "agy-acp"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o644)
    if os.access(tool, os.X_OK):
        pytest.skip("this platform has no removable execute bit")
    descriptor = _operator_descriptor(executable=str(tool))
    resolved, reason = GenericAdapter().resolve_executable(descriptor)
    assert resolved == ""
    assert "not an executable file" in reason


def test_absolute_zero_byte_file_is_refused(tmp_path):
    """A truncated install: executable bit set, zero bytes."""
    tool = tmp_path / "agy-acp"
    tool.write_bytes(b"")
    tool.chmod(0o755)
    descriptor = _operator_descriptor(executable=str(tool))
    resolved, reason = GenericAdapter().resolve_executable(descriptor)
    assert resolved == ""
    assert "zero-byte file" in reason


# ── pre_spawn: a generic (genuinely foreign) harness never gets kiro's key ──


def test_generic_pre_spawn_strips_kiro_clis_api_key(tmp_path):
    """The base strip is inherited, and the generic adapter is the genuinely
    foreign harness — an operator ACP server must never receive KIRO_API_KEY."""
    from kiro_crew.config.loader import CRED_KIRO_API_KEY

    descriptor = _operator_descriptor()
    env = {CRED_KIRO_API_KEY: "secret", "PATH": "/usr/bin"}
    GenericAdapter().pre_spawn(descriptor, env=env, workdir=str(tmp_path), agent="kirocrew")
    assert CRED_KIRO_API_KEY not in env
    assert env["PATH"] == "/usr/bin"


# ── adapter resolution ──


def test_adapter_less_descriptor_resolves_to_generic():
    descriptor = _operator_descriptor()
    assert descriptor.adapter == ""
    adapter = adapter_for(descriptor)
    assert isinstance(adapter, GenericAdapter)
    assert adapter.name == ADAPTER_GENERIC


def test_bundled_codex_is_adapter_less_and_resolves_to_generic():
    codex = registry().get(HARNESS_CODEX)
    assert codex.adapter == ""
    assert isinstance(adapter_for(codex), GenericAdapter)


@pytest.mark.parametrize(
    "harness_id,adapter_cls",
    [
        (HARNESS_KIRO, KiroAdapter),
        (HARNESS_KAS, KasAdapter),
        (HARNESS_CLAUDE, ClaudeAdapter),
    ],
)
def test_bespoke_harnesses_keep_their_own_adapter(harness_id, adapter_cls):
    descriptor = registry().get(harness_id)
    assert isinstance(adapter_for(descriptor), adapter_cls)
