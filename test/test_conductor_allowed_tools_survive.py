"""The user's own ``allowedTools`` entries survive a conductor spec regeneration.

The conductor installers run on every ``rebuild_agent_config`` -- every gateway start
-- and an installer that rebuilt ``allowedTools`` from its grant tuple and wrote the
file without reading it would leave an entry the user approved on
``kirocrew-conductor.json`` (or the pipeline, security or ledger-alias spec) gone at
the next start, silently, while ``toolsSettings`` carried over. ``kirocrew.json``
keeps the user's list on an existing install; these tests pin that the four conductor
specs do too, the way that list does: the user's entries are kept, the shipped grants
are re-added, the governance ceiling still removes what it forbids, and nothing is
dropped -- or kept -- without a log line or an audit event naming it.

What tells a user's entry apart from Crew's own is the history of every grant any
release has shipped on the spec (``conductor_agents._SHIPPED_GRANT_HISTORY``): an
entry it names is Crew's and is kept exactly when this release ships it; an entry it
does not name is the user's and survives. Without it an add-only merge would keep
every grant an earlier release shipped and a later one deliberately stopped shipping
-- the goal conductor's core grant is named verbs where an earlier release's was a
bare ``@kirocrew-core``, and that narrowing has to reach existing installs. The one
cost is pinned too: a grant Crew once shipped and has since retired, hand-re-added by
a user, reads as Crew's and goes, loudly.

A spec that is present but unreadable is not "no spec", and what happens to it
follows the reader's two failure classes: bytes that can never be a spec are written
over (nothing loadable was there to keep, and leaving them would leave kiro-cli
without the conductor and the ceiling with nothing to re-filter), and so is a spec
whose inode carries a second hard link -- the one shape kiro-cli would load while the
hardened reader refuses it for good, so leaving it would put that list out of the
ceiling's reach; the write swaps the directory entry and the other name keeps its
bytes. A read that may succeed next time leaves the user's file exactly where it is
-- and the installer says it wrote nothing, as it does when its write raised, so a
moved ceiling stays pending instead of being marked projected onto a list that was
never rewritten. That hold is reported apart from the shared-home guard's refusal:
``kirocrew.json`` was written, and the dashboard's default-model applier must not
read a held conductor spec as a change that failed to land.
"""

from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from conftest import requires_symlinks
from kiro_crew import agent, agent_discovery
from kiro_crew.agent_files import (
    CONDUCTOR_AGENT_FILENAME,
    LEDGER_CONDUCTOR_AGENT_FILENAME,
    PIPELINE_CONDUCTOR_AGENT_FILENAME,
    SECURITY_CONDUCTOR_AGENT_FILENAME,
)
from kiro_crew.agent_materialization import conductor_agents
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.platform import governance

#: (spec name, filename, installer) for the three installers; the ledger alias shares
#: ``_conductor_spec`` with the goal conductor and gets its own test below.
_CONDUCTORS = [
    pytest.param(
        "kirocrew-conductor",
        CONDUCTOR_AGENT_FILENAME,
        "_install_conductor_agent",
        id="conductor",
    ),
    pytest.param(
        "kirocrew-pipeline-conductor",
        PIPELINE_CONDUCTOR_AGENT_FILENAME,
        "_install_pipeline_conductor_agent",
        id="pipeline",
    ),
    pytest.param(
        "kirocrew-security-conductor",
        SECURITY_CONDUCTOR_AGENT_FILENAME,
        "_install_security_conductor_agent",
        id="security",
    ),
]

#: Mounted on every conductor and auto-approved by none of them: the entry a user
#: approves ("stop asking me about web fetches") and expects to stay approved. A
#: network builtin, because the always-on PreToolUse floor refuses ``fs_read`` or
#: ``execute_bash`` on any host, ceiling or not, and the rig below is that host.
_USER_GRANT = "web_fetch"

#: A grant an earlier release shipped on the spec and this one does not -- the bare
#: ``@kirocrew-core`` on the goal conductor. The pipeline and security conductors
#: have retired nothing (the pipeline conductor's one-release work-ledger verbs are
#: shipped again as the conductor base); on them an earlier release's file holds
#: nothing the history can claim.
_RETIRED_GRANT: dict[str, str | None] = {
    "kirocrew-conductor": "@kirocrew-core",
    "kirocrew-pipeline-conductor": None,
    "kirocrew-security-conductor": None,
}

#: An entry every regeneration drops from any conductor spec: a wildcard, which the
#: gate cannot judge and so is never carried forward.
_DROPPED_BY_EVERY_REWRITE = "session_*"

#: Work verbs NO release ever shipped on the spec, though a sibling spec ships or
#: shipped them: ``work_report`` is the worker's verb on every conductor, and
#: ``work_brief`` reached the goal conductor only, never the pipeline conductor. On
#: these specs they are a user's entries and must survive -- a table that named
#: them as Crew's would drop a hand-added approval at every start.
_NEVER_SHIPPED_HERE = [
    pytest.param(
        "_install_conductor_agent",
        CONDUCTOR_AGENT_FILENAME,
        "@kirocrew-work/work_report",
        id="conductor-work_report",
    ),
    pytest.param(
        "_install_pipeline_conductor_agent",
        PIPELINE_CONDUCTOR_AGENT_FILENAME,
        "@kirocrew-work/work_report",
        id="pipeline-work_report",
    ),
    pytest.param(
        "_install_pipeline_conductor_agent",
        PIPELINE_CONDUCTOR_AGENT_FILENAME,
        "@kirocrew-work/work_brief",
        id="pipeline-work_brief",
    ),
]

#: What the history holds BEYOND each spec's current tuple: exactly the grants a
#: release is shown to have shipped there and a later one stopped shipping. Every
#: other entry is shipped today and needs no archaeology; an entry can join this
#: residue only by being added here with its provenance.
_VERIFIED_RETIREMENTS: dict[str, frozenset[str]] = {
    "kirocrew-conductor": frozenset({"@kirocrew-core"}),
    "kirocrew-ledger-conductor": frozenset(),
    "kirocrew-pipeline-conductor": frozenset(),
    "kirocrew-security-conductor": frozenset(),
}


@pytest.fixture
def agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private agents directory, an ungoverned ceiling, and an accepting kiro-cli."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )
    monkeypatch.setattr(
        agent,
        "build_agent_config",
        lambda: {
            "name": "kirocrew",
            "prompt": "file://x",
            "mcpServers": {
                "kirocrew-core": {"command": "/resolved/kirocrew", "args": ["mcp-core"]},
            },
            "tools": ["fs_write", "@kirocrew-core"],
            "allowedTools": ["@kirocrew-core"],
        },
    )
    monkeypatch.setattr(
        agent, "_kirocrew_mcp_invocation", lambda sub: ("/resolved/kirocrew", [sub])
    )
    # An ungoverned host as the gate sees it: no ceiling, the floor still on.
    monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: governance.may_skip_gate(ref, None))
    return tmp_path


def _read(agents_dir: Path, filename: str) -> dict:
    return json.loads((agents_dir / filename).read_text(encoding="utf-8"))


