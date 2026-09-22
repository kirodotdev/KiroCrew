"""A rebuild must reconcile a scope global's spec onto the entry it already wrote.

The generated agent spec is this rebuild's output AND one of its inputs, so the
merge of ``~/.kiro/settings/mcp.json`` (and of each extra scope global) ran
``setdefault`` against a name the previous pass had already put there -- a no-op.
A source the user then CHANGED never reached the file again: a bumped ``timeout``
stayed at its old value, a moved ``command`` stayed pinned to the old path.

The rule under test: the source owns its own MODIFIER keys and nothing else.
Those are overwritten on every pass, and one the source has since dropped is
removed, so the entry projects what the source says now. Every other key on the
entry is the user's and survives by omission -- ``autoApprove`` is the
load-bearing case, because overwriting the entry wholesale would silently revoke
a grant the user added to the generated spec, and preserving by an explicit
user-owned LIST would destroy the next user-owned field somebody invents.

Reconciliation applies only to a name a PREVIOUS rebuild left behind, and
retires it once claimed, so the declared precedence between scopes is untouched:
the provider global still loses to the kiro global for a name both declare.

These drive the REAL rebuild twice against one on-disk config, which is the only
way the readback participates at all. Harness shared with
``test_mcp_rebuild_reconsumption``; seeding each rebuild by hand is what made the
defect invisible.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp_merge_helpers import bundled_defaults as _bundled_defaults
from mcp_merge_helpers import run_install_mcp_merge as _run_install_mcp_merge

SERVER = "reconcile-me"


def _exe(tmp_path: Path, name: str) -> str:
    """A real resolvable absolute command, so resolution is not the thing failing."""
    path = tmp_path / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def _kiro(tmp_path: Path, cfg_dir: Path, spec: dict) -> dict:
    """One rebuild with *spec* as the kiro-global entry for :data:`SERVER`."""
    return _run_install_mcp_merge(tmp_path, cfg_dir, cc_servers={}, kiro_servers={SERVER: spec})[
        "mcpServers"
    ]


def _scope(tmp_path: Path, cfg_dir: Path, spec: dict) -> dict:
    """The same, via an EXTRA scope global -- the second merge site."""
    return _run_install_mcp_merge(tmp_path, cfg_dir, cc_servers={SERVER: spec}, kiro_servers={})[
        "mcpServers"
    ]


def _grant(tmp_path: Path, tools: list[str]) -> None:
    """Add an ``autoApprove`` to the GENERATED spec, as a user hand-edit would."""
    spec_path = tmp_path / "kiro_agents" / "kirocrew.json"
    cfg = json.loads(spec_path.read_text(encoding="utf-8"))
    cfg["mcpServers"][SERVER]["autoApprove"] = tools
    spec_path.write_text(json.dumps(cfg), encoding="utf-8")


@pytest.fixture(autouse=True)
def _stable_path(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")


class TestAChangedSourceReachesTheGeneratedSpec:
    """The defect: the second pass's source was thrown away."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "the TRANSPORT is deliberately not reconciled: two invariants in "
            "test_mcp_rebuild_reconsumption.py already own that question and this "
            "defers to both -- "
            "test_a_scope_entry_without_a_command_does_not_own_one[url-only] (a "
            "same-named scope entry declaring only a url must not own an agent-only "
            "stdio command) and "
            "test_a_non_resolving_scope_command_does_not_destroy_the_record (a scope "
            "command that does not resolve must leave the entry's own resolved "
            "command and its re-derivation record intact). Overwriting `command` "
            "deletes the one value _resolve_command's fallback can return to, so "
            "closing this needs that fallback to consider the entry's previous "
            "value -- a separate change with its own failure modes."
        ),
    )
    def test_a_changed_command_is_rewritten(self, tmp_path: Path) -> None:
        cfg_dir = _bundled_defaults(tmp_path)
        old, new = _exe(tmp_path, "old-srv"), _exe(tmp_path, "new-srv")

        first = _kiro(tmp_path, cfg_dir, {"command": old})
        assert first[SERVER]["command"] == old

        second = _kiro(tmp_path, cfg_dir, {"command": new})
        assert second[SERVER]["command"] == new, (
            "the entry the previous pass wrote absorbed the merge, so the source's "
            "new command never reached the generated spec"
        )

    def test_a_changed_timeout_lands(self, tmp_path: Path) -> None:
        """The reported symptom: source says 130000, generated spec kept 120000."""
        cfg_dir = _bundled_defaults(tmp_path)
        cmd = _exe(tmp_path, "srv")

        assert (
            _kiro(tmp_path, cfg_dir, {"command": cmd, "timeout": 120000})[SERVER]["timeout"]
            == 120000
        )
        assert (
            _kiro(tmp_path, cfg_dir, {"command": cmd, "timeout": 130000})[SERVER]["timeout"]
            == 130000
        )

    def test_a_changed_source_reaches_an_extra_scope_entry_too(self, tmp_path: Path) -> None:
        """The second merge site: the fix is called at both, so both are pinned."""
        cfg_dir = _bundled_defaults(tmp_path)
        cmd = _exe(tmp_path, "srv")

        _scope(tmp_path, cfg_dir, {"command": cmd, "timeout": 120000})
        assert (
            _scope(tmp_path, cfg_dir, {"command": cmd, "timeout": 130000})[SERVER]["timeout"]
            == 130000
        )

    def test_a_dropped_transport_key_is_removed(self, tmp_path: Path) -> None:
        """Projecting the source means an omitted key goes, not lingers forever."""
        cfg_dir = _bundled_defaults(tmp_path)
        cmd = _exe(tmp_path, "srv")

        _kiro(tmp_path, cfg_dir, {"command": cmd, "timeout": 120000})
        assert "timeout" not in _kiro(tmp_path, cfg_dir, {"command": cmd})[SERVER]


