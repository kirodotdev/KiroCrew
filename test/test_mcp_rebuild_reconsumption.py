"""A rebuild must not consume its own previous output.

The agent spec is this rebuild's output AND one of its inputs. The resolved
absolute ``command`` in it is computed, not authored, so reading it back as the
user's makes it permanent: ``_resolve_command`` accepts an absolute existing
executable with no PATH search, so a command resolved once can never be rebound.

These run the REAL rebuild repeatedly against one on-disk config, the only way the
readback participates; seeding each rebuild by hand would hide the bug.

The record applies only to a server no other config source declares;
``TestScopeOwnedIsExcluded`` pins that boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp_merge_helpers import bundled_defaults as _bundled_defaults
from mcp_merge_helpers import run_install_mcp_merge as _run_install_mcp_merge

from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.mcp_provenance import DERIVED_KEY


@pytest.fixture(autouse=True)
def _pinned_kiro_cli_version(monkeypatch):
    """Pin the kiro-cli release the spec ``permissions`` gate believes is installed.

    Every rebuild ends in ``_write_derived_permissions``, which would otherwise spawn
    the HOST's real ``kiro-cli --version``. Pinned to the floor release, as
    ``test_agent.py`` does.
    """
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


def _emitted(tmp_path: Path, cfg_dir: Path, kiro_servers: dict, **kw) -> dict:
    return _run_install_mcp_merge(
        tmp_path, cfg_dir, cc_servers={}, kiro_servers=kiro_servers, **kw
    )["mcpServers"]


def _make_agent_only(tmp_path: Path, name: str, command: str) -> None:
    """Turn an emitted entry into an agent-only one: bare command, no scope entry.

    This is the shape ``kiro-cli mcp add --agent kirocrew`` and a hand-edit both
    produce -- a server whose only persisted home is the generated spec.
    """
    spec_path = tmp_path / "kiro_agents" / "kirocrew.json"
    cfg = json.loads(spec_path.read_text())
    cfg["mcpServers"][name] = {"command": command}
    spec_path.write_text(json.dumps(cfg))


class TestAnAgentOnlyCommandIsReDerived:
    """The fix: a computed command is re-resolved rather than pinned."""

    def test_a_relocated_binary_is_re_resolved(self, tmp_path: Path, monkeypatch) -> None:
        """A stored absolute path must not pin a command whose location moved.

        ``_resolve_command`` accepts an absolute existing executable without a PATH
        search, so an emitted path still on disk short-circuits every later
        resolution. Re-deriving from the recorded source is what lets a changed
        search path rebind it.
        """
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        old = tmp_path / "old" / "srv"
        new = tmp_path / "new" / "srv"
        for p in (old, new):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)

        _emitted(tmp_path, cfg_dir, {})
        _make_agent_only(tmp_path, "s", "srv")

        first = _emitted(
            tmp_path, cfg_dir, {}, which_side_effect=lambda c, **kw: str(old) if c == "srv" else c
        )
        assert first["s"]["command"] == str(old)
        assert first["s"][DERIVED_KEY]["from"] == "srv"

        second = _emitted(
            tmp_path, cfg_dir, {}, which_side_effect=lambda c, **kw: str(new) if c == "srv" else c
        )
        assert second["s"]["command"] == str(new), (
            "the stored absolute path short-circuited resolution, so the command "
            "could not be rebound"
        )

    def test_re_derivation_is_stable_across_repeated_rebuilds(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Nothing accumulates and nothing drifts when the source has not changed."""
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        binary = tmp_path / "bin" / "srv"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)

        _emitted(tmp_path, cfg_dir, {})
        _make_agent_only(tmp_path, "s", "srv")
        which = lambda c, **kw: str(binary) if c == "srv" else c  # noqa: E731
        seen = [_emitted(tmp_path, cfg_dir, {}, which_side_effect=which)["s"] for _ in range(3)]
        assert all(e["command"] == str(binary) for e in seen)
        assert all(e[DERIVED_KEY] == seen[0][DERIVED_KEY] for e in seen)

    def test_an_unresolvable_source_never_deletes_the_server(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Re-derivation must never cost the entry its only copy.

        If the recorded source stops resolving (a shim removed from PATH) but the
        emitted path works, keep the emitted path: this file is the only copy.
        """
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        target = tmp_path / "target" / "srv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)

        _emitted(tmp_path, cfg_dir, {})
        _make_agent_only(tmp_path, "s", "srv")
        first = _emitted(
            tmp_path,
            cfg_dir,
            {},
            which_side_effect=lambda c, **kw: str(target) if c == "srv" else c,
        )
        assert first["s"]["command"] == str(target)

        # "srv" does not resolve; the emitted absolute path still exists.
        second = _emitted(tmp_path, cfg_dir, {}, which_side_effect=lambda c, **kw: None)
        assert "s" in second, "the server was dropped, and this file was its only copy"
        assert second["s"]["command"] == str(target)
        # The record must survive VERBATIM, so a later PATH fix can still rebind it.
        assert second["s"][DERIVED_KEY] == {
            "from": "srv",
            "emitted": str(target),
        }

    @pytest.mark.parametrize(
        "scope_entry",
        [
            None,  # malformed: supplies nothing
            {},  # empty: declares no command
            {"url": "https://example.invalid"},  # remote: declares no command
            {"command": ""},  # blank: not a usable command
        ],
        ids=["null", "empty", "url-only", "blank-command"],
    )
    def test_a_scope_entry_without_a_command_does_not_own_one(
        self, tmp_path: Path, monkeypatch, scope_entry: object
    ) -> None:
        """Ownership of the COMMAND needs a scope that actually declares one.

        A same-named entry that declares no command is not a competing source, so
        treating it as one would strip the record off an agent-only stdio server and
        strand its stale path -- the defect this fixes, reintroduced sideways.
        """
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        old = tmp_path / "old" / "srv"
        new = tmp_path / "new" / "srv"
        for p in (old, new):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)

        _emitted(tmp_path, cfg_dir, {})
        _make_agent_only(tmp_path, "s", "srv")
        scope = {"s": scope_entry}
        first = _emitted(
            tmp_path,
            cfg_dir,
            scope,
            which_side_effect=lambda c, **kw: str(old) if c == "srv" else c,
        )
        assert DERIVED_KEY in first["s"], "the record was stripped by a non-command scope entry"

        second = _emitted(
            tmp_path,
            cfg_dir,
            scope,
            which_side_effect=lambda c, **kw: str(new) if c == "srv" else c,
        )
        assert second["s"]["command"] == str(new)

    def test_a_non_resolving_scope_command_does_not_destroy_the_record(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A scope-owned server's record is inert, not deleted.

        If the scope entry goes away, the server is agent-only again and needs the
        record to rebind a relocated binary.
        """
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        old = tmp_path / "old" / "srv"
        new = tmp_path / "new" / "srv"
        for p in (old, new):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)
        which = lambda c, **kw: str(old) if c == "srv" else None  # noqa: E731

        _emitted(tmp_path, cfg_dir, {})
        _make_agent_only(tmp_path, "s", "srv")
        first = _emitted(tmp_path, cfg_dir, {}, which_side_effect=which)
        assert first["s"][DERIVED_KEY] == {"from": "srv", "emitted": str(old)}

        # A same-named scope command appears and never resolves.
        with_scope = _emitted(
            tmp_path, cfg_dir, {"s": {"command": "nonexistent-binary"}}, which_side_effect=which
        )
        assert (
            DERIVED_KEY in with_scope["s"]
        ), "a non-resolving scope command destroyed the only re-derivation source"
        assert with_scope["s"][DERIVED_KEY] == {"from": "srv", "emitted": str(old)}

        # The scope entry goes away and the binary has moved: rebinding must work.
        back = _emitted(
            tmp_path,
            cfg_dir,
            {},
            which_side_effect=lambda c, **kw: str(new) if c == "srv" else None,
        )
        assert back["s"]["command"] == str(new)

    def test_a_resolving_scope_command_does_not_take_over_an_agent_only_server(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A scope switching programs rewrites a scope-owned entry, never a recorded one.

        A rewrite would leave the record naming a command the entry does not run, so
        it could not rebind the server once the scope entry goes away.
        """
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        old, other = tmp_path / "old" / "srv", tmp_path / "other" / "tool"
        for p in (old, other):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)
        which = lambda c, **kw: str(old) if c == "srv" else None  # noqa: E731

        _emitted(tmp_path, cfg_dir, {})
        _make_agent_only(tmp_path, "s", "srv")
        _emitted(tmp_path, cfg_dir, {}, which_side_effect=which)

        with_scope = _emitted(
            tmp_path, cfg_dir, {"s": {"command": str(other)}}, which_side_effect=which
        )
        assert with_scope["s"]["command"] == str(old)
        assert with_scope["s"][DERIVED_KEY] == {"from": "srv", "emitted": str(old)}

    def test_a_recovered_source_rebinds_after_being_retained(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Retaining the record is what makes the pause recoverable."""
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        old = tmp_path / "old" / "srv"
        new = tmp_path / "new" / "srv"
        for p in (old, new):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)

        _emitted(tmp_path, cfg_dir, {})
        _make_agent_only(tmp_path, "s", "srv")
        _emitted(
            tmp_path, cfg_dir, {}, which_side_effect=lambda c, **kw: str(old) if c == "srv" else c
        )
        _emitted(tmp_path, cfg_dir, {}, which_side_effect=lambda c, **kw: None)  # source broken
        back = _emitted(
            tmp_path, cfg_dir, {}, which_side_effect=lambda c, **kw: str(new) if c == "srv" else c
        )
        assert back["s"]["command"] == str(new), (
            "the retained record did not survive the unresolvable pass, so the "
            "command could not be rebound once the source came back"
        )