def _edit_allowed(agents_dir: Path, filename: str, mutate) -> None:
    """Edit the spec's ``allowedTools`` on disk the way a user does: by hand."""
    _edit_spec(agents_dir, filename, lambda data: mutate(data["allowedTools"]))


def _edit_spec(agents_dir: Path, filename: str, mutate) -> None:
    """Rewrite the spec on disk after *mutate* -- a hand edit, or another release's write."""
    path = agents_dir / filename
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class _SelRecorder:
    """Stands in for ``sel()``: records each audit call instead of writing it."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_api_access(self, **fields: Any) -> None:
        self.events.append(fields)


@pytest.fixture
def sel_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorder = _SelRecorder()
    monkeypatch.setattr(agent, "sel", lambda: recorder)
    return recorder.events


def _warnings(caplog: pytest.LogCaptureFixture, filename: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and filename in record.getMessage()
    ]


def _one_line(caplog: pytest.LogCaptureFixture, filename: str) -> str:
    lines = _warnings(caplog, filename)
    assert len(lines) == 1, f"expected ONE warning naming the spec, got {lines!r}"
    return lines[0]


def _events(sel_events: list[dict[str, Any]], operation: str) -> list[dict[str, Any]]:
    return [e for e in sel_events if e.get("operation") == operation]


def _shipped_tuple(monkeypatch: pytest.MonkeyPatch, installer: str) -> tuple[str, ...]:
    """Run *installer* and return the shipped tuple it handed to ``_governed_grants``,
    the grants as the installer ships them and before the ceiling touches them."""
    seen: list[tuple[str, ...]] = []
    real = conductor_agents._governed_grants

    def recording(shipped: tuple[str, ...], **kwargs: Any) -> list[str] | None:
        seen.append(tuple(shipped))
        return real(shipped, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(conductor_agents, "_governed_grants", recording)
        assert getattr(agent, installer)() is True
    assert len(seen) == 1, "one install, one shipped tuple"
    return seen[0]


@pytest.mark.parametrize(("name", "filename", "installer"), _CONDUCTORS)
class TestUserGrantsSurviveRegeneration:
    def test_a_user_added_grant_survives_and_the_shipped_grants_stay(
        self, agents_dir: Path, name: str, filename: str, installer: str
    ) -> None:
        """The reporter's case: approve a tool, restart, it is still approved."""
        install = getattr(agent, installer)
        assert install() is True
        shipped = _read(agents_dir, filename)["allowedTools"]
        assert _USER_GRANT in _read(agents_dir, filename)["tools"]
        assert _USER_GRANT not in shipped

        _edit_allowed(agents_dir, filename, lambda lst: lst.append(_USER_GRANT))
        assert install() is True

        # Shipped grants first, in their shipped order; the user's entry after them.
        assert _read(agents_dir, filename)["allowedTools"] == [*shipped, _USER_GRANT]

    def test_the_user_entry_is_still_there_after_a_second_regeneration(
        self, agents_dir: Path, name: str, filename: str, installer: str
    ) -> None:
        """Surviving one start is not the bar; every start is."""
        install = getattr(agent, installer)
        install()
        _edit_allowed(agents_dir, filename, lambda lst: lst.append(_USER_GRANT))
        install()
        install()
        assert _USER_GRANT in _read(agents_dir, filename)["allowedTools"]

    def test_a_shipped_grant_the_user_removed_comes_back(
        self, agents_dir: Path, name: str, filename: str, installer: str
    ) -> None:
        """The list is the shipped grants PLUS the user's, like ``kirocrew.json``'s
        managed refs: narrowing a conductor is the governance ceiling's job."""
        install = getattr(agent, installer)
        install()
        shipped = _read(agents_dir, filename)["allowedTools"]
        assert "report" in shipped

        _edit_allowed(agents_dir, filename, lambda lst: lst.remove("report"))
        install()

        assert _read(agents_dir, filename)["allowedTools"] == shipped

    def test_a_ceiling_forbidden_user_grant_is_removed_and_named(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """``allowedTools`` never reaches the PreToolUse gate, so a preserved entry
        passes the same ceiling the shipped grants do; the tool stays MOUNTED."""
        install = getattr(agent, installer)
        install()
        _edit_allowed(agents_dir, filename, lambda lst: lst.append("execute_bash"))
        monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: ref != "execute_bash")

        with caplog.at_level(logging.WARNING):
            install()

        data = _read(agents_dir, filename)
        assert "execute_bash" in data["tools"]
        assert "execute_bash" not in data["allowedTools"]
        assert "execute_bash" in _one_line(caplog, filename)

    @pytest.mark.parametrize(
        "pattern",
        [
            pytest.param("*", id="star"),
            pytest.param("fs_*", id="fs-glob"),
            pytest.param("execute_?ash", id="single-char"),
            pytest.param("[ef]*", id="char-class"),
            pytest.param("web_*", id="network-glob"),
            pytest.param("*fetch*", id="infix-glob"),
            pytest.param("session_*", id="mcp-verb-glob"),
            pytest.param("*_send", id="mcp-verb-suffix-glob"),
            pytest.param("@*", id="every-server"),
            pytest.param("@kirocrew-*", id="server-glob"),
            pytest.param("@other-server/do_*", id="server-scoped-verb-glob"),
        ],
    )
    def test_a_wildcard_is_never_carried_forward(
        self,
        agents_dir: Path,
        sel_events: list[dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
        name: str,
        filename: str,
        installer: str,
        pattern: str,
    ) -> None:
        """``may_skip_gate`` judges a ref by its exact NAME, so a pattern -- kiro-cli's
        ``*`` for every tool, ``fs_*``, ``session_*``, ``@*`` -- passes it as an
        unmapped name while kiro-cli expands it over every builtin AND every verb of
        every mounted MCP server, on the one path that never reaches the gate. The
        builtins could be enumerated; the MCP verbs cannot (a server's tools are
        discovered when kiro-cli connects, not at spec-write time), so no expansion
        can judge the pattern and none is attempted: every wildcard of the user's is
        dropped before the merge, whatever it would reach, the shipped grants and a
        plain user entry beside it survive, ONE warning names the pattern and the
        remedy (name tools exactly), and the SEL feed records the revocation under
        that reason."""
        install = getattr(agent, installer)
        install()
        _edit_allowed(agents_dir, filename, lambda lst: lst.extend([pattern, _USER_GRANT]))

        with caplog.at_level(logging.WARNING):
            install()

        allowed = _read(agents_dir, filename)["allowedTools"]
        assert pattern not in allowed, "a wildcard was carried forward"
        assert _USER_GRANT in allowed and "session" in allowed
        line = _one_line(caplog, filename)
        assert pattern in line and "cannot be judged" in line and "name tools exactly" in line
        revoked = _events(sel_events, "mcp_auto_approve_revoked")
        assert [e for e in revoked if pattern in e["resources"] and "wildcard" in e["resources"]]
        retained = _events(sel_events, "mcp_auto_approve_retained")
        assert all(
            pattern not in e["resources"] for e in retained
        ), "a dropped wildcard was audited as retained"

    @pytest.mark.parametrize(
        "pattern",
        [
            pytest.param("session_*", id="mcp-verb-glob"),
            pytest.param("*_send", id="mcp-verb-suffix-glob"),
            pytest.param("@*", id="every-server"),
            pytest.param("*fetch*", id="infix-glob"),
        ],
    )
    def test_a_wildcard_reaching_a_verb_the_mcp_ceiling_withholds_is_not_carried(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        name: str,
        filename: str,
        installer: str,
        pattern: str,
    ) -> None:
        """The case the expansion over builtin names could not see: a ceiling whose
        ``mcp`` rules withhold ``@kirocrew-dashboard/session_send`` by name withholds
        every exact ``@kirocrew-dashboard`` grant this rebuild offers -- and a kept
        ``session_*``, ``*_send`` or ``@*`` would hand the same verb back, auto-approved
        past that ceiling, because the pattern matches no builtin, names no server and
        so passes the gate as an unmapped name. The wildcard goes; the exact grants
        the ceiling withholds go too, as before; the user's exact entry survives."""
        install = getattr(agent, installer)
        install()
        _edit_allowed(agents_dir, filename, lambda lst: lst.extend([pattern, _USER_GRANT]))
        # The gate under such a ceiling, as the exact names see it: every ref on the
        # named server is withheld, every other exact name keeps its answer -- and a
        # pattern, in no table and naming no server, is passed.
        monkeypatch.setattr(
            agent,
            "_may_auto_approve",
            lambda ref: not ref.startswith("@kirocrew-dashboard")
            and governance.may_skip_gate(ref, None),
        )

        with caplog.at_level(logging.WARNING):
            install()

        allowed = _read(agents_dir, filename)["allowedTools"]
        assert pattern not in allowed, "a wildcard was carried past the MCP ceiling"
        assert not [ref for ref in allowed if ref.startswith("@kirocrew-dashboard")]
        assert _USER_GRANT in allowed and "session" in allowed
        line = _one_line(caplog, filename)
        assert pattern in line and "name tools exactly" in line

    @pytest.mark.parametrize(
        "entry",
        [
            pytest.param("@other-server", id="bare-server-ref"),
            pytest.param("@other-server/do_this", id="server-verb-ref"),
            pytest.param("web_search", id="exact-builtin-the-gate-passes"),
        ],
    )
    def test_an_exact_entry_is_the_users_and_is_judged_by_the_gate(
        self,
        agents_dir: Path,
        caplog: pytest.LogCaptureFixture,
        name: str,
        filename: str,
        installer: str,
        entry: str,
    ) -> None:
        """The rule is syntactic -- a pattern is never offered to the gate -- and only
        that: an exact ``@server`` or ``@server/verb`` ref and an exact builtin name
        the gate passes are the user's entries, judged by the gate as before, and
        stay."""
        install = getattr(agent, installer)
        install()
        _edit_allowed(agents_dir, filename, lambda lst: lst.append(entry))

        with caplog.at_level(logging.WARNING):
            install()

        assert entry in _read(agents_dir, filename)["allowedTools"]
        assert _warnings(caplog, filename) == []

    def test_no_expansion_of_a_wildcard_is_attempted(
        self, name: str, filename: str, installer: str
    ) -> None:
        """What a pattern reaches cannot be enumerated at spec-write time (the MCP verbs
        are discovered when kiro-cli connects), so the module keeps no table of
        governed builtins and no glob expansion to judge one by: the rule is the
        characters alone."""
        assert not hasattr(conductor_agents, "_GOVERNED_BUILTINS")
        assert not hasattr(conductor_agents, "_builtins_a_wildcard_may_not_skip_for")
        assert conductor_agents._is_wildcard("session_*") is True
        assert conductor_agents._is_wildcard("@kirocrew-dashboard/session_send") is False

    def test_a_retired_grant_goes_and_the_users_entry_stays(
        self,
        agents_dir: Path,
        caplog: pytest.LogCaptureFixture,
        sel_events: list[dict[str, Any]],
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """The narrowing shape: a spec written by an earlier release carries a grant
        that release shipped and this one does not -- the bare ``@kirocrew-core``
        where this release ships named verbs -- beside an entry the user added. The
        history of every grant any release shipped tells them apart: the retired
        grant is Crew's and goes, named and audited as a revoked auto-approval, and
        the user's entry, which no release ever shipped, stays. Not a reset. On a
        spec that has retired nothing, the user's entry simply stays."""
        install = getattr(agent, installer)
        install()
        shipped = _read(agents_dir, filename)["allowedTools"]
        retired = _RETIRED_GRANT[name]
        assert retired not in shipped
        _edit_allowed(
            agents_dir,
            filename,
            lambda lst: lst.extend([*([retired] if retired else []), _USER_GRANT]),
        )

        with caplog.at_level(logging.WARNING):
            install()

        assert _read(agents_dir, filename)["allowedTools"] == [*shipped, _USER_GRANT]
        revoked = _events(sel_events, "mcp_auto_approve_revoked")
        if retired is None:
            assert not _warnings(caplog, filename) and not revoked
        else:
            line = _one_line(caplog, filename)
            assert retired in line and _USER_GRANT not in line
            assert len(revoked) == 1 and retired in revoked[0]["resources"]
            assert _USER_GRANT not in revoked[0]["resources"]

    def test_every_grant_the_installer_writes_is_in_the_history(
        self, agents_dir: Path, name: str, filename: str, installer: str
    ) -> None:
        """The history is literal so a retired grant stays named; this pin is what
        keeps it from falling behind: a grant added to a conductor tuple must be
        listed there before it ships, or an existing spec would read it as the
        user's at the next start."""
        getattr(agent, installer)()
        written = set(_read(agents_dir, filename)["allowedTools"])
        assert written <= conductor_agents._SHIPPED_GRANT_HISTORY[name]

    def test_every_grant_the_current_tuple_ships_is_in_the_history(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """The same pin on the tuple ITSELF, read as the installer hands it to
        ``_governed_grants`` and before any ceiling touches it, so a grant the
        ceiling happened to withhold on the test host could not hide from the
        check: current ⊆ history, which is what lets a future retirement need no
        step -- the retired grant is already named."""
        shipped = _shipped_tuple(monkeypatch, installer)
        assert set(shipped) <= conductor_agents._SHIPPED_GRANT_HISTORY[name]
        # Under the accepting ceiling of this rig the written list IS the tuple.
        assert _read(agents_dir, filename)["allowedTools"] == list(shipped)

    def test_the_history_pin_bites_when_a_shipped_grant_is_missing_from_it(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """Red-first for the pin above, by construction: with one shipped grant
        taken out of the history, the subset check fails -- so a grant that ships
        unlisted cannot pass it."""
        history = conductor_agents._SHIPPED_GRANT_HISTORY[name]
        assert "report" in history
        monkeypatch.setitem(conductor_agents._SHIPPED_GRANT_HISTORY, name, history - {"report"})
        shipped = _shipped_tuple(monkeypatch, installer)
        assert "report" in shipped
        assert not set(shipped) <= conductor_agents._SHIPPED_GRANT_HISTORY[name]

    def test_a_user_entry_crew_once_shipped_reads_as_crews_and_goes(
        self,
        agents_dir: Path,
        caplog: pytest.LogCaptureFixture,
        sel_events: list[dict[str, Any]],
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """The one cost of a history with no per-install record: a user who hand-adds
        a grant Crew once shipped and has since retired cannot be told from that
        release's own output, so the entry goes -- loudly, as a revoked
        auto-approval, and the tool it covered asks instead. The same grant on a
        spec that never shipped it is the user's and stays."""
        install = getattr(agent, installer)
        install()
        once_shipped = _RETIRED_GRANT[name] or "@kirocrew-core"
        assert once_shipped not in _read(agents_dir, filename)["allowedTools"]
        _edit_allowed(agents_dir, filename, lambda lst: lst.append(once_shipped))

        with caplog.at_level(logging.WARNING):
            install()

        kept = once_shipped in _read(agents_dir, filename)["allowedTools"]
        assert kept is (_RETIRED_GRANT[name] is None)
        if not kept:
            assert once_shipped in _one_line(caplog, filename)
            assert once_shipped in _events(sel_events, "mcp_auto_approve_revoked")[0]["resources"]

    def test_a_retained_user_entry_is_audited(
        self,
        agents_dir: Path,
        sel_events: list[dict[str, Any]],
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """Keeping an auto-approval past a rebuild is a permission decision, so it
        lands in the SEL feed beside the withholds and revocations: one event per
        regeneration naming the user's entries and the spec, none when the user
        added nothing, and never for a shipped or a retired grant."""
        install = getattr(agent, installer)
        install()
        assert not _events(sel_events, "mcp_auto_approve_retained")
        retired = _RETIRED_GRANT[name]
        _edit_allowed(
            agents_dir,
            filename,
            lambda lst: lst.extend([_USER_GRANT, *([retired] if retired else [])]),
        )

        install()

        retained = _events(sel_events, "mcp_auto_approve_retained")
        assert len(retained) == 1
        assert _USER_GRANT in retained[0]["resources"] and filename in retained[0]["resources"]
        assert "report" not in retained[0]["resources"]
        if retired:
            assert retired not in retained[0]["resources"]
        assert retained[0]["caller"] == "system" and retained[0]["source"] == installer

    def test_the_audit_never_breaks_the_install(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """Best-effort: a SEL that raises costs the audit line, not the spec."""

        def broken() -> Any:
            raise RuntimeError("no audit log here")

        monkeypatch.setattr(agent, "sel", broken)
        install = getattr(agent, installer)
        install()
        _edit_allowed(agents_dir, filename, lambda lst: lst.append(_USER_GRANT))
        assert install() is True
        assert _USER_GRANT in _read(agents_dir, filename)["allowedTools"]

    def test_nothing_is_logged_when_nothing_is_dropped(
        self,
        agents_dir: Path,
        caplog: pytest.LogCaptureFixture,
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        install = getattr(agent, installer)
        install()
        _edit_allowed(agents_dir, filename, lambda lst: lst.append(_USER_GRANT))
        with caplog.at_level(logging.WARNING):
            install()
        assert not _warnings(caplog, filename)

    def test_a_clean_install_resets_the_list(
        self, agents_dir: Path, name: str, filename: str, installer: str
    ) -> None:
        """``kirocrew setup --agent-only --clean`` is the explicit reset, and it resets
        the conductor specs as it resets ``kirocrew.json``."""
        install = getattr(agent, installer)
        install()
        shipped = _read(agents_dir, filename)["allowedTools"]
        _edit_allowed(agents_dir, filename, lambda lst: lst.append(_USER_GRANT))
        assert install(clean=True) is True
        assert _read(agents_dir, filename)["allowedTools"] == shipped

    def test_a_malformed_list_on_disk_preserves_only_its_string_entries(
        self, agents_dir: Path, name: str, filename: str, installer: str
    ) -> None:
        """A hand-edited ``allowedTools: [1, "web_fetch"]`` keeps the ref and drops the
        number: a non-string is not a tool ref and would crash the ceiling predicate."""
        install = getattr(agent, installer)
        install()
        _edit_allowed(agents_dir, filename, lambda lst: lst.extend([1, _USER_GRANT]))
        install()
        after = _read(agents_dir, filename)["allowedTools"]
        assert _USER_GRANT in after
        assert all(isinstance(ref, str) for ref in after)

    def test_kas_permissions_follow_the_merged_list(
        self, agents_dir: Path, name: str, filename: str, installer: str
    ) -> None:
        """The KAS block is derived from the list that is written, user entry included,
        so an approval that survives on kiro-cli survives on KAS too."""
        install = getattr(agent, installer)
        install()
        user_grant = "@kirocrew-core/knowledge_list_sources"
        assert user_grant not in _read(agents_dir, filename)["allowedTools"]
        _edit_allowed(agents_dir, filename, lambda lst: lst.append(user_grant))
        install()
        rules = _read(agents_dir, filename)["permissions"]["rules"]
        matches = [m for rule in rules for m in (rule.get("match") or [])]
        assert "kirocrew-core/knowledge_list_sources" in matches

    def test_removing_a_shipped_grant_by_hand_brings_it_back_and_keeps_the_users_entry(
        self,
        agents_dir: Path,
        caplog: pytest.LogCaptureFixture,
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """A shipped grant deleted by hand comes back -- narrowing a conductor is the
        ceiling's job -- and the user's entry, which no release shipped, stays.
        Nothing is dropped, so nothing is logged."""
        install = getattr(agent, installer)
        install()
        shipped = _read(agents_dir, filename)["allowedTools"]
        _edit_allowed(
            agents_dir, filename, lambda lst: (lst.remove("report"), lst.append(_USER_GRANT))
        )
        with caplog.at_level(logging.WARNING):
            install()
        assert _read(agents_dir, filename)["allowedTools"] == [*shipped, _USER_GRANT]
        assert not _warnings(caplog, filename)

    def test_a_shipped_grant_the_ceiling_withheld_returns_when_the_ceiling_allows(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """A ceiling that moves between two starts: a shipped grant it withheld is on
        neither the file nor the history's "retired" side, so when the ceiling
        allows it again it is simply shipped again, beside the user's entry."""
        install = getattr(agent, installer)
        monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: ref != "report")
        install()
        assert "report" not in _read(agents_dir, filename)["allowedTools"]
        _edit_allowed(agents_dir, filename, lambda lst: lst.append(_USER_GRANT))
        monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: True)
        install()
        after = _read(agents_dir, filename)["allowedTools"]
        assert "report" in after and _USER_GRANT in after

    # ── a spec that is present and unreadable is not "no spec" ────────────────

    @pytest.mark.parametrize(
        "breakage",
        [
            pytest.param("json", id="malformed-json"),
            pytest.param("object", id="not-an-object"),
            pytest.param("list", id="allowedTools-not-a-list"),
            pytest.param("cap", id="over-the-read-cap"),
        ],
    )
    def test_bytes_that_can_never_be_a_spec_are_written_over_and_named(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        name: str,
        filename: str,
        installer: str,
        breakage: str,
    ) -> None:
        """A hand edit gone wrong -- a JSON typo in the edit the docs invite, a list
        that is not a list -- leaves bytes kiro-cli cannot load either: nothing
        loadable is preserved by keeping them, a conductor dispatched by name would
        resolve to the default agent instead, and a tightened ceiling could never be
        re-filtered onto the list. So a loadable spec is written over them, the
        install reports that it wrote, and ONE warning names the cause and that no
        entry on the file was carried forward."""
        install = getattr(agent, installer)
        install()
        _edit_allowed(agents_dir, filename, lambda lst: lst.append(_USER_GRANT))
        path = agents_dir / filename
        repaired = path.read_text(encoding="utf-8")
        if breakage == "json":
            path.write_text(repaired.rstrip().rstrip("}") + ",\n}", encoding="utf-8")
        elif breakage == "object":
            path.write_text(json.dumps([json.loads(repaired)]), encoding="utf-8")
        elif breakage == "list":
            _edit_spec(agents_dir, filename, lambda data: data.update(allowedTools=_USER_GRANT))

        with caplog.at_level(logging.WARNING), monkeypatch.context() as scoped:
            if breakage == "cap":
                scoped.setattr("kiro_crew.hooks.MAX_FILE_BYTES", 64)
            wrote = install()

        data = _read(agents_dir, filename)
        assert data["name"] == name, "a loadable spec was not put back"
        assert isinstance(data["allowedTools"], list) and data["allowedTools"]
        assert _USER_GRANT not in data["allowedTools"]
        line = _one_line(caplog, filename)
        assert "could not be read" in line and "written afresh" in line
        assert wrote is True

    def test_a_read_that_may_succeed_later_leaves_the_spec_in_place(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """A read that failed for a reason the reader's own contract calls transient
        -- an I/O error -- may stand before the user's intact spec. It is left
        exactly where it is, the install reports that it did NOT write (so a moved
        ceiling stays pending), one warning names the cause and the way out, and
        once the read succeeds again the entries on it are carried forward."""
        install = getattr(agent, installer)
        install()
        _edit_allowed(agents_dir, filename, lambda lst: lst.append(_USER_GRANT))
        path = agents_dir / filename
        intact = path.read_bytes()

        def read_fails(real: Path) -> bytes:
            raise OSError(errno.EIO, "Input/output error", str(real))

        with caplog.at_level(logging.WARNING), monkeypatch.context() as scoped:
            scoped.setattr(agent_discovery, "_read_spec_bytes", read_fails)
            assert install() is False

        assert path.read_bytes() == intact, "the spec was replaced on a transient failure"
        line = _one_line(caplog, filename)
        assert "could not be read" in line and "left in place" in line and "--clean" in line

        assert install() is True
        assert _USER_GRANT in _read(agents_dir, filename)["allowedTools"]

    def test_a_read_that_fails_once_is_retried_and_the_spec_is_rewritten(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """A transient failure is what a second read a moment later clears, and a hold
        on the first one leaves the previous ceiling's list on disk until something
        retries the whole rebuild -- on a standalone host, an hour later. So the read
        is attempted again, after a bounded pause, before the spec is held: an I/O
        error on the first read and a clean second one is one install that writes,
        carries the user's entry, warns about nothing, and paused once."""
        install = getattr(agent, installer)
        install()
        _edit_allowed(agents_dir, filename, lambda lst: lst.append(_USER_GRANT))
        real_read = agent_discovery._read_spec_bytes
        failures = {"left": 1}
        pauses: list[float] = []

        def read_fails_once(real: Path) -> bytes:
            if failures["left"]:
                failures["left"] -= 1
                raise OSError(errno.EIO, "Input/output error", str(real))
            return real_read(real)

        with caplog.at_level(logging.WARNING), monkeypatch.context() as scoped:
            scoped.setattr(agent_discovery, "_read_spec_bytes", read_fails_once)
            scoped.setattr(conductor_agents, "_sleep", pauses.append, raising=False)
            wrote = install()

        assert wrote is True, "a read that failed once held the spec instead of retrying"
        assert _USER_GRANT in _read(agents_dir, filename)["allowedTools"]
        assert pauses == [conductor_agents._TRANSIENT_READ_BACKOFF_SECS[0]]
        assert _warnings(caplog, filename) == []

    def test_a_read_that_keeps_failing_is_held_after_the_bounded_attempts(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """The retry is bounded -- the installers run inside the boot rebuild -- so a
        failure that outlasts every attempt is held exactly as before: the file
        untouched, the install reporting that it did not write, one warning that
        names the attempts. The pauses are the declared backoff, and a path shape
        is never retried (a re-read changes nothing about a link)."""
        install = getattr(agent, installer)
        install()
        path = agents_dir / filename
        intact = path.read_bytes()
        reads: list[Path] = []
        pauses: list[float] = []

        def read_fails(real: Path) -> bytes:
            reads.append(real)
            raise OSError(errno.ESTALE, "Stale file handle", str(real))

        with caplog.at_level(logging.WARNING), monkeypatch.context() as scoped:
            scoped.setattr(agent_discovery, "_read_spec_bytes", read_fails)
            scoped.setattr(conductor_agents, "_sleep", pauses.append, raising=False)
            assert install() is False

        assert path.read_bytes() == intact, "the spec was replaced on a transient failure"
        assert len(reads) == 3, f"the read was attempted {len(reads)} time(s) before the hold"
        assert conductor_agents._TRANSIENT_READ_ATTEMPTS == 3
        assert pauses == list(conductor_agents._TRANSIENT_READ_BACKOFF_SECS)
        line = _one_line(caplog, filename)
        assert "3 reads failed" in line and "left in place" in line

        # A shape is decided before any read and written over at once: no pause.
        pauses.clear()
        twin = agents_dir / f"{filename}.twin"
        os.link(path, twin)
        with monkeypatch.context() as scoped:
            scoped.setattr(conductor_agents, "_sleep", pauses.append, raising=False)
            assert install() is True
        assert pauses == [] and os.lstat(path).st_nlink == 1

    def test_a_path_probe_that_fails_leaves_the_spec_in_place(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """The link-shape fence probes the path's metadata before any byte is read. A
        probe that itself fails -- a stale NFS handle, an I/O error on the directory --
        says nothing about the shape, and the bytes behind the name may be the user's
        intact spec; it is a hold like every other read that may succeed next time,
        never a verdict that writes the file over. (``_spec_path_is_safe`` still
        answers False to the same failure for the callers that only need permission
        to rewrite in place.)"""
        install = getattr(agent, installer)
        install()
        _edit_allowed(agents_dir, filename, lambda lst: lst.append(_USER_GRANT))
        path = agents_dir / filename
        intact = path.read_bytes()
        real_stat = os.stat

        def probe_fails(target: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            # ``Path.is_symlink`` probes through ``os.stat(follow_symlinks=False)``;
            # only that probe fails here, so a rewrite that followed would go through
            # and show as the replaced bytes it is.
            no_follow = kwargs.get("follow_symlinks", True) is False
            if (
                no_follow
                and isinstance(target, (str, os.PathLike))
                and Path(target).name == filename
            ):
                raise OSError(errno.ESTALE, "Stale file handle", str(target))
            return real_stat(target, *args, **kwargs)

        with caplog.at_level(logging.WARNING), monkeypatch.context() as scoped:
            scoped.setattr(os, "stat", probe_fails)
            agents = agent.kiro_agents_dir_path()
            fence = agent._spec_path_is_safe(path, agents)
            wrote = install()

        assert path.read_bytes() == intact, "the spec was replaced on a failed path probe"
        assert wrote is False, "the installer did not report the hold"
        assert fence is False, "the bool fence must still fail closed on the same failure"
        line = _one_line(caplog, filename)
        assert "metadata could not be read" in line and "Stale file handle" in line
        assert "left in place" in line

        assert install() is True
        assert _USER_GRANT in _read(agents_dir, filename)["allowedTools"]

    def test_a_second_hard_link_is_written_afresh_and_the_ceiling_reaches_the_spec(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """A second hard link on the spec's inode is the one unreadable shape kiro-cli
        WOULD load: the hardened reader refuses a multiply-linked inode for good,
        kiro-cli reads the same bytes without that fence, so a spec left in place
        for that reason is a list a tightened ceiling never reaches. It is written
        afresh instead -- the install reports that it wrote, the grant the ceiling
        now denies is gone from the file kiro-cli loads, ONE warning names the
        link and that no entry was carried -- and the write swaps the directory
        entry, so the other name keeps its bytes and nothing is written through
        the shared inode."""
        install = getattr(agent, installer)
        install()
        _edit_allowed(agents_dir, filename, lambda lst: lst.extend([_USER_GRANT, "execute_bash"]))
        path = agents_dir / filename
        twin = agents_dir / "twin.json"
        try:
            os.link(path, twin)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"hard links unavailable here: {exc}")
        shared_bytes = twin.read_bytes()
        shared_inode = os.stat(twin).st_ino
        monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: ref != "execute_bash")

        with caplog.at_level(logging.WARNING):
            wrote = install()

        data = _read(agents_dir, filename)
        assert "execute_bash" not in data["allowedTools"], "the ceiling did not reach the spec"
        assert data["name"] == name, "a loadable spec was not put back"
        assert _USER_GRANT not in data["allowedTools"], "an unreadable entry was carried"
        assert os.stat(path).st_nlink == 1 and os.stat(path).st_ino != shared_inode
        assert twin.read_bytes() == shared_bytes, "the write went through the shared inode"
        assert os.stat(twin).st_nlink == 1
        line = _one_line(caplog, filename)
        assert "hard link" in line and "written afresh" in line
        assert wrote is True

    def test_a_clean_install_replaces_an_unreadable_spec(
        self, agents_dir: Path, name: str, filename: str, installer: str
    ) -> None:
        """``kirocrew setup --agent-only --clean`` is the documented way out: it reads
        nothing, so it rebuilds the broken file."""
        install = getattr(agent, installer)
        install()
        path = agents_dir / filename
        path.write_text("{not json", encoding="utf-8")
        assert install(clean=True) is True
        assert _read(agents_dir, filename)["name"] == name

    @requires_symlinks
    def test_a_link_at_the_specs_name_is_replaced_and_nothing_behind_it_is_carried(
        self,
        agents_dir: Path,
        caplog: pytest.LogCaptureFixture,
        name: str,
        filename: str,
        installer: str,
    ) -> None:
        """The read-then-write-back fence every spec rewriter applies: a link at the
        spec's name is not followed into a copy-out of its target's entries. It is
        written over with a regular file -- what puts a loadable spec back -- and
        the warning says nothing on it was carried."""
        install = getattr(agent, installer)
        install()
        path = agents_dir / filename
        target = agents_dir / "elsewhere.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["allowedTools"].append(_USER_GRANT)
        target.write_text(json.dumps(data), encoding="utf-8")
        path.unlink()
        os.symlink(target, path)

        with caplog.at_level(logging.WARNING):
            assert install() is True

        assert not path.is_symlink()
        assert _USER_GRANT not in _read(agents_dir, filename)["allowedTools"]
        line = _one_line(caplog, filename)
        assert "written afresh" in line
        assert json.loads(target.read_text(encoding="utf-8")) == data


class TestLedgerAliasPreservesUnderItsOwnName:
    def test_the_alias_keeps_its_own_user_entries(self, agents_dir: Path) -> None:
        """The alias emits the goal conductor's spec under the old name, so a user's
        entry on the alias file is kept on the alias file."""
        assert agent._install_ledger_conductor_agent() is True
        shipped = _read(agents_dir, LEDGER_CONDUCTOR_AGENT_FILENAME)["allowedTools"]
        _edit_allowed(
            agents_dir, LEDGER_CONDUCTOR_AGENT_FILENAME, lambda lst: lst.append(_USER_GRANT)
        )
        agent._install_ledger_conductor_agent()
        assert _read(agents_dir, LEDGER_CONDUCTOR_AGENT_FILENAME)["allowedTools"] == [
            *shipped,
            _USER_GRANT,
        ]
        # The goal conductor's own file was never written.
        assert not (agents_dir / CONDUCTOR_AGENT_FILENAME).exists()

    def test_the_alias_tuple_is_in_its_own_history(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shipped = _shipped_tuple(monkeypatch, "_install_ledger_conductor_agent")
        assert set(shipped) <= conductor_agents._SHIPPED_GRANT_HISTORY["kirocrew-ledger-conductor"]


class TestTheHistoryClaimsOnlyWhatAReleaseShipped:
    """The table decides whose an entry is, so an entry it names that no release
    ever shipped on that spec is a user's approval dropped at every start -- the
    bug this change exists to fix, re-introduced for that one grant."""

    @pytest.mark.parametrize(("installer", "filename", "grant"), _NEVER_SHIPPED_HERE)
    def test_a_work_verb_no_release_shipped_on_this_spec_is_the_users(
        self,
        agents_dir: Path,
        caplog: pytest.LogCaptureFixture,
        installer: str,
        filename: str,
        grant: str,
    ) -> None:
        install = getattr(agent, installer)
        install()
        _edit_allowed(agents_dir, filename, lambda lst: lst.append(grant))

        with caplog.at_level(logging.WARNING):
            install()

        assert (
            grant in _read(agents_dir, filename)["allowedTools"]
        ), "a grant no release shipped here was read as Crew's and dropped"
        assert _warnings(caplog, filename) == []

    @pytest.mark.parametrize(
        ("name", "installer"),
        [
            pytest.param("kirocrew-conductor", "_install_conductor_agent", id="conductor"),
            pytest.param(
                "kirocrew-ledger-conductor", "_install_ledger_conductor_agent", id="ledger"
            ),
            pytest.param(
                "kirocrew-pipeline-conductor", "_install_pipeline_conductor_agent", id="pipeline"
            ),
            pytest.param(
                "kirocrew-security-conductor", "_install_security_conductor_agent", id="security"
            ),
        ],
    )
    def test_the_history_beyond_the_current_tuple_is_exactly_the_verified_retirements(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch, name: str, installer: str
    ) -> None:
        """Every history entry the spec ships today needs no archaeology; what is
        left is the residue a reader has to take on the table's word, so it is
        pinned to the retirements the installers' commit history shows."""
        shipped = set(_shipped_tuple(monkeypatch, installer))
        residue = conductor_agents._SHIPPED_GRANT_HISTORY[name] - shipped
        assert residue == _VERIFIED_RETIREMENTS[name], (
            f"{name}: the history names {sorted(residue - _VERIFIED_RETIREMENTS[name])} "
            "beyond the current tuple without a verified retirement behind it"
        )


class TestASpecLeftInPlaceHoldsTheCeilingMemo:
    """``reproject_for_ceiling_change`` advances its memo only when the rebuild wrote
    AND no conductor spec was held: a conductor spec left on disk unwritten -- a read
    that may clear later, or an installer whose write raised -- is an ``allowedTools``
    list the rebuild did not re-derive, so the rebuild must report the hold -- else
    a tightened ceiling would be marked projected onto a list that still carries the
    old grants, and the retry every later poll would otherwise give is lost for the
    process. The hold is NOT the shared-home guard's refusal: ``kirocrew.json`` was
    written, so ``wrote`` stays ``True`` and the dashboard's default-model applier,
    which raises on ``wrote=False``, does not announce a landed change as failed."""

    @staticmethod
    def _shared_home_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """The real rebuild under a relocated home whose agents dir reads as SHARED,
        so the guard stands aside and the installers' own verdicts decide the hold.
        The whole home is redirected to tmp and the resolved-home memo reset, as
        ``test_agent_home_isolation``'s module fixtures do, so the operator's real
        default home is never resolved (conftest's ratchet fails a test that does)."""
        from test_agent_home_isolation import _durable_checkout, _pretend_target_is_shared

        from kiro_crew.config import paths

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
        own_home = tmp_path / "relocated-home"
        own_home.mkdir()
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.setenv("KIROCREW_HOME", str(own_home))
        _durable_checkout(monkeypatch, agent)
        shared = tmp_path / "agents"
        _pretend_target_is_shared(monkeypatch, agent, shared)
        # The real rebuild below writes the hold memo; restored after the test.
        monkeypatch.setattr(agent, "_conductor_spec_held", False, raising=False)
        return shared

    @staticmethod
    def _pending_memo(monkeypatch: pytest.MonkeyPatch) -> None:
        """Arm the real hook: no generation projected yet, no pending warning issued,
        no hold carried in from an earlier rebuild."""
        monkeypatch.setattr(agent, "_projected_ceiling_generation", None, raising=False)
        monkeypatch.setattr(agent, "_pending_projection_warned_generation", None, raising=False)
        monkeypatch.setattr(agent, "_conductor_spec_held", False, raising=False)

    @staticmethod
    def _report() -> tuple[bool, list[bool]]:
        """``(wrote, held_out)`` from one reporting rebuild."""
        held: list[bool] = []
        _reported, wrote = agent.rebuild_agent_config_reporting(_held_out=held)
        return wrote, held

    def test_a_conductor_spec_left_in_place_is_a_hold_and_not_a_refusal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        shared = self._shared_home_rig(monkeypatch, tmp_path)
        assert self._report() == (True, [False]), "a landed rebuild writes and holds nothing"
        pipeline = shared / PIPELINE_CONDUCTOR_AGENT_FILENAME
        before = pipeline.read_bytes()

        real = conductor_agents._spec_to_replace

        def pipeline_read_fails(path: Path) -> dict[str, Any] | None:
            if path.name == PIPELINE_CONDUCTOR_AGENT_FILENAME:
                raise conductor_agents._SpecUnusable("Input/output error", replace=False)
            return real(path)

        with monkeypatch.context() as scoped:
            scoped.setattr(conductor_agents, "_spec_to_replace", pipeline_read_fails)
            # The dashboard's applier calls it bare: a held spec must not read as refused.
            reported, wrote = agent.rebuild_agent_config_reporting()
            wrote_again, held = self._report()

        assert reported == shared / agent.AGENT_FILENAME
        assert wrote is True and wrote_again is True, "a held spec was reported as a refusal"
        assert held == [True], "a spec left unwritten must hold the ceiling memo"
        assert pipeline.read_bytes() == before
        # The other specs were still rewritten; once the read clears, so is this one.
        assert self._report() == (True, [False])

    def test_an_installer_whose_write_raises_holds_the_memo(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failed WRITE leaves the previous list on disk exactly as a refused read
        does -- a read-only file, a refused ``os.replace`` -- and the rebuild logs
        the installer's exception rather than propagating it. The hold must still
        be reported, and the real hook must leave the memo pending and say so."""
        shared = self._shared_home_rig(monkeypatch, tmp_path)
        agent.rebuild_agent_config()
        security = shared / SECURITY_CONDUCTOR_AGENT_FILENAME
        before = security.read_bytes()
        real_write = agent._atomic_json_write

        def security_write_fails(path: Path, data: Any) -> None:
            if path.name == SECURITY_CONDUCTOR_AGENT_FILENAME:
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            real_write(path, data)

        with monkeypatch.context() as scoped:
            scoped.setattr(agent, "_atomic_json_write", security_write_fails)
            wrote, held = self._report()
            self._pending_memo(scoped)
            with caplog.at_level(logging.WARNING):
                agent.reproject_for_ceiling_change()
            pending = agent._projected_ceiling_generation

        assert security.read_bytes() == before
        assert wrote is True, "kirocrew.json was written; this is not a refusal"
        assert held == [True], "an installer that raised must hold the ceiling memo"
        assert pending is None, "the memo advanced over a spec that was never rewritten"
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("pending" in w and "read or written" in w for w in warnings), warnings

    def test_the_hook_advances_only_when_the_rebuild_wrote_and_held_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hook reads both signals: a refusal OR a hold leaves the memo behind;
        only a rebuild that wrote and held nothing advances it."""
        from kiro_crew.platform.context import governance_generation

        outcomes = iter([(False, False), (True, True), (True, False)])

        def fake_reporting(**kw: Any) -> tuple[Path, bool]:
            wrote, held = next(outcomes)
            kw["_held_out"].append(held)
            return Path("/shared/agents/kirocrew.json"), wrote

        monkeypatch.setattr(agent, "rebuild_agent_config_reporting", fake_reporting)
        self._pending_memo(monkeypatch)
        agent.reproject_for_ceiling_change()
        assert agent._projected_ceiling_generation is None, "a refusal advanced the memo"
        agent.reproject_for_ceiling_change()
        assert agent._projected_ceiling_generation is None, "a held spec advanced the memo"
        agent.reproject_for_ceiling_change()
        assert agent._projected_ceiling_generation == governance_generation()

    def test_a_spec_held_at_boot_is_retried_by_the_first_poll(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Boot rebuilds, THEN seeds the ceiling baseline, and nothing on the boot path
        reads the hold: a conductor spec the boot rebuild left unwritten still carries
        the list the previous ceiling wrote, yet a seeded baseline says the current
        generation is projected, so the hook skips every poll until the ceiling moves
        again -- the list is never re-derived for the process lifetime. The baseline
        must stay unseeded while the last rebuild held, so the first poll retries; it
        advances only once the spec is rewritten."""
        from kiro_crew.platform.context import governance_generation

        shared = self._shared_home_rig(monkeypatch, tmp_path)
        agent.rebuild_agent_config()
        pipeline = shared / PIPELINE_CONDUCTOR_AGENT_FILENAME
        # A list the rewrite narrows: a wildcard entry, which no rewrite carries.
        retired = _DROPPED_BY_EVERY_REWRITE
        _edit_allowed(shared, PIPELINE_CONDUCTOR_AGENT_FILENAME, lambda lst: lst.append(retired))
        real = conductor_agents._spec_to_replace
        unreadable = {"value": True}

        def pipeline_read_fails(path: Path) -> dict[str, Any] | None:
            if unreadable["value"] and path.name == PIPELINE_CONDUCTOR_AGENT_FILENAME:
                raise conductor_agents._SpecUnusable("Input/output error", replace=False)
            return real(path)

        monkeypatch.setattr(conductor_agents, "_spec_to_replace", pipeline_read_fails)
        self._pending_memo(monkeypatch)
        # The boot path: the bare rebuild, then the baseline seeded before the poller.
        agent.rebuild_agent_config()
        assert retired in json.loads(pipeline.read_text(encoding="utf-8"))["allowedTools"]
        with caplog.at_level(logging.WARNING):
            agent.prime_ceiling_projection()
        assert (
            agent._projected_ceiling_generation is None
        ), "boot seeded the baseline over a conductor spec it never rewrote"
        assert any("unseeded" in r.getMessage() for r in caplog.records)

        # The first poll, the ceiling unchanged: the rebuild runs again and still holds.
        agent.reproject_for_ceiling_change()
        assert agent._projected_ceiling_generation is None
        assert retired in json.loads(pipeline.read_text(encoding="utf-8"))["allowedTools"]

        # The read clears: the next poll rewrites the spec, and only then does the
        # memo say the generation is projected.
        unreadable["value"] = False
        agent.reproject_for_ceiling_change()
        assert agent._projected_ceiling_generation == governance_generation()
        assert retired not in json.loads(pipeline.read_text(encoding="utf-8"))["allowedTools"]
        # And a boot whose rebuild held nothing seeds the baseline as before.
        agent.prime_ceiling_projection()
        assert agent._projected_ceiling_generation == governance_generation()

    def test_a_spec_held_at_boot_is_rewritten_by_the_maintenance_wake_without_a_poll(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A host with no central distribution starts no poller, so the hook the
        previous test relies on never runs there: the ceiling changes at boot alone,
        and a conductor spec THAT boot's rebuild held would keep the list the previous
        ceiling wrote for the process lifetime. The gateway's hourly maintenance wake
        retries it: a no-op while nothing is held, one reporting rebuild when
        something is, the hold standing while the failure does, and the memo clearing
        only once every spec is written."""
        from kiro_crew.platform import policy_distribution

        shared = self._shared_home_rig(monkeypatch, tmp_path)
        agent.rebuild_agent_config()
        pipeline = shared / PIPELINE_CONDUCTOR_AGENT_FILENAME
        retired = _DROPPED_BY_EVERY_REWRITE
        _edit_allowed(shared, PIPELINE_CONDUCTOR_AGENT_FILENAME, lambda lst: lst.append(retired))
        real = conductor_agents._spec_to_replace
        unreadable = {"value": True}

        def pipeline_read_fails(path: Path) -> dict[str, Any] | None:
            if unreadable["value"] and path.name == PIPELINE_CONDUCTOR_AGENT_FILENAME:
                raise conductor_agents._SpecUnusable("Input/output error", replace=False)
            return real(path)

        monkeypatch.setattr(conductor_agents, "_spec_to_replace", pipeline_read_fails)
        self._pending_memo(monkeypatch)
        # Boot on a standalone host: the rebuild holds, the baseline stays unseeded,
        # and no poller starts -- nothing runs the post-install hook, ever.
        agent.rebuild_agent_config()
        agent.prime_ceiling_projection()
        assert agent._projected_ceiling_generation is None
        assert policy_distribution.start_refresher() is False, "this host polls no distribution"
        assert retired in json.loads(pipeline.read_text(encoding="utf-8"))["allowedTools"]

        retry = getattr(agent, "retry_held_conductor_specs", None)
        assert retry is not None, (
            "nothing retries a conductor spec the boot rebuild held on a host that "
            "polls no distribution: its previous-ceiling allowedTools stand for the "
            "process lifetime"
        )
        # The wake while the read still fails: the hold stands, and the memo with it.
        with caplog.at_level(logging.WARNING):
            assert retry() is False
        assert agent._conductor_spec_held is True
        assert retired in json.loads(pipeline.read_text(encoding="utf-8"))["allowedTools"]
        assert any("still unwritten" in r.getMessage() for r in caplog.records)

        # The read clears: the next wake rewrites the spec, and only then does the
        # memo clear.
        unreadable["value"] = False
        assert retry() is True
        assert agent._conductor_spec_held is False
        assert retired not in json.loads(pipeline.read_text(encoding="utf-8"))["allowedTools"]

        # Nothing held: a wake runs no rebuild at all.
        with monkeypatch.context() as scoped:
            scoped.setattr(
                agent,
                "rebuild_agent_config_reporting",
                lambda **kw: pytest.fail("a wake with nothing held ran a rebuild"),
            )
            assert retry() is True

    def test_the_hourly_maintenance_wake_retries_held_specs_before_its_sweeps(self) -> None:
        """The retry rides the gateway's existing hourly maintenance wake -- the one
        loop that already hosts the scratch, work-root and session-dir sweeps rather
        than a scheduler of its own -- first in the wake, off the event loop, and
        from nowhere else: no second timer, no thread."""
        from kiro_crew.slack import gateway

        source = Path(gateway.__file__).read_text(encoding="utf-8")
        wake = source.index("async def _run_agent_scratch_sweep()")
        slept = source.index("await asyncio.sleep(3600)", wake)
        retried = source.index("await asyncio.to_thread(retry_held_conductor_specs)", slept)
        swept = source.index("await asyncio.to_thread(agent_scratch.sweep_dead_scratch)", slept)
        assert slept < retried < swept, "the retry runs first in the hourly wake"
        assert (
            source.count("await asyncio.to_thread(retry_held_conductor_specs)") == 1
            and source.count("import retry_held_conductor_specs") == 1
        ), "one import and one call, on the existing wake"

    def test_a_spec_written_over_does_not_hold_the_memo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Bytes that can never be a spec are written over, so that rebuild DID
        re-derive the list and holds nothing. The damage here keeps the
        document parseable (``allowedTools`` is not a list): under a relocated home
        the shared-home guard reads an unparseable spec as foreign and refuses the
        whole rebuild before any installer runs -- its own, older, conservative
        arm -- so that shape never reaches the installer here."""
        shared = self._shared_home_rig(monkeypatch, tmp_path)
        agent.rebuild_agent_config()
        security = shared / SECURITY_CONDUCTOR_AGENT_FILENAME
        damaged = json.loads(security.read_text(encoding="utf-8"))
        damaged["allowedTools"] = "session"
        security.write_text(json.dumps(damaged), encoding="utf-8")

        wrote, held = self._report()

        assert (wrote, held) == (True, [False])
        rewritten = json.loads(security.read_text(encoding="utf-8"))
        assert rewritten["name"] == "kirocrew-security-conductor"
        assert isinstance(rewritten["allowedTools"], list) and rewritten["allowedTools"]