class TestAUserFieldOnTheEntrySurvives:
    """The risk the fixed key list bounds: an overwrite that revokes a grant."""

    def test_an_auto_approve_the_user_added_survives(self, tmp_path: Path, monkeypatch) -> None:
        """``mcp.honour_auto_approve`` is pinned on: the subject is preservation by
        omission, and the undeclared-grant floor would otherwise drop the key before
        the owned-key list is exercised at all."""
        from kiro_crew.config import live as _live
        from kiro_crew.config.loader import KiroCrewConfig as _Cfg

        _cfg = _Cfg()
        _cfg.mcp.honour_auto_approve = True
        monkeypatch.setattr(_live, "snapshot", lambda: _cfg)
        cfg_dir = _bundled_defaults(tmp_path)
        old, new = _exe(tmp_path, "old-srv"), _exe(tmp_path, "new-srv")

        _kiro(tmp_path, cfg_dir, {"command": old})
        _grant(tmp_path, ["safe_tool"])

        # Only the grant is asserted, deliberately: this test must still pass
        # against a restored ``setdefault``, because its job is to prove the FIX
        # destroys nothing. The command rewrite it enables is pinned above.
        merged = _kiro(tmp_path, cfg_dir, {"command": new})[SERVER]
        assert merged["autoApprove"] == ["safe_tool"], (
            "the source's transport keys must not carry the user's grant away with "
            "them -- preservation is by omission from the owned-key list"
        )

    def test_a_field_the_source_never_mentions_survives(self, tmp_path: Path) -> None:
        """Preserve-by-omission: an unmodelled key is kept without being listed."""
        cfg_dir = _bundled_defaults(tmp_path)
        cmd = _exe(tmp_path, "srv")

        _kiro(tmp_path, cfg_dir, {"command": cmd})
        spec_path = tmp_path / "kiro_agents" / "kirocrew.json"
        cfg = json.loads(spec_path.read_text(encoding="utf-8"))
        cfg["mcpServers"][SERVER]["someFutureUserField"] = {"kept": True}
        spec_path.write_text(json.dumps(cfg), encoding="utf-8")

        merged = _kiro(tmp_path, cfg_dir, {"command": cmd, "timeout": 5000})[SERVER]
        assert merged["someFutureUserField"] == {"kept": True}


class TestAnAppAssignedNameStillOutranksAGlobal:
    """The app manifest claims by ASSIGNMENT, which must also retire the name.

    ``_stale`` is seeded before the app loop, so an app entry the PREVIOUS rebuild
    wrote is in it. If the app loop did not discard the name on assignment, a
    same-named leftover in a shared global would reconcile onto the freshly
    manifest-derived spec, and the app server would launch with the leftover's
    values -- inverting the precedence the merge-order comment declares ("an app's
    namespaced entry outranks any same-named leftover in the shared global file").
    """

    def test_a_shared_global_leftover_does_not_reconcile_onto_an_app_entry(
        self, tmp_path: Path
    ) -> None:
        cfg_dir = _bundled_defaults(tmp_path)
        cmd = _exe(tmp_path, "srv")
        app_spec = {"command": cmd, "timeout": 111}
        leftover = {"command": cmd, "timeout": 999}

        def _emit() -> dict:
            with patch(
                "kiro_crew.agent._collect_app_mcp_servers",
                lambda **kw: {SERVER: dict(app_spec)},
            ):
                return _kiro(tmp_path, cfg_dir, leftover)

        assert _emit()[SERVER]["timeout"] == 111
        # The second pass is the one that regressed: now the name IS in _stale.
        assert _emit()[SERVER]["timeout"] == 111, (
            "a shared-global leftover reconciled onto the app's manifest-derived "
            "spec, so the app server would launch with the leftover's values"
        )
