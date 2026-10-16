"""Tests for how a spec's MCP servers are merged from the global sources.

Source priority and resolution-aware fallback, the effective ``env.PATH`` a
spec is written with, and the reconcile that keeps an enabled app's servers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent_helpers import _pinned_kiro_cli_version  # noqa: F401

# Shared with ``test_mcp_rebuild_reconsumption`` via a dedicated helpers module --
# the repo's convention, since a test module is not importable from another one.
# Re-exported under the original private names so the call sites below are unchanged.
from mcp_merge_helpers import bundled_defaults as _bundled_defaults
from mcp_merge_helpers import run_install_mcp_merge as _run_install_mcp_merge

from conftest import host_abs

# ---------------------------------------------------------------------------
# MCP merge: CC-global vs Kiro-global priority + resolution-aware fallback
# (regression coverage for the builder-mcp shadowing bug — a bare/unresolvable
# command in the higher-priority source must not shadow a resolvable command
# in a lower-priority source, and Kiro-global outranks CC-global.)
# ---------------------------------------------------------------------------


def _make_exec(tmp_path: Path, name: str) -> str:
    """Create a real executable file and return its absolute path."""
    p = tmp_path / "bin" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return str(p)


def _abs(*parts: str) -> str:
    """Host-absolute fixture path; see ``conftest.host_abs`` for why ``/opt/shims`` is not enough."""
    return host_abs(*parts)


class TestSpecEnvPathIsExpandedOnEmit:
    """A spec's ``env.PATH`` is written out as the full effective PATH.

    The spec's ``env`` is applied per key, so a declared ``PATH`` REPLACES the
    child's inherited one. Emitting the fragment verbatim hands the server a
    PATH holding only the directories the user happened to name, which breaks
    any launcher that resolves a sibling binary at runtime while the dashboard
    probe — which merges instead of replacing — still reports it healthy.
    """

    def test_declared_path_is_expanded(self, tmp_path: Path, monkeypatch) -> None:
        # Host-absolute spellings: the spec's entries pass through the
        # ``os.path.isabs`` filter in ``_spec_path_entries``, and from Python 3.13
        # ``ntpath.isabs("/opt/shims")`` is False (no drive), so a POSIX spelling
        # is dropped as "non-absolute" on Windows and the assertion below sees the
        # augmentation first instead of the declared dir.
        shims, usr_bin, bin_dir = _abs("opt", "shims"), _abs("usr", "bin"), _abs("bin")
        monkeypatch.setenv("PATH", os.pathsep.join([usr_bin, bin_dir]))
        cfg_dir = _bundled_defaults(tmp_path)
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"wrapped": {"command": "/opt/wrapped", "env": {"PATH": shims}}},
        )
        emitted = config["mcpServers"]["wrapped"]["env"]["PATH"].split(os.pathsep)
        # The declared dir stays first, and the inherited PATH survives.
        assert emitted[0] == shims
        assert usr_bin in emitted
        assert bin_dir in emitted

    def test_other_env_keys_are_untouched(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={
                "wrapped": {
                    "command": "/opt/wrapped",
                    "env": {"PATH": "/opt/shims", "TOKEN_FILE": "/etc/token"},
                }
            },
        )
        assert config["mcpServers"]["wrapped"]["env"]["TOKEN_FILE"] == "/etc/token"

    def test_spec_without_path_is_left_alone(self, tmp_path: Path, monkeypatch) -> None:
        """No env.PATH means the child inherits a usable PATH already.

        Writing one anyway would bake this host's directory list into every
        config that does not need it.
        """
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"plain": {"command": "/opt/plain", "env": {"TOKEN_FILE": "/etc/token"}}},
        )
        assert config["mcpServers"]["plain"]["env"] == {"TOKEN_FILE": "/etc/token"}

    def test_empty_path_value_is_expanded(self, tmp_path: Path, monkeypatch) -> None:
        """An empty declared PATH expands exactly like the probe expands it.

        The probe and the command resolver run ``spec_env_path("")`` (the
        augmented inherited PATH); emitting the raw empty string instead hands
        the session a child with NO path while the probe shows green — the
        probe/session divergence this whole emit path exists to close.
        """
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"blank": {"command": "/opt/blank", "env": {"PATH": ""}}},
        )
        emitted = config["mcpServers"]["blank"]["env"]["PATH"]
        assert emitted != ""
        assert "/usr/bin" in emitted.split(os.pathsep)

    def test_non_string_path_is_left_verbatim(self, tmp_path: Path, monkeypatch) -> None:
        """A malformed value must not be rewritten into a working-looking PATH."""
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"broken": {"command": "/opt/broken", "env": {"PATH": ["/opt/a"]}}},
        )
        assert config["mcpServers"]["broken"]["env"]["PATH"] == ["/opt/a"]

    def test_command_resolves_via_the_inherited_half(self, tmp_path: Path, monkeypatch) -> None:
        """Resolution must search more than the spec's own fragment.

        The other tests in this class stub ``shutil.which`` to resolve anything,
        so they only pin the emitted bytes. This one honours the ``path=``
        kwarg, so it catches an expansion narrowed to the declared fragment —
        which would leave a bare command resolvable only through the inherited
        half silently dropped from the config.
        """
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)

        def _path_aware(cmd, **kw):  # noqa: ANN001, ANN003 - test shim
            if os.path.isabs(cmd):
                return cmd
            searched = (kw.get("path") or "").split(os.pathsep)
            # Resolvable ONLY through the inherited half of the expansion.
            return "/usr/bin/srv" if cmd == "srv" and "/usr/bin" in searched else None

        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"srv": {"command": "srv", "env": {"PATH": "/opt/shims"}}},
            which_side_effect=_path_aware,
        )
        assert config["mcpServers"]["srv"]["command"] == "/usr/bin/srv"

    def test_source_config_env_is_not_mutated(self, tmp_path: Path, monkeypatch) -> None:
        """``dict(spec)`` is shallow, so the env dict must be copied before write.

        Mutating through would rewrite the caller's in-memory source config and
        leak the expanded value back into whatever else reads it.
        """
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_mcp = tmp_path / "fake_kiro_mcp.json"
        _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"wrapped": {"command": "/opt/wrapped", "env": {"PATH": "/opt/shims"}}},
        )
        on_disk = json.loads(kiro_mcp.read_text(encoding="utf-8"))
        assert on_disk["mcpServers"]["wrapped"]["env"]["PATH"] == "/opt/shims"

    def test_rebuild_is_stable(self, tmp_path: Path, monkeypatch) -> None:
        """install_agent runs on every start; the emitted PATH must not grow."""
        monkeypatch.setenv("PATH", os.pathsep.join([_abs("usr", "bin"), _abs("bin")]))
        cfg_dir = _bundled_defaults(tmp_path)
        servers = {"wrapped": {"command": "/opt/wrapped", "env": {"PATH": _abs("opt", "shims")}}}
        first = _run_install_mcp_merge(tmp_path, cfg_dir, cc_servers={}, kiro_servers=servers)[
            "mcpServers"
        ]["wrapped"]["env"]["PATH"]
        second = _run_install_mcp_merge(tmp_path, cfg_dir, cc_servers={}, kiro_servers=servers)[
            "mcpServers"
        ]["wrapped"]["env"]["PATH"]
        assert first == second
        assert len(first.split(os.pathsep)) == len(set(first.split(os.pathsep)))


class TestRebuildReconcileRetainsEnabledAppServers:
    """The final kirocrew.json reconcile must not delete an ENABLED app's
    manifest-derived MCP server just because it is absent from on-disk — a clean
    rebuild (or a missing/empty config) starts with an empty on_disk, and the
    app's tools would vanish. It must drop a server only when its app is
    confirmed not enabled (a concurrent deregister).

    Pinned by source inspection: the reconcile is an inline block in the
    rebuild's commit phase (``default_spec_commit.write_default_spec``, which
    ``install_agent`` calls) gated on ``is_kirocrew_json`` (the written path
    equalling ``bridges._mcp_json_path()``), which the merge-priority harness does
    not reproduce — so the guarantee is asserted structurally.
    """

    def test_reconcile_drops_by_enabled_state_not_ondisk_absence(self) -> None:
        import inspect

        from kiro_crew import agent
        from kiro_crew.agent_materialization import default_spec_commit

        assert "default_spec_commit.write_default_spec(" in inspect.getsource(agent.install_agent)
        src = inspect.getsource(default_spec_commit.write_default_spec)
        # The drop must be gated on the app being DISABLED (deregistered), not on
        # mere absence from on_disk — else a clean rebuild with an empty on_disk
        # would delete an enabled app's manifest-derived server.
        assert "_app_of_key_enabled" in src
        assert "if not _app_of_key_enabled(_k):" in src
        assert "is_app_enabled" in src
        # The old, buggy condition (delete when absent from on_disk) must be gone.
        assert "if _k not in on_disk_app:" not in src


class TestMcpMergePriority:
    def test_the_authorship_marker_never_reaches_the_rendered_spec(self, tmp_path: Path):
        """A shared-file marker is provenance, not configuration.

        The marker records that Kiro Crew wrote an entry into a file it does NOT
        own. The rendered spec is ours, so carrying the key through would put a
        field in front of the runtime that says nothing to it -- and would change
        the emitted spec for every managed remote, which nothing about recording
        authorship needs to do.
        """
        from kiro_crew.mcp_provenance import MARKER_KEY, stamp

        cfg_dir = _bundled_defaults(tmp_path)
        kiro_cmd = _make_exec(tmp_path, "marked-srv")
        cc_cmd = _make_exec(tmp_path, "cc-marked-srv")
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={"cc-srv": stamp({"command": cc_cmd})},
            kiro_servers={"kiro-srv": stamp({"command": kiro_cmd})},
        )
        for name in ("kiro-srv", "cc-srv"):
            assert name in config["mcpServers"], f"{name} must still be merged"
            assert MARKER_KEY not in config["mcpServers"][name]

    def test_kiro_global_outranks_cc_global(self, tmp_path: Path):
        """Same server in both globals → Kiro-global command wins (CC down-ranked)."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_cmd = _make_exec(tmp_path, "kiro-srv")
        cc_cmd = _make_exec(tmp_path, "cc-srv")
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={"shared-srv": {"command": cc_cmd}},
            kiro_servers={"shared-srv": {"command": kiro_cmd}},
        )
        assert config["mcpServers"]["shared-srv"]["command"] == kiro_cmd

    def test_unresolvable_cc_does_not_shadow_resolvable_kiro(self, tmp_path: Path):
        """CC bare/unresolvable command must not shadow Kiro's resolvable absolute path."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_cmd = _make_exec(tmp_path, "real-srv")
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={"srv": {"command": "bare-builder-mcp"}},
            kiro_servers={"srv": {"command": kiro_cmd}},
            which_side_effect=lambda c, **kw: None if c == "bare-builder-mcp" else c,
        )
        assert "srv" in config["mcpServers"], "server was dropped (shadowed by bare CC command)"
        assert config["mcpServers"]["srv"]["command"] == kiro_cmd

    def test_fallback_to_resolvable_lower_source(self, tmp_path: Path):
        """If the winning source's command is unresolvable, fall back to a
        resolvable command from another source instead of dropping the server."""
        cfg_dir = _bundled_defaults(tmp_path)
        cc_cmd = _make_exec(tmp_path, "cc-real")
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={"srv": {"command": cc_cmd}},
            kiro_servers={"srv": {"command": "bare-kiro"}},
            which_side_effect=lambda c, **kw: None if c == "bare-kiro" else c,
        )
        assert "srv" in config["mcpServers"], "server dropped instead of falling back"
        assert config["mcpServers"]["srv"]["command"] == cc_cmd

    def test_fallback_adopts_source_args_env_unit(self, tmp_path: Path, monkeypatch):
        """On cross-source fallback, the resolving source's command/args/env
        are adopted as a unit — the winner's stale args/env must not leak in,
        but non-command fields (autoApprove) are preserved.

        The opt-in is pinned on because the non-command field this pins is a
        hand-added ``autoApprove``, which the undeclared-grant floor drops.
        """
        from kiro_crew.config import live as _live
        from kiro_crew.config.loader import KiroCrewConfig as _Cfg

        _cfg = _Cfg()
        _cfg.mcp.honour_auto_approve = True
        monkeypatch.setattr(_live, "snapshot", lambda: _cfg)
        cfg_dir = _bundled_defaults(tmp_path)
        cc_cmd = _make_exec(tmp_path, "cc-real")
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            # CC (lower priority) is the resolvable one and carries its own args.
            cc_servers={"srv": {"command": cc_cmd, "args": ["--cc"], "env": {"CC": "1"}}},
            # Kiro wins priority but is unresolvable and has different args +
            # a non-command field (autoApprove) that should survive.
            kiro_servers={
                "srv": {
                    "command": "bare-kiro",
                    "args": ["--kiro"],
                    "autoApprove": ["tool"],
                }
            },
            which_side_effect=lambda c, **kw: None if c == "bare-kiro" else c,
        )
        srv = config["mcpServers"]["srv"]
        assert srv["command"] == cc_cmd
        assert srv["args"] == ["--cc"]  # adopted from the resolving source
        assert srv.get("env") == {"CC": "1"}  # adopted from the resolving source
        assert srv.get("autoApprove") == ["tool"]  # winner's non-command field kept

    def test_server_without_command_is_dropped(self, tmp_path: Path):
        """A server with no command in any source is dropped (no crash)."""
        cfg_dir = _bundled_defaults(tmp_path)
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"srv": {"args": ["--x"]}},  # no command anywhere
        )
        assert "srv" not in config["mcpServers"]

    def test_registry_pointer_without_command_is_carried(self, tmp_path: Path):
        """A commandless ``"type": "registry"`` pointer kiro-cli wrote into
        ~/.kiro/settings/mcp.json reaches the spec verbatim, with its tool ref.

        kiro-cli resolves it by map key against the admin's catalog, and the spec
        pins ``includeMcpJson: false``, so this copy is the only route by which the
        session sees it. Dropping it as "no command" would hide every governed server.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        pointer = {"type": "registry", "disabled": False}
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"aws-iac-mcp": dict(pointer)},
        )
        entry = config["mcpServers"].get("aws-iac-mcp")
        assert isinstance(entry, dict), sorted(config["mcpServers"])
        # Still a pointer: nothing local was invented for the catalog to override.
        assert entry.get("type") == "registry"
        assert "command" not in entry and "url" not in entry
        assert "@aws-iac-mcp" in config["tools"]

    def test_unmarked_entry_with_type_other_than_registry_is_still_dropped(self, tmp_path: Path):
        """Only the registry marker carries a commandless entry; any other ``type``
        with no command or url is still a broken local entry and is dropped."""
        cfg_dir = _bundled_defaults(tmp_path)
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"srv": {"type": "stdio"}},
        )
        assert "srv" not in config["mcpServers"]

    def test_kirocrew_override_does_not_corrupt_global_fallback(self, tmp_path: Path):
        """Regression (review-bot): a kirocrew mcp.json override carrying an
        unresolvable command must not mutate the shared kiro-global source dict
        that is reused as a fallback candidate. The resolvable kiro-global
        command must still be recovered via the fallback (server not dropped)."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_cmd = _make_exec(tmp_path, "kiro-real")
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"srv": {"command": kiro_cmd}},
            kirocrew_servers={"srv": {"command": "bare-unresolvable"}},
            which_side_effect=lambda c, **kw: None if c == "bare-unresolvable" else c,
        )
        assert (
            "srv" in config["mcpServers"]
        ), "server dropped: global source dict corrupted by override"
        assert config["mcpServers"]["srv"]["command"] == kiro_cmd