class TestUserEditsAreNotOverwritten:
    """Provenance must protect a hand edit, not undo it."""

    def test_a_hand_edited_command_is_left_alone(self, tmp_path: Path, monkeypatch) -> None:
        """If the stored value is not ours, the user owns it.

        Same rule the entry-level marker applies: an entry we cannot prove we wrote
        is never rewritten. At field level the proof is that the stored value is
        still exactly what we emitted.
        """
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        found = tmp_path / "found" / "srv"
        chosen = tmp_path / "chosen" / "srv"
        for p in (found, chosen):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)
        which = lambda c, **kw: str(found) if c == "srv" else c  # noqa: E731

        _emitted(tmp_path, cfg_dir, {})
        _make_agent_only(tmp_path, "s", "srv")
        _emitted(tmp_path, cfg_dir, {}, which_side_effect=which)

        spec_path = tmp_path / "kiro_agents" / "kirocrew.json"
        cfg = json.loads(spec_path.read_text())
        cfg["mcpServers"]["s"]["command"] = str(chosen)
        spec_path.write_text(json.dumps(cfg))

        # Twice: surviving one rebuild is not enough. If the emit recorded the edit
        # as ours, the SECOND pass would read our own record as proof and re-derive
        # over it.
        for pass_no in (1, 2):
            out = _emitted(tmp_path, cfg_dir, {}, which_side_effect=which)
            assert out["s"]["command"] == str(
                chosen
            ), f"the hand edit was reclaimed on pass {pass_no}"


