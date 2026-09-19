"""The operator-authored harness descriptors are write-protected against agents.

``~/.kiro/crew/harnesses.json`` holds the operator-authored HARNESS DESCRIPTORS
that the backend registry loads: each entry names an ``executable`` and an
``argv`` template the gateway RESOLVES AND SPAWNS to serve a backend. That makes
the file an EXECUTION GRANT rather than an input to a decision about one — a
prompt-injected agent that could write it would plant an attacker-chosen command
and have Kiro Crew's own trusted spawner run it, re-armed on every listing.

The guard is three-way and mirrors the ``agent_model_state.json`` sidecar:

1. The agent file-edit tool is refused (``security._WRITE_PROTECTED_HOME_PATHS``
   via :func:`is_sensitive_write_path`), while READS of the file itself stay
   allowed — the registry enumerates backends from it on every listing and
   Settings reads it to render the inventory, and it holds no secret.
2. The SHELL side is held by the OS sandbox, not by command-text parsing (the
   keystone bash gate matches no paths at all — see ``is_sensitive_bash_command``).
   The leaf is sealed READ-ONLY there (``sandbox._CREW_READONLY_LEAVES``) and, so
   an ABSENT descriptor file on a fresh install is still sealed, is materialised
   empty by the Linux launcher (``sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES``).
3. It is NEVER settable from Settings: the PATCH allowlist
   (``dashboard.handlers.core._EDITABLE_CONFIG``) names no harness-definitions key
   and the invariant pin below asserts it never gains one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.security import (
    is_sensitive_path,
    is_sensitive_write_path,
    write_protected_home_paths,
)

_PREFIXES = ("~/.kiro/crew", "~/.kirocrew")
_LEAF = "harnesses.json"


class TestHarnessDescriptorsWriteProtection:
    """Writes to the descriptor file are refused; reads of it stay allowed."""

    @pytest.mark.parametrize("prefix", _PREFIXES)
    def test_descriptors_file_is_write_protected(self, prefix: str) -> None:
        assert is_sensitive_write_path(f"{prefix}/{_LEAF}")
        assert is_sensitive_write_path(str(Path.home() / prefix[2:] / _LEAF))

    @pytest.mark.parametrize("prefix", _PREFIXES)
    def test_descriptor_reads_stay_allowed_via_tools(self, prefix: str) -> None:
        # WRITE-only, deliberately NOT read+write sensitive: the registry reads it
        # on every listing and Settings reads it to render the inventory, so the
        # read gate must not fence it.
        assert is_sensitive_path(f"{prefix}/{_LEAF}") is False

    def test_entry_is_published_on_the_posture_surface(self) -> None:
        entries = write_protected_home_paths()
        matches = [e for e in entries if e.endswith("/" + _LEAF)]
        # Both data-home spellings, so a legacy install is gated too.
        assert len(matches) == 2, entries

    def test_sibling_data_home_paths_unaffected(self) -> None:
        """A prefix-neighbour of the entry must not be caught by it."""
        # A different file that merely shares the string prefix stays writable.
        assert is_sensitive_write_path("~/.kiro/crew/harnesses.json.bak") is False
        # Quick check the list is intact around the new entry.
        assert is_sensitive_write_path("~/.kiro/crew/config.json") is True

    def test_bash_write_is_sealed_by_the_sandbox(self) -> None:
        # Shell writes are fenced by the OS sandbox seal, not by command-string
        # parsing: the keystone bash gate matches no paths at all, so the OS
        # sandbox is the only layer that can hold the path fence. The descriptor
        # file must therefore sit in BOTH seal lists — the read-only seal for a
        # PRESENT file and the Linux pre-create seal for an ABSENT one (the default
        # on an install that has never authored a descriptor).
        from kiro_crew import sandbox

        assert _LEAF in sandbox._CREW_READONLY_LEAVES
        assert _LEAF in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES

    def test_descriptors_file_is_not_deliberately_excluded_from_precreate(self) -> None:
        # The pre-create list deliberately EXCLUDES ceilings whose empty document
        # does not mean "absent" or whose stale sealed read fails dangerously
        # (``denied_commands.json``, ``security_policy.json``, ``app_admission.json``,
        # ``admission_policy.json`` — see test_sandbox_absent_ceiling_seal). The
        # descriptor file is NOT one of those: an empty ``{}`` means "no operator
        # descriptors" (absent-equivalent) and a frozen ``{}`` under-reports backends
        # (fail-safe), so it is materialised rather than skipped.
        from kiro_crew import sandbox

        assert _LEAF not in {
            "denied_commands.json",
            "security_policy.json",
            "app_admission.json",
            "admission_policy.json",
        }
        assert _LEAF in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES


class TestHarnessDescriptorsNeverEditableConfigKey:
    """The descriptor file is NEVER a Settings-PATCH-writable config key."""

    def test_editable_config_has_no_harness_key(self) -> None:
        # INVARIANT: the descriptor definitions live in ``harnesses.json`` and are
        # edited out-of-band, never through the config PATCH allowlist. A
        # harness-definitions key here would reintroduce, through the dashboard
        # write path, exactly the agent-authored execution grant the file
        # protection above exists to prevent — the PATCH handler is auth-gated but
        # a prompt-injected agent that mints a dashboard token could still reach it.
        # The GLOBAL backend SELECTOR (``agent.acp_backend``) is a value setter, not
        # a definitions key, so it is allowed and does not match this pin.
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        offending = [key for key in _EDITABLE_CONFIG if "harness" in key.lower()]
        assert offending == [], (
            "no _EDITABLE_CONFIG key may contain 'harness' — descriptor definitions "
            f"must stay out of the config PATCH allowlist, found: {offending}"
        )
