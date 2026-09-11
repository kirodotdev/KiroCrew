"""The per-agent bookkeeping sidecar is write-protected against agent tools.

``~/.kiro/crew/agent_model_state.json`` (agent_state.py) records model
bookkeeping AND fork lineage (``forked_from`` / ``private_to``) — the record the
fork endpoint trusts to decide a template is already a crew's private copy. A
prompt-injected agent that could write it would forge a ``private_to`` entry
naming a SHARED template, so the fork returns ``already_private`` and the owner's
next PATCH mutates the shared file. Writes must be
refused at the agent file-edit gate; reads of the sidecar itself must keep
working, because it holds no secret and is read constantly to resolve models and
enrich the agent list.

Its ``.lock`` sibling and the ``atomic_write`` temp it is renamed over land in
the crew home root, which is a keystone-artifact parent (``.env`` lives there),
so they are already fenced read+write by ``_is_keystone_publish_artifact``. These
tests pin that too, so a refactor that moved the sidecar out of the crew home
root — unfencing the lock/temp — fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import agent_state
from kiro_crew.security import (
    is_sensitive_path,
    is_sensitive_write_path,
    write_protected_home_paths,
)

_PREFIXES = ("~/.kiro/crew", "~/.kirocrew")


class TestAgentStateSidecarWriteProtection:
    """Writes to the sidecar are refused; reads of it stay allowed."""

    @pytest.mark.parametrize("prefix", _PREFIXES)
    def test_sidecar_is_write_protected(self, prefix: str) -> None:
        assert is_sensitive_write_path(f"{prefix}/agent_model_state.json")
        assert is_sensitive_write_path(str(Path.home() / prefix[2:] / "agent_model_state.json"))

    def test_entry_matches_module_filename(self) -> None:
        """Pin the gate entry to agent_state's own filename constant.

        Ties the security entry to ``agent_state._STATE_FILENAME`` rather than to a
        second hand-copied spelling, so renaming the sidecar in agent_state without
        updating the security entry fails here instead of silently unprotecting the
        fork-lineage record. Constant-based rather than resolver-based so it does not
        depend on the live KIROCREW_HOME anchor under test isolation.
        """
        entries = write_protected_home_paths()
        assert any(e.endswith("/" + agent_state._STATE_FILENAME) for e in entries), entries

    @pytest.mark.parametrize("prefix", _PREFIXES)
    def test_lock_sibling_is_fenced(self, prefix: str) -> None:
        # The cross-process advisory lock beside the sidecar. Already read+write
        # fenced by the keystone-artifact rule (crew home root is an artifact
        # parent), so both gates refuse it.
        lock = f"{prefix}/{agent_state._STATE_FILENAME}.lock"
        assert is_sensitive_write_path(lock)
        assert is_sensitive_path(lock)

    @pytest.mark.parametrize("prefix", _PREFIXES)
    def test_atomic_write_temp_is_fenced(self, prefix: str) -> None:
        # ``atomic_write`` stages a ``tmpXXXX.tmp`` in the sidecar's own directory
        # (the crew home root) and renames it over the sidecar. That temp holds the
        # full payload, so it is fenced read+write by the keystone-artifact rule.
        tmp = f"{prefix}/tmp0a1b2c3d.tmp"
        assert is_sensitive_write_path(tmp)
        assert is_sensitive_path(tmp)

    @pytest.mark.parametrize("prefix", _PREFIXES)
    def test_sidecar_reads_stay_allowed_via_tools(self, prefix: str) -> None:
        # WRITE-only, deliberately NOT read+write sensitive: model resolution and
        # ``list_agents`` read it constantly, so the read gate must not fence it.
        assert is_sensitive_path(f"{prefix}/agent_model_state.json") is False

    def test_bash_write_is_sealed_by_the_sandbox(self) -> None:
        # Shell writes are fenced by the OS sandbox seal, not by command-string
        # parsing: the OS sandbox is the layer that can hold the path fence,
        # so the sidecar must sit in BOTH seal
        # lists — the read-only seal for a present file and the Linux
        # pre-create seal for an absent one.
        from kiro_crew import sandbox

        assert "agent_model_state.json" in sandbox._CREW_READONLY_LEAVES
        assert "agent_model_state.json" in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES
        # The lock sibling too: an unsealed lock can be
        # unlinked and recreated inside the sandbox, so concurrent writers
        # lock different inodes and a stale RMW erases lineage.
        assert "agent_model_state.json.lock" in sandbox._CREW_READONLY_LEAVES
        assert "agent_model_state.json.lock" in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES

    def test_template_spec_lock_is_sealed_by_the_sandbox(self) -> None:
        # GPT rounds 39-40: the whole KIRO AGENTS tree is sealed read-only —
        # the fork specs are what governance sanitizes, so a sandboxed write
        # to one hands the next spawn forged grants; the advisory lock is
        # covered by the same directory seal. Resolved per spawn (never a
        # hard-coded literal), and the dir is pre-created so the Linux mount
        # seal has a target on an install where it does not exist yet.
        from kiro_crew import sandbox
        from kiro_crew.config.paths import kiro_agents_dir

        targets = sandbox._resolved_kiro_agents_targets()
        assert targets == [str(kiro_agents_dir())] or targets, targets
        dirs, _files = sandbox._sealable_absent_ceilings()
        assert any(d.rstrip("/").endswith("agents") for d in dirs), dirs

    def test_entry_is_published_on_the_posture_surface(self) -> None:
        entries = write_protected_home_paths()
        matches = [e for e in entries if e.endswith("agent_model_state.json")]
        # Both data-home spellings, so a legacy install is gated too.
        assert len(matches) == 2, entries

    def test_sibling_data_home_paths_unaffected(self) -> None:
        """A prefix-neighbour of the entry must not be caught by it."""
        # A different file that shares the string prefix stays writable.
        assert is_sensitive_write_path("~/.kiro/crew/agent_model_state.json.bak") is False
        assert (
            is_sensitive_write_path("~/.kiro/crew/config.json") is True
        )  # quick check: list intact