class TestTheRecordIsNotPartOfSpecIdentity:
    """The record must never make two copies of one server look like two servers.

    ``_normalize_mcp_server_keys`` mints ``-2``/``-3`` suffixes for specs that differ
    after normalization, so the record must not count as a difference.
    """

    def test_the_record_is_excluded_from_the_dedup_comparison(self) -> None:
        from kiro_crew.agent import _norm_mcp_spec

        plain = {"command": "/abs/srv", "args": ["--x"]}
        recorded = dict(plain)
        recorded[DERIVED_KEY] = {"from": "srv", "emitted": "/abs/srv"}
        assert _norm_mcp_spec(recorded) == _norm_mcp_spec(plain)

    def test_a_real_difference_still_differs(self) -> None:
        """The exclusion must not flatten specs that genuinely disagree."""
        from kiro_crew.agent import _norm_mcp_spec

        a = {"command": "/abs/one", DERIVED_KEY: {"from": "srv", "emitted": "/abs/one"}}
        b = {"command": "/abs/two"}
        assert _norm_mcp_spec(a) != _norm_mcp_spec(b)


class TestScopeOwnedIsExcluded:
    """A server another source declares gets no record.

    Choosing between the record and a live declaration would be a merge-precedence
    change (per-field source after resolution), not a provenance one.
    """

    def test_a_scope_owned_server_gets_no_record(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        out = _emitted(tmp_path, cfg_dir, {"s": {"command": "/opt/s"}})
        assert DERIVED_KEY not in out["s"]

    def test_an_alias_keyed_scope_entry_still_counts_as_owned(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A scope keys by its own raw name; the config key was alias-normalized.

        The merge normalizes a scope entry's raw key, so a slash-keyed scope server and
        an alias-keyed config server become ONE entry (the assertion below shows it). A
        raw-key probe would miss that owner and let the record shadow it.
        """
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg_dir = _bundled_defaults(tmp_path)
        scope_bin = tmp_path / "scope" / "srv"
        scope_bin.parent.mkdir(parents=True, exist_ok=True)
        scope_bin.write_text("#!/bin/sh\n")
        scope_bin.chmod(0o755)

        _emitted(tmp_path, cfg_dir, {})
        _make_agent_only(tmp_path, "vendor-srv", "gone-binary")
        out = _emitted(
            tmp_path,
            cfg_dir,
            {"vendor/srv": {"command": str(scope_bin)}},
            which_side_effect=lambda c, **kw: None if c == "gone-binary" else c,
        )
        assert out["vendor-srv"]["command"] == str(scope_bin), (
            "the colliding scope entry did not supply the command, so alias matching "
            "would be over-broad -- revisit the ownership test"
        )
        assert DERIVED_KEY not in out["vendor-srv"], (
            "an alias-colliding scope entry that supplies the command must read as "
            "owning it, or the record would shadow a live declaration"
        )
