"""A rebuild must reconcile a scope global's spec onto the entry it already wrote.

The generated spec is also an input, so a plain ``setdefault`` merge would ignore a
changed source. The source owns its MODIFIER keys: overwritten each pass, removed when
dropped. Every other key is the user's (``autoApprove`` is the key case) and survives.

Only a name a PREVIOUS rebuild left behind is reconciled, so scope precedence holds.
The REAL rebuild runs twice on one on-disk config; hand seeding would skip the readback.
"""

from __future__ import annotations

import json
import logging
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
    path.write_text("#!/bin/sh\n", encoding="utf-8")
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

    def test_a_changed_command_is_rewritten(self, tmp_path: Path) -> None:
        cfg_dir = _bundled_defaults(tmp_path)
        old, new = _exe(tmp_path, "old-srv"), _exe(tmp_path, "new-srv")

        first = _kiro(tmp_path, cfg_dir, {"command": old, "env": {"FOR": "old"}})
        assert first[SERVER]["command"] == old

        second = _kiro(tmp_path, cfg_dir, {"command": new, "env": {"FOR": "new"}})
        assert second[SERVER]["command"] == new, (
            "the entry the previous pass wrote absorbed the merge, so the source's "
            "new command never reached the generated spec"
        )
        assert second[SERVER]["env"] == {"FOR": "new"}

    def test_a_store_command_keeps_its_own_unit_over_a_switched_global(
        self, tmp_path: Path
    ) -> None:
        """``~/.kiro/crew/mcp.json`` outranks the Kiro global's new program and its args."""
        cfg_dir = _bundled_defaults(tmp_path)
        store, other = _exe(tmp_path, "store-srv"), _exe(tmp_path, "other-srv")

        def _emit(kiro_servers: dict) -> dict:
            return _run_install_mcp_merge(
                tmp_path,
                cfg_dir,
                cc_servers={},
                kiro_servers=kiro_servers,
                kirocrew_servers={SERVER: {"command": store}},
            )["mcpServers"][SERVER]

        _emit({})
        merged = _emit({SERVER: {"command": other, "args": ["--other"]}})
        assert merged["command"] == store
        assert "args" not in merged, "the global's args were paired with the store's command"

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


class TestTheCommandsUnitFollowsAnUnchangedCommand:
    """An ``env`` added AFTER the first rebuild reaches the server the entry runs.

    ``args`` and ``env`` follow the command that actually runs, never a different
    program's: the as-a-unit rule.
    """

    def test_an_env_key_added_later_lands(self, tmp_path: Path) -> None:
        cfg_dir = _bundled_defaults(tmp_path)
        cmd = _exe(tmp_path, "srv")

        first = _kiro(tmp_path, cfg_dir, {"command": cmd, "env": {"FIREFOX_BIN": "/ff"}})
        assert first[SERVER]["env"] == {"FIREFOX_BIN": "/ff"}

        later = {"FIREFOX_BIN": "/ff", "CONNECT_EXISTING": "true", "MARIONETTE_PORT": "2828"}
        second = _kiro(tmp_path, cfg_dir, {"command": cmd, "env": later})
        assert second[SERVER]["env"] == later, (
            "the entry the previous pass wrote kept its first-pass env, so the "
            "variables added to the source later never reached the spawned server"
        )

    def test_changed_args_land(self, tmp_path: Path) -> None:
        cfg_dir = _bundled_defaults(tmp_path)
        cmd = _exe(tmp_path, "srv")

        _kiro(tmp_path, cfg_dir, {"command": cmd, "args": ["-y", "pkg"]})
        new_args = ["-y", "pkg", "--connect-existing", "--marionette-port", "2828"]
        assert _kiro(tmp_path, cfg_dir, {"command": cmd, "args": new_args})[SERVER]["args"] == (
            new_args
        )

    def test_a_dropped_env_is_removed(self, tmp_path: Path) -> None:
        """The unit is projected, not merged: a key the source dropped goes too."""
        cfg_dir = _bundled_defaults(tmp_path)
        cmd = _exe(tmp_path, "srv")

        _kiro(tmp_path, cfg_dir, {"command": cmd, "env": {"OLD": "1"}})
        assert "env" not in _kiro(tmp_path, cfg_dir, {"command": cmd})[SERVER]

    def test_the_env_follows_a_bare_command_the_entry_holds_resolved(self, tmp_path: Path) -> None:
        """The agent entry pins the RESOLVED path of the source's bare name.

        ``npx`` in mcp.json becomes ``/…/bin/npx`` in the generated spec, so a
        string comparison would read every stdio server as a changed program and
        the fix would never apply to the case it was written for.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        resolved = _exe(tmp_path, "npx")

        def _which(c, **kw):
            return resolved if c == "npx" else c

        def _rebuild(env: dict) -> dict:
            return _run_install_mcp_merge(
                tmp_path,
                cfg_dir,
                cc_servers={},
                kiro_servers={SERVER: {"command": "npx", "env": env}},
                which_side_effect=_which,
            )["mcpServers"]

        first = _rebuild({"A": "1"})
        assert first[SERVER]["command"] == resolved
        assert first[SERVER]["env"] == {"A": "1"}

        assert _rebuild({"A": "1", "B": "2"})[SERVER]["env"] == {"A": "1", "B": "2"}

    def test_a_switched_program_that_does_not_resolve_keeps_the_old_unit(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A new program missing on this host must not drop the server or lend its env.

        The entry falls back to the command it ran, with that command's own env, and
        says so: the user edited the source and needs to know it did not land.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        old, missing = _exe(tmp_path, "old-srv"), str(tmp_path / "bin" / "not-installed")

        _kiro(tmp_path, cfg_dir, {"command": old, "args": ["--old"], "env": {"FOR": "old"}})
        new_spec = {"command": missing, "args": ["--new"], "env": {"FOR": "missing"}}
        with caplog.at_level(logging.WARNING):
            second = _run_install_mcp_merge(
                tmp_path,
                cfg_dir,
                cc_servers={},
                kiro_servers={SERVER: new_spec},
                which_side_effect=lambda c, **kw: None if c == missing else c,
            )["mcpServers"][SERVER]
        assert second["command"] == old
        assert second["args"] == ["--old"]
        assert second["env"] == {"FOR": "old"}
        assert f"its new command {missing} did not resolve" in caplog.text

    def test_a_user_grant_survives_the_env_reconcile(self, tmp_path: Path, monkeypatch) -> None:
        """Adopting the unit touches ``args``/``env`` only; the user's keys stay."""
        from kiro_crew.config import live as _live
        from kiro_crew.config.loader import KiroCrewConfig as _Cfg

        _cfg = _Cfg()
        _cfg.mcp.honour_auto_approve = True
        monkeypatch.setattr(_live, "snapshot", lambda: _cfg)
        cfg_dir = _bundled_defaults(tmp_path)
        cmd = _exe(tmp_path, "srv")

        _kiro(tmp_path, cfg_dir, {"command": cmd, "env": {"A": "1"}})
        _grant(tmp_path, ["safe_tool"])

        merged = _kiro(tmp_path, cfg_dir, {"command": cmd, "env": {"A": "2"}})[SERVER]
        assert merged["env"] == {"A": "2"}
        assert merged["autoApprove"] == ["safe_tool"]


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

    ``_stale`` is seeded before the app loop, so without the discard a same-named
    leftover in a shared global would reconcile onto the manifest-derived spec.
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
