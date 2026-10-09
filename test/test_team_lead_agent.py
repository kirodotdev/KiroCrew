"""The team-lead crewmate: its spec, its charter, its provisioning and its skill.

Follows ``test_security_conductor_agent.py``'s installer half -- stub the agents
dir and ``build_agent_config``, run the installer, assert on the JSON it wrote --
and adds the three halves that agent does not have: a charter whose contract
sentences are pinned because the capability is the sentence, a crewmate record
whose provisioning must be safe to repeat on every boot, and a skill that reuses
another skill's scripts rather than carrying a copy.

Each class is named so it can be selected alone with ``-k``.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import source_corpus

from kiro_crew import agent, agent_state, subagent
from kiro_crew.acp.types import METHOD_SET_MODE
from kiro_crew.agent_files import (
    OWNED_KIRO_AGENT_FILES,
    REQUIRED_KIRO_AGENT_FILES,
    TEAM_LEAD_AGENT_FILENAME,
)
from kiro_crew.dashboard.handlers.agents import api_agent_detail
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.testing.ids import UNALLOCATABLE_PID

_ACCEPTS = SPEC_PERMISSIONS_MIN_VERSION
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SKILLS = _REPO_ROOT / "src" / "kiro_crew" / "builtin_skills"


class _Recorder:
    """A SEL sink that accepts every record and keeps it, for a test that needs neither.

    Stands in for the real ``SecurityEventLog`` so a test observing something else -- the
    spec lock's critical section -- is not also exercising SEL init, which reads and
    writes the trust directory. The installer's own audits are fenced, so this is a
    convenience rather than a requirement; the tests that assert ON the records define
    their own local recorder beside the assertions that read it.
    """

    def __init__(self) -> None:
        self.records: list[dict] = []

    def log_api_access(self, **kwargs):
        self.records.append(kwargs)


def _stub_environment(tmp_path, monkeypatch, *, may_auto_approve=None) -> None:
    """Pin the agents dir, the template and the writer's version gate.

    The template mirrors what ``config/defaults.json`` ships in the one respect
    these tests turn on: ``fs_write`` mounted and ungranted.
    """
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: _ACCEPTS)
    monkeypatch.setattr(
        agent,
        "build_agent_config",
        lambda: {
            "name": "kirocrew",
            "prompt": "file://x",
            "mcpServers": {
                "kirocrew-core": {"command": "/resolved/kirocrew", "args": ["mcp-core"]},
                "builder-mcp": {"command": "/x/builder", "args": []},
            },
            "tools": ["fs_write", "code", "execute_bash", "fs_read", "@kirocrew-core"],
            "allowedTools": ["fs_read", "@kirocrew-core"],
        },
    )
    monkeypatch.setattr(
        agent, "_kirocrew_mcp_invocation", lambda sub: ("/resolved/kirocrew", [sub])
    )
    monkeypatch.setattr(agent, "_may_auto_approve", may_auto_approve or (lambda ref: True))


def _install(tmp_path, monkeypatch, *, may_auto_approve=None) -> dict:
    _stub_environment(tmp_path, monkeypatch, may_auto_approve=may_auto_approve)
    assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
    return json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))


def _charter(tmp_path, monkeypatch) -> str:
    """The emitted prompt with its whitespace normalized, so a reflow cannot
    break an assertion about a sentence the charter actually makes."""
    return " ".join(_install(tmp_path, monkeypatch)["prompt"].split())


class TestTeamLeadSpecIsMaterializedByProductCode:
    def test_the_installer_writes_the_spec(self, tmp_path, monkeypatch):
        """The whole point of a shipped crewmate: the file is produced by an
        installer the rebuild calls, never by a hand that drops it in."""
        data = _install(tmp_path, monkeypatch)
        assert data["name"] == "kirocrew-team-lead"
        assert (tmp_path / TEAM_LEAD_AGENT_FILENAME).is_file()

    def test_the_rebuild_calls_it(self):
        """Eager, beside the other generated specs. ``session_create`` resolves an
        agent from a boot-time in-memory snapshot that no spec write refreshes, so
        a spec materialized later cannot be dispatched by name at all."""
        source = (_REPO_ROOT / "src" / "kiro_crew" / "agent.py").read_text(encoding="utf-8")
        assert "team_lead_agent._install_team_lead_agent(clean=clean)" in source

    def test_only_a_transient_hold_enters_the_retry_set(self):
        """``_conductor_spec_held`` feeds an hourly retry sweep, so a PERMANENT
        decline must not be counted there: the file is still somebody else's on
        every later pass, which makes it a retry that can never succeed and a log
        line reading as a failure for a correct decision."""
        source = (_REPO_ROOT / "src" / "kiro_crew" / "agent.py").read_text(encoding="utf-8")
        head, _, rest = source.partition("team_lead_agent._install_team_lead_agent(clean=clean)")
        assert rest, "the rebuild no longer calls the installer the way this test reads"
        assert "InstallOutcome.HELD" in rest[:200]
        # Not the collapsed form, which would count DECLINED as held.
        assert "not team_lead_agent._install_team_lead_agent" not in source

    def test_filename_is_owned_but_not_required(self):
        """Owned, so the convergence sweep rewrites it when the Playwright servers
        move. NOT required: that list fails every turn when its file is absent,
        and this spec's absence disables one feature."""
        assert TEAM_LEAD_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES
        assert TEAM_LEAD_AGENT_FILENAME not in REQUIRED_KIRO_AGENT_FILES

    def test_the_agent_is_advertised(self):
        """The opposite of every conductor, and for the reason that set exists: a
        conductor is withheld from the roster because work handed to it cannot be
        done, having no file-writing tool. This one can do the work."""
        assert "kirocrew-team-lead" not in subagent.UNADVERTISED_AGENTS


class TestTeamLeadInstallerDeclinesAForeignSpec:
    """Provenance, not existence. The spec id makes a collision unlikely rather
    than impossible, and an operator's own agent at this filename is not an
    out-of-date spec: replacing it destroys whatever put it there."""

    def test_a_crew_of_its_own_at_this_filename_is_left_alone(self, tmp_path, monkeypatch):
        """Left alone means BYTE-IDENTICAL. The install declines and changes nothing --
        no grant removed, no key added, no reformat. What keeps the grants on it from
        mattering is that a session on this spec is REFUSED, not that the file was
        sanitized: sanitizing needs a correct judgement about every field it touches,
        and refusing needs none."""
        foreign = {
            "name": "my-own-lead",
            "prompt": "mine",
            "tools": ["fs_write"],
            "mcpServers": {},
        }
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        target.write_text(json.dumps(foreign), encoding="utf-8")
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.DECLINED
        assert json.loads(target.read_text(encoding="utf-8")) == foreign, (
            "the install changed a spec it declined; it may read that file and refuse to "
            "run it, and nothing else"
        )

    def test_declining_is_reported_at_error_level(self, tmp_path, monkeypatch):
        """The installer's own caller swallows at debug, so without this the one
        event an operator needs is invisible at any ordinary level."""
        (tmp_path / TEAM_LEAD_AGENT_FILENAME).write_text(
            json.dumps({"name": "my-own-lead"}), encoding="utf-8"
        )
        _stub_environment(tmp_path, monkeypatch)
        errors: list[str] = []
        monkeypatch.setattr(
            agent.logger, "error", lambda msg, *a, **kw: errors.append(msg % a if a else msg)
        )
        assert agent._install_team_lead_agent() is agent.InstallOutcome.DECLINED
        assert errors and "my-own-lead" in errors[0]

    def test_its_own_previous_output_is_rewritten(self, tmp_path, monkeypatch):
        """Idempotence depends on this: the marks are what the installer itself
        writes, so a second rebuild re-derives rather than declining forever."""
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN

    def test_a_missing_defining_server_is_named_in_the_reason(self):
        """A spec sharing the name but not the mounts is somebody else's. The
        reason says which mark failed, so the log tells an operator what to look
        at rather than only that something is in the way."""
        for absent in agent._DEFINING_SERVERS:
            present = [s for s in agent._DEFINING_SERVERS if s != absent]
            spec = {
                "name": "kirocrew-team-lead",
                "mcpServers": {s: {} for s in present},
            }
            reason = agent._foreign_team_lead_spec_reason(spec)
            assert reason is not None and absent in reason, absent

    def test_only_the_shape_this_release_writes_is_accepted(self):
        """Nothing is healed, because there is no earlier release of this spec to
        heal. The mount must be in ``mcpServers``; the same reference reached
        through ``tools`` or a per-tool grant is somebody else's file."""
        assert (
            agent._foreign_team_lead_spec_reason(
                {
                    "name": "kirocrew-team-lead",
                    "mcpServers": {s: {} for s in agent._DEFINING_SERVERS},
                }
            )
            is None
        )
        reason = agent._foreign_team_lead_spec_reason(
            {
                "name": "kirocrew-team-lead",
                "tools": ["@kirocrew-dashboard"],
                "allowedTools": ["@kirocrew-work/work_ledger_read"],
            }
        )
        assert reason == "it declares no mcpServers map"

    def test_a_foreign_spec_with_a_malformed_grant_list_is_still_declined(
        self, tmp_path, monkeypatch
    ):
        """Attribution runs BEFORE grant-shape validation, and this is the case
        that proves it. The hardened reader rejects a non-list ``allowedTools`` as
        bytes it cannot use, which a hand-edit typo produces on a file that is
        otherwise entirely the operator's. Reading that as "no spec here" would
        skip attribution and overwrite the prompt, servers and model pin whole."""
        foreign = {
            "name": "my-own-lead",
            "prompt": "file:///somewhere/my-own-lead.md",
            "model": "my-pinned-model",
            "mcpServers": {"my-server": {"command": "x", "args": []}},
            "allowedTools": None,
        }
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        raw = json.dumps(foreign)
        target.write_text(raw, encoding="utf-8")
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.DECLINED
        # Byte-identical, typo included: the install does not repair a hand-edit and does
        # not sanitize one either.
        assert target.read_text(encoding="utf-8") == raw, "the operator's file was rewritten"

    def test_a_transient_read_in_the_attribution_gap_holds_rather_than_writes(
        self, tmp_path, monkeypatch
    ):
        """The narrow case the attribution read opens, and why it is a HOLD.

        The file parses, so the first read answers "cannot use these bytes" on
        grant shape alone; the attribution read then hits an I/O error. Treating
        that as "nothing to attribute" would overwrite a file that had just
        demonstrated it is somebody's. A transient failure is the reader's own
        other class, so it holds, and a hold costs one rebuild.
        """
        foreign = {
            "name": "my-own-lead",
            "prompt": "file:///somewhere/my-own-lead.md",
            "allowedTools": None,
        }
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        raw = json.dumps(foreign)
        target.write_text(raw, encoding="utf-8")
        _stub_environment(tmp_path, monkeypatch)

        # ONLY the attribution read fails. Failing both would hold for the first
        # read's own reason and prove nothing about this gap, which is the trap
        # this test exists inside: the two reads are told apart by the
        # ``operation`` each passes, so the real reader still serves the first.
        from kiro_crew import agent_discovery

        real = agent_discovery.read_agent_spec_strict
        calls: list[str] = []

        def _reader(path, *, operation, source):
            calls.append(operation)
            if operation == "team_lead_spec_attribution":
                raise OSError("input/output error")
            return real(path, operation=operation, source=source)

        monkeypatch.setattr(agent_discovery, "read_agent_spec_strict", _reader)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.HELD
        assert target.read_text(encoding="utf-8") == raw, "the operator's file was rewritten"
        assert calls == [
            "conductor_spec_regeneration",
            "team_lead_spec_attribution",
        ], f"the first read must succeed and the second must be reached: {calls}"

    def test_a_file_that_is_present_and_unparseable_is_declined(self, tmp_path, monkeypatch):
        """One missing brace is the ordinary result of a hand edit, and those bytes
        are the operator's whether or not they parse. Declining does not strand the
        agent: the remedy is documented and costs one rename, which is the
        dashboard-author contract rather than the derived worker's."""
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        raw = '{ "name": "my-own-lead", "prompt": "file:///mine.md"'
        target.write_text(raw, encoding="utf-8")
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.DECLINED
        assert target.read_text(encoding="utf-8") == raw, "the operator's file was rewritten"

    def test_a_present_name_with_no_readable_bytes_is_declined(self, tmp_path, monkeypatch):
        """A dangling link is not an absent file. Only ABSENT installs without a
        name to check, so a name that still exists behind the read failure is
        present with nothing to attribute."""
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        target.symlink_to(tmp_path / "nowhere.json")
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.DECLINED
        assert target.is_symlink(), "the link was replaced"

    def test_only_an_absent_path_installs_without_attribution(self, tmp_path, monkeypatch):
        """The control for both declines above, and the third of the three states.
        A guard that refused everything present would still have to install on an
        empty agents directory, which is every first install."""
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        assert not target.exists()
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert json.loads(target.read_text(encoding="utf-8"))["name"] == "kirocrew-team-lead"

    def test_the_three_outcomes_are_told_apart_on_the_same_path(self, tmp_path, monkeypatch):
        """The control for the two decline tests above. A guard that refused
        everything would pass them both, so this requires all three answers from
        the same path: a malformed foreign spec declined, a well-formed foreign
        spec declined, and a spec this installer wrote replaced.

        The third case is the installer's OWN write rather than a hand-made file
        carrying the right marks, because the marks are a shape and not authorship:
        only bytes reproducing the recorded ownership digest are replaced."""
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        _stub_environment(tmp_path, monkeypatch)

        target.write_text(json.dumps({"name": "theirs", "allowedTools": None}), encoding="utf-8")
        assert agent._install_team_lead_agent() is agent.InstallOutcome.DECLINED

        target.write_text(
            json.dumps({"name": "theirs", "mcpServers": {}, "allowedTools": []}),
            encoding="utf-8",
        )
        assert agent._install_team_lead_agent() is agent.InstallOutcome.DECLINED

        target.unlink()
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert json.loads(target.read_text(encoding="utf-8"))["prompt"].startswith(
            "# Kiro Crew Team Lead"
        )

    def test_the_global_mcp_merge_is_pinned_off(self, tmp_path, monkeypatch):
        """``includeMcpJson`` tells kiro-cli to merge the global ``mcp.json`` ON TOP of
        this spec, and the operator's own ``agent.json`` can set it, because
        ``build_agent_config`` deep-merges that file. Every filter in this installer reads
        ``config["mcpServers"]`` and nothing else, so a server arriving from the global
        file is governed by none of them: its ``autoApprove`` is never seen by the ceiling
        strip, and kiro-cli approves such a tool locally with no permission request. The
        pin is what makes the filtered map the only map.

        Control: a template that sets neither field still gets the pin written rather than
        left absent, because absent is not the same answer to kiro-cli as false."""
        _stub_environment(tmp_path, monkeypatch)
        base = agent.build_agent_config()
        base["includeMcpJson"] = True
        base["useLegacyMcpJson"] = True
        monkeypatch.setattr(agent, "build_agent_config", lambda: json.loads(json.dumps(base)))

        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        spec = json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))
        assert spec["includeMcpJson"] is False, (
            "the global mcp.json merge stayed on, so a server from that file reaches "
            "kiro-cli past every filter in this installer"
        )
        assert "useLegacyMcpJson" not in spec, "the retired spelling can re-open the merge"

        # Control: with neither field in the template the pin is still written, and the
        # spec this installer produces is otherwise the ordinary one.
        clean = agent.build_agent_config()
        clean.pop("includeMcpJson", None)
        clean.pop("useLegacyMcpJson", None)
        monkeypatch.setattr(agent, "build_agent_config", lambda: json.loads(json.dumps(clean)))
        (tmp_path / TEAM_LEAD_AGENT_FILENAME).unlink()
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        spec = json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))
        assert spec["includeMcpJson"] is False
        assert "useLegacyMcpJson" not in spec
        assert "@kirocrew-work/work_brief" in spec["allowedTools"]

    def test_every_grant_this_installer_withholds_is_audited(self, tmp_path, monkeypatch):
        """Withholding a grant is a permission DECISION, and an operator reads the SEL
        feed to find out why a grant they can see in the template is not on the spec. The
        whole-server narrowing is the one that fires on an ORDINARY rebuild -- the
        template's bare ``@kirocrew-core`` is subtracted every time -- and the ceiling pass
        after it sees only the survivors, so an unaudited subtraction there leaves no trace
        anywhere.

        All three of this installer's own withholds are required in ONE run, so a feed
        that recorded two of them does not pass."""
        records: list[dict] = []

        class _Recorder:
            def log_api_access(self, **kwargs):
                records.append(kwargs)

        _stub_environment(tmp_path, monkeypatch)
        monkeypatch.setattr(agent, "sel", lambda: _Recorder())
        base = agent.build_agent_config()
        base["mcpServers"] = {**(base.get("mcpServers") or {}), "kirocrew-panel": {"command": "/p"}}
        base["allowedTools"] = [*base.get("allowedTools", []), "session_*"]
        monkeypatch.setattr(agent, "build_agent_config", lambda: json.loads(json.dumps(base)))

        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        withheld = [
            str(r.get("resources"))
            for r in records
            if r.get("operation") == "mcp_auto_approve_withheld"
        ]
        # The whole-server narrowing: present on every rebuild, and the gap this pins.
        assert any(
            "@kirocrew-core" in r and "verb by verb" in r for r in withheld
        ), f"the whole-server narrowing left no audit record: {withheld}"
        # The two that already audited, required here so this cannot pass on one event.
        assert any("session_*" in r for r in withheld), f"no record names the pattern: {withheld}"
        assert any("kirocrew-panel" in r for r in withheld), f"no record names it: {withheld}"

    def test_the_whole_read_attribute_write_runs_under_the_spec_lock(self, tmp_path, monkeypatch):
        """This installer is a read-modify-writer and was the one that took no lock.
        ``agents_spec_lock`` is the template-spec writer lock every other one holds --
        the worker installer, the dashboard-author installer, the reset path, the fork
        refresh, the dashboard PATCH -- so two overlapping rebuilds could interleave
        their reads and writes here and silently revert each other.

        Pinned as ONE critical section rather than merely as "a lock is taken", because
        splitting it is the bug and not a smaller version of it: attributing under the
        lock and then writing outside it is the same race with a narrower window. The
        read, the attribution and the write are each required to happen while it is
        held, and the lock is required to be taken exactly once -- it is a cross-process
        ``flock`` on a sidecar path, so a second acquire from this process would wait on
        itself."""
        events: list[str] = []
        real_lock = agent.agents_spec_lock

        import contextlib

        @contextlib.contextmanager
        def _watched(agents_dir):
            events.append("lock-enter")
            with real_lock(agents_dir):
                yield
            events.append("lock-exit")

        # Watched on the owning module rather than the facade: the attribution read is
        # this installer's own helper, which reaches the conductor installers' hardened
        # reader, so the facade carries no name for it.
        from kiro_crew.agent_materialization import team_lead_agent

        real_read = team_lead_agent._existing_spec_for_attribution
        real_write = agent._atomic_json_write

        _stub_environment(tmp_path, monkeypatch)
        monkeypatch.setattr(agent, "sel", lambda: _Recorder())
        monkeypatch.setattr(agent, "agents_spec_lock", _watched)
        monkeypatch.setattr(
            team_lead_agent,
            "_existing_spec_for_attribution",
            lambda *a, **k: (events.append("read"), real_read(*a, **k))[1],
        )
        monkeypatch.setattr(
            agent,
            "_atomic_json_write",
            lambda *a, **k: (events.append("write"), real_write(*a, **k))[1],
        )

        # A file already present, so the attribution read actually runs rather than
        # short-circuiting on an absent path.
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        events.clear()
        assert target.is_file()
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN

        assert events.count("lock-enter") == 1, (
            f"the lock was taken {events.count('lock-enter')} times; it is a "
            f"cross-process flock, so a second acquire would wait on itself: {events}"
        )
        enter, exit_ = events.index("lock-enter"), events.index("lock-exit")
        assert "read" in events and "write" in events, f"neither ran: {events}"
        for stage in ("read", "write"):
            assert enter < events.index(stage) < exit_, (
                f"the {stage} happened outside the critical section, so another writer "
                f"can land between it and the rest: {events}"
            )

    def test_an_audit_failure_never_breaks_the_install(self, tmp_path, monkeypatch):
        """Same footing as every other audit here: an unwritable SEL sink costs one
        record, not the spec. The control for the test above -- an installer that raised
        out of its own audit would fail this one instead of recording anything."""

        class _Broken:
            def log_api_access(self, **kwargs):
                raise OSError("audit sink unavailable")

        _stub_environment(tmp_path, monkeypatch)
        monkeypatch.setattr(agent, "sel", lambda: _Broken())
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert (tmp_path / TEAM_LEAD_AGENT_FILENAME).is_file()

    def test_an_inherited_wildcard_grant_is_not_carried(self, tmp_path, monkeypatch):
        """The ceiling judges an ``allowedTools`` entry by its EXACT NAME, so a PATTERN
        escapes it silently: it is in no table and names no server, passes as an unmapped
        name, and kiro-cli then expands it over every builtin and every verb of every
        mounted server -- including ``@kirocrew-dashboard``, which this same installer
        mounts. ``session_?end`` is the sharp case: the exact spelling is withheld by name
        while the pattern that expands onto it would be auto-approved, on the one path
        that never reaches the approval gate.

        Both controls the ceiling's own behaviour depends on are asserted here, so a drop
        that took everything would fail: an exact grant the ceiling allows survives, and a
        bare ``@server`` entry is still narrowed by its own separate pass.
        """
        _stub_environment(tmp_path, monkeypatch)
        base = agent.build_agent_config()
        base["allowedTools"] = [
            *base.get("allowedTools", []),
            "session_*",
            "*_send",
            "@*",
            "@kirocrew-dashboard/session_?end",
        ]
        monkeypatch.setattr(agent, "build_agent_config", lambda: json.loads(json.dumps(base)))
        monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: "session_send" not in ref)

        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        spec = json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))
        allowed = spec["allowedTools"]

        for pattern in ("session_*", "*_send", "@*", "@kirocrew-dashboard/session_?end"):
            assert pattern not in allowed, (
                f"{pattern} reached the spec, so kiro-cli would expand it over a verb the "
                "ceiling withholds by name"
            )
        # Control: the exact grants this installer ships are judged by name and kept, so
        # the drop is "patterns" and not "anything the operator touched".
        assert "fs_read" in allowed
        assert "@kirocrew-work/work_brief" in allowed
        # Control: the bare ``@server`` pass still does its own separate job, which a
        # wildcard filter neither replaces nor is replaced by.
        assert "@kirocrew-core" not in allowed

    def test_a_governed_auto_approve_on_an_inherited_server_is_stripped(
        self, tmp_path, monkeypatch
    ):
        """``autoApprove`` on an ``mcpServers`` entry is the SECOND channel a call skips
        the PreToolUse gate through: kiro-cli approves such a tool locally and emits no
        permission request, so no amount of ``allowedTools`` filtering reaches it. This
        map is ADDITIVE and ``build_agent_config`` deep-merges the operator's own
        ``agent.json``, so an entry they wrote arrives with whatever ``autoApprove`` they
        gave it -- a grant no ceiling has read.

        Driven through the real governance pass by its own predicate rather than a stubbed
        strip, so the two answers are the ceiling's. Both controls are here: a governed
        server keeps nothing, an ungoverned one the operator wrote survives, and the
        ``allowedTools`` filter still does its own separate job.
        """
        from kiro_crew.platform import governance

        monkeypatch.setattr(
            governance, "may_skip_gate_now", lambda ref: not ref.startswith("@governed")
        )
        _stub_environment(tmp_path, monkeypatch)
        base = agent.build_agent_config()
        base["mcpServers"] = {
            **(base.get("mcpServers") or {}),
            "governed": {"command": "/x/g", "args": [], "autoApprove": ["delete"]},
            "friendly": {"command": "/x/f", "args": [], "autoApprove": ["read"]},
        }
        monkeypatch.setattr(agent, "build_agent_config", lambda: json.loads(json.dumps(base)))

        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        spec = json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))
        servers = spec["mcpServers"]

        assert "autoApprove" not in servers["governed"], (
            "a ceiling-governed autoApprove reached the spec, so a native chat would run "
            "the denied tool without a permission request"
        )
        # The server itself stays mounted: its tools go through the approval gate, which
        # is where a per-argument ceiling rule is actually applied.
        assert "governed" in servers
        # Control: the ceiling is silent about this one and the operator wrote it, so
        # their own statement about their own tools survives. Without this a pass that
        # stripped every autoApprove would pass the assertion above.
        assert servers["friendly"]["autoApprove"] == ["read"]
        # Control: the other channel still behaves as before -- the shipped grants are
        # on the list and the template's whole-server entry is still subtracted.
        assert "@kirocrew-work/work_brief" in spec["allowedTools"]
        assert "@kirocrew-core" not in spec["allowedTools"]

    def test_an_opt_in_set_the_operator_mounted_is_not_inherited(self, tmp_path, monkeypatch):
        """An ``opt_in`` server is an assignable SET, not an always-on capability: the
        agents that need one hand-build the entry, and that IS the per-agent assignment.
        An operator mounting one on their personal ``agent.json`` has assigned it to that
        agent, so it must not ride through ``build_agent_config`` into this spec -- least
        of all ``kirocrew-panel``, which this agent's own charter says no spec may emit.

        All three surfaces, because a server reaches a session through any of them."""
        _stub_environment(tmp_path, monkeypatch)
        base = agent.build_agent_config()
        base["mcpServers"] = {**(base.get("mcpServers") or {}), "kirocrew-panel": {"command": "/p"}}
        base["tools"] = [*base.get("tools", []), "@kirocrew-panel"]
        base["allowedTools"] = [*base.get("allowedTools", []), "@kirocrew-panel"]
        monkeypatch.setattr(agent, "build_agent_config", lambda: json.loads(json.dumps(base)))

        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        spec = json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))
        assert "kirocrew-panel" not in spec["mcpServers"]
        assert "@kirocrew-panel" not in spec["tools"]
        assert "@kirocrew-panel" not in spec["allowedTools"]
        # Control: the drop set is "the opt-in sets nobody assigned HERE", not "every
        # opt-in set". Asserted on the set itself, because the two this installer assigns
        # are re-added after the drop -- so their presence in the written spec would hold
        # even if the drop had taken them, and would prove nothing.
        unassignable = agent._team_lead_unassignable_servers()
        assert "kirocrew-panel" in unassignable
        for server in agent._DEFINING_SERVERS:
            assert server not in unassignable, server
        # And the entries that survive are the installer's hand-built ones: an operator
        # who mounts their own `kirocrew-work` does not get to decide how it launches.
        assert spec["mcpServers"]["kirocrew-work"]["args"] == ["mcp-work"]

    def test_an_in_place_edit_is_declined_rather_than_silently_reverted(
        self, tmp_path, monkeypatch
    ):
        """An operator who opens the installed file and changes its prompt or its model
        leaves the declared name and both mounted servers exactly as they were, and holds
        no lineage record. On the marks alone that file reads as this installer's own and
        is rewritten whole on the next rebuild, so EVERY in-place edit would be silently
        reverted -- ordinary use, not a forge.

        Authorship is therefore the digest the installer records for the bytes it lands.
        The control that matters most is the second one: the installer's own unmodified
        write must still be replaceable, because an over-tight rule here means the agent
        can never be updated again."""
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        _stub_environment(tmp_path, monkeypatch)

        # Control: an absent path installs and records its ownership digest.
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert agent_state.get_managed_digest("kirocrew-team-lead"), "no ownership recorded"

        # Control: the installer's own unmodified write is replaced on the next rebuild, so
        # the digest does not freeze the spec against future releases.
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN

        # The finding: an in-place edit that leaves both marks intact.
        spec = json.loads(target.read_text(encoding="utf-8"))
        assert spec["name"] == "kirocrew-team-lead"
        for server in agent._DEFINING_SERVERS:
            assert server in spec["mcpServers"], server
        spec["prompt"] = "my own charter"
        edited = json.dumps(spec, indent=2) + "\n"
        target.write_text(edited, encoding="utf-8")

        assert agent._install_team_lead_agent() is agent.InstallOutcome.DECLINED
        assert target.read_text(encoding="utf-8") == edited, "the operator's edit was reverted"

    def test_a_recorded_private_copy_is_declined_though_both_marks_hold(
        self, tmp_path, monkeypatch
    ):
        """Both marks can arrive without anyone forging them. A private copy is named after
        the crew that owns it and its declared ``name`` is set to that stem, so a crew named
        for this agent lands a file declaring this agent's name; copy any conductor and that
        file mounts both defining servers too. The lineage record is the one signal no spec
        field carries, so it is read first and the crew's own bytes are left alone.

        Driven against a file this installer DID write, so the record is the only thing
        that changes the answer and is shown to outrank even a confirmed ownership digest:
        a crew's lineage on this name means hands off whoever wrote the bytes."""
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        _stub_environment(tmp_path, monkeypatch)

        # Control: our own write, replaced on the next rebuild, so the decline below is the
        # record's doing and not some other rejection of this file.
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        raw = target.read_text(encoding="utf-8")

        agent_state.set_fork_info(
            "kirocrew-team-lead", forked_from="kirocrew-conductor", private_to="a-crew"
        )
        assert agent._install_team_lead_agent() is agent.InstallOutcome.DECLINED
        # Byte-identical, and this is the ONE declined class the strip leaves alone. A
        # recorded fork is already governed -- ``fork_refresh`` projects the ceiling onto
        # each recorded fork's grants -- and stripping it would break that: these bytes
        # are this installer's own write, with a matching ownership digest, so rewriting
        # them under the record leaves a digest that can never match again and the
        # control below could never pass.
        assert target.read_text(encoding="utf-8") == raw, "the crew's private copy was rewritten"

        # Control: clearing the record restores the write, so the decline tracks the record
        # rather than latching on the first refusal.
        agent_state.clear_fork_info("kirocrew-team-lead")
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN

    def test_a_lineage_read_that_may_succeed_later_holds_rather_than_writes(
        self, tmp_path, monkeypatch
    ):
        """The record is a SECOND read, so it has the spec read's two classes. A sidecar that
        cannot be read is not a file nobody claims: collapsing the two would replace a crew's
        copy on one failed read, so it holds and the next rebuild asks again."""
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        raw = json.dumps(
            {
                "name": "kirocrew-team-lead",
                "mcpServers": {s: {} for s in agent._DEFINING_SERVERS},
                "allowedTools": [],
            }
        )
        target.write_text(raw, encoding="utf-8")
        _stub_environment(tmp_path, monkeypatch)

        def _unreadable(_name, *, strict=False):
            raise OSError("input/output error")

        monkeypatch.setattr(agent_state, "get_fork_info", _unreadable)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.HELD
        assert target.read_text(encoding="utf-8") == raw, "the operator's file was rewritten"

    def test_an_absent_path_is_the_one_state_with_nothing_to_attribute(self):
        """``None`` means absent, and absent is the only answer that writes. A
        present file that does not parse never reaches this function: the caller
        declines it before asking, because unreadable bytes at this name are still
        somebody's."""
        assert agent._foreign_team_lead_spec_reason(None) is None

    def test_a_hand_edited_container_does_not_raise_out_of_the_check(self):
        """The map is shape-checked before it is read, so a wrong-typed value is
        a declined write rather than an error out of an attribution."""
        for bad in (1, "kirocrew-dashboard", ["kirocrew-dashboard"]):
            reason = agent._foreign_team_lead_spec_reason(
                {"name": "kirocrew-team-lead", "mcpServers": bad}
            )
            assert reason == "it declares no mcpServers map", bad
        # A map that IS one, missing a mount, names the mount.
        reason = agent._foreign_team_lead_spec_reason(
            {"name": "kirocrew-team-lead", "mcpServers": {"kirocrew-work": {}}}
        )
        assert reason is not None and "kirocrew-dashboard" in reason


class TestAHandEditedSpecIsRefusedRatherThanRewritten:
    """The decline governs by REFUSING THE START, not by sanitizing the file.

    Rewriting somebody's spec to make it safe needs a correct judgement about every
    field it touches, and ``permissions`` is the field that shows why that is the
    wrong shape: ``kas_permissions._EFFECTS`` admits ``deny`` and ``ask`` beside
    ``allow``, so a pass emptying the block to remove GRANTS could delete a DENY the
    operator wrote and leave them less restricted than they asked to be. Refusing
    needs no such judgement and changes nothing on disk.

    Selectable alone with ``-k HandEdited``."""

    def test_an_edited_spec_is_untouched_and_its_start_is_refused(self, tmp_path, monkeypatch):
        """Both halves in one run, because either alone is the bug: untouched without a
        refusal leaves the edited grants live, and a refusal that rewrote the file first
        is the thing this replaces."""
        _stub_environment(tmp_path, monkeypatch)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN

        spec = json.loads(target.read_text(encoding="utf-8"))
        spec["prompt"] = "my own charter"
        spec["allowedTools"] = [*spec["allowedTools"], "web_*"]
        spec["permissions"] = {"rules": [{"capability": "shell", "effect": "deny"}]}
        edited = json.dumps(spec, indent=2) + "\n"
        target.write_text(edited, encoding="utf-8")

        assert agent._install_team_lead_agent() is agent.InstallOutcome.DECLINED
        assert target.read_text(encoding="utf-8") == edited, (
            "the install rewrote a spec it declined; the operator's own `deny` rule is "
            "exactly what a sanitizing pass would have removed"
        )
        refusal = agent.spec_start_refusal("kirocrew-team-lead")
        assert refusal == (
            "kirocrew-team-lead.json was edited by hand; Kiro Crew will not run it. "
            "Rename it to keep your copy, or delete it to get the shipped one back."
        ), refusal

    def test_the_refusal_reaches_every_start_through_the_shared_gate(self, tmp_path, monkeypatch):
        """Wired into ``require_fork_governance``, not into the three harnesses. The KAS
        harness, the kiro harness and the ACP client all call that one function, so a
        rule copied into each is a rule that will be in two of them after the next
        change. Raised as ``ForkGovernanceUnresolved``, which is what all three already
        catch, so the message reaches the operator unchanged."""
        _stub_environment(tmp_path, monkeypatch)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        spec = json.loads(target.read_text(encoding="utf-8"))
        spec["prompt"] = "mine"
        target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")

        with pytest.raises(agent.ForkGovernanceUnresolved) as caught:
            agent.require_fork_governance("kirocrew-team-lead", tmp_path)
        assert "will not run it" in str(caught.value)
        # Control: the gate admits every OTHER non-fork agent exactly as before, so this
        # is one refusal and not a new class of them.
        agent.require_fork_governance("kirocrew-worker", tmp_path)
        agent.require_fork_governance("some-operator-agent", tmp_path)

    def test_deleting_the_file_restores_the_shipped_spec(self, tmp_path, monkeypatch):
        """The remedy the message names has to work. Renaming frees the name the same
        way; deleting is the half that must also put the shipped spec back."""
        _stub_environment(tmp_path, monkeypatch)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        spec = json.loads(target.read_text(encoding="utf-8"))
        spec["prompt"] = "mine"
        target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        assert agent._install_team_lead_agent() is agent.InstallOutcome.DECLINED
        assert agent.spec_start_refusal("kirocrew-team-lead") is not None

        target.unlink()
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert json.loads(target.read_text(encoding="utf-8"))["prompt"] != "mine"
        assert agent.spec_start_refusal("kirocrew-team-lead") is None, (
            "the shipped spec is back and this installer confirms it, so nothing is left "
            "to refuse"
        )

    def test_a_recorded_fork_still_starts(self, tmp_path, monkeypatch):
        """THE control for the fork lane. A recorded fork is a supported feature with its
        own governance -- the refresh re-filters its grants and the gate's own wait holds
        its starts -- so refusing it here would break that lane outright, which is a worse
        regression than the finding this closes.

        It cannot happen by construction rather than by care: the refusal is reached only
        on the gate's own NON-fork path, keyed on the ``is_fork`` the gate already
        resolved, so there is one sidecar read and one notion of "is a fork" rather than
        two that can disagree. Driven by SETTING a real lineage record, not by stubbing a
        predicate."""
        _stub_environment(tmp_path, monkeypatch)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        # Edited AND recorded as a crew's private copy: on bytes alone this is the
        # refusable case, so only the record can be what admits it.
        spec = json.loads(target.read_text(encoding="utf-8"))
        spec["prompt"] = "the crew's own charter"
        target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        assert agent.spec_start_refusal("kirocrew-team-lead") is not None

        agent_state.set_fork_info(
            "kirocrew-team-lead", forked_from="kirocrew-conductor", private_to="a-crew"
        )
        import kiro_crew.agent_materialization.fork_refresh as fr

        fr._fork_refresh_settled.set()
        monkeypatch.setattr(fr, "_fork_refresh_failed", frozenset())
        # Admitted: the fork path runs its own checks and reaches no refusal here.
        agent.require_fork_governance("kirocrew-team-lead", tmp_path)

        # Control: clear the record and the SAME bytes are refused again, so the record
        # is the only thing that moves the answer.
        agent_state.clear_fork_info("kirocrew-team-lead")
        with pytest.raises(agent.ForkGovernanceUnresolved):
            agent.require_fork_governance("kirocrew-team-lead", tmp_path)

    def test_a_rebuild_that_dies_before_this_installer_still_refuses_the_start(
        self, tmp_path, monkeypatch
    ):
        """THE case a marker cannot catch, and the reason the check is stateless.

        ``rebuild_agent_config`` writes the default spec BEFORE it reaches this
        installer, so a failure there ends the pass without this installer running at
        all -- and any marker the installer would have set is left exactly as a healthy
        boot leaves it. Asking the question at admission needs no cooperation from the
        rebuild: the file's own grants are compared with what the current ceiling would
        produce, so a rebuild that never ran is indistinguishable from one that ran and
        could not write, which is the point."""
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert agent.spec_start_refusal("kirocrew-team-lead") is None

        # The ceiling tightens against a verb the file already auto-approves, and the
        # rebuild that would re-derive it dies on the FIRST spec it writes.
        monkeypatch.setattr(
            agent, "_may_auto_approve", lambda r: not r.startswith("@kirocrew-core/")
        )
        from kiro_crew.agent_materialization import default_spec_commit

        monkeypatch.setattr(
            default_spec_commit,
            "write_default_spec",
            lambda *a, **k: (_ for _ in ()).throw(OSError("read-only file system")),
        )
        with pytest.raises(Exception):
            agent.rebuild_agent_config()

        refusal = agent.spec_start_refusal("kirocrew-team-lead")
        assert refusal is not None, (
            "the rebuild died before this installer ran, so its grants were never "
            "re-derived under the tightened ceiling, and a session would consume them"
        )
        assert "earlier governance policy" in refusal, refusal
        assert "edited by hand" not in refusal, "an operator who edited nothing was told they did"
        with pytest.raises(agent.ForkGovernanceUnresolved, match="earlier governance policy"):
            agent.require_fork_governance("kirocrew-team-lead", tmp_path)

    def test_a_failed_team_lead_write_under_a_tightened_ceiling_refuses_the_start(
        self, tmp_path, monkeypatch
    ):
        """The same answer when the install DOES run and cannot write. The file keeps
        the previous ceiling's list, and the check reads that off the file rather than
        off any record of what the install did."""
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN

        monkeypatch.setattr(
            agent, "_may_auto_approve", lambda r: not r.startswith("@kirocrew-core/")
        )
        with monkeypatch.context() as broken:
            broken.setattr(
                agent,
                "_atomic_json_write",
                lambda *a, **k: (_ for _ in ()).throw(OSError("no space left on device")),
            )
            with pytest.raises(OSError):
                agent._install_team_lead_agent()

        refusal = agent.spec_start_refusal("kirocrew-team-lead")
        assert refusal is not None and "earlier governance policy" in refusal, refusal

    def test_autoapprove_drift_alone_refuses_the_start(self, tmp_path, monkeypatch):
        """The half that goes inert if the pair is not run, and it needs its own case.
        ``_apply_allowed_tools_ceiling`` returns early on its own key and never looks at
        ``mcpServers``, so a check that ran only the ceiling and then compared
        ``autoApprove`` would compare the file against itself and always pass.

        Driven with ``allowedTools`` DELIBERATELY left alone: the ceiling permits every
        grant on the list, and only the server-level auto-approve is governed away. If
        this reddens only because the grant list also drifted, it would prove nothing
        about the strip pass."""
        from kiro_crew.platform import governance

        _stub_environment(tmp_path, monkeypatch)
        base = agent.build_agent_config()
        base["mcpServers"] = {
            **(base.get("mcpServers") or {}),
            "vendor": {"command": "/v", "autoApprove": ["delete"]},
        }
        monkeypatch.setattr(agent, "build_agent_config", lambda: json.loads(json.dumps(base)))
        monkeypatch.setattr(governance, "may_skip_gate_now", lambda r: True)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        spec = json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))
        assert spec["mcpServers"]["vendor"].get("autoApprove") == ["delete"], (
            "the fixture never got an autoApprove onto the file, so this test cannot "
            "be about the strip pass"
        )
        assert agent.spec_start_refusal("kirocrew-team-lead") is None

        # The ceiling tightens against THAT SERVER only. Every allowedTools entry is
        # still permitted, so the grant list does not drift and the ceiling pass alone
        # would see nothing.
        monkeypatch.setattr(governance, "may_skip_gate_now", lambda r: r != "@vendor")
        from kiro_crew.agent_materialization import team_lead_agent as tla

        probe = json.loads(json.dumps(spec))
        tla.auto_approve._apply_allowed_tools_ceiling(probe, source="t")
        assert (
            probe["allowedTools"] == spec["allowedTools"]
        ), "the grant list drifted too, so this case no longer isolates autoApprove"
        refusal = agent.spec_start_refusal("kirocrew-team-lead")
        assert refusal is not None and "earlier governance policy" in refusal, (
            "a server-level autoApprove the ceiling now denies stayed live on a spec "
            "kiro-cli loads, and that channel never reaches the approval gate"
        )

    def test_a_clean_rebuild_admits_the_start(self, tmp_path, monkeypatch):
        """The over-tight control, and the one a too-strict comparison breaks. A spec
        this installer just wrote under the CURRENT ceiling must admit -- the passes run
        on the copy have to produce exactly what is already on the file. Comparing more
        than the governed fields reddens this while every refusal test above stays
        green, which is how a comparison that refuses forever would otherwise ship."""
        _stub_environment(tmp_path, monkeypatch)
        # SCOPED, so the restrictive ceiling comes off without an undo(): the global
        # form would also drop the ``_stub_environment`` this test stands on, and the
        # second half below would then run against the real environment.
        with monkeypatch.context() as tightened:
            tightened.setattr(
                agent, "_may_auto_approve", lambda r: not r.startswith("@kirocrew-core/")
            )
            assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
            assert agent.spec_start_refusal("kirocrew-team-lead") is None, (
                "a spec written under this very ceiling reads as drifted, so this agent "
                "can never start"
            )
            agent.require_fork_governance("kirocrew-team-lead", tmp_path)

        # And again with the ordinary ceiling, so the admit is not an artifact of the
        # one stub above.
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert agent.spec_start_refusal("kirocrew-team-lead") is None

    def test_a_recorded_fork_still_starts_under_a_tightened_ceiling(self, tmp_path, monkeypatch):
        """The fork lane stays out, third round running. A recorded fork has its own
        governance -- the refresh re-filters its grants and the gate's own wait holds
        its starts -- and the drift check is reached only on the gate's non-fork path,
        so drift on the TEMPLATE cannot block a fork."""
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        monkeypatch.setattr(
            agent, "_may_auto_approve", lambda r: not r.startswith("@kirocrew-core/")
        )
        assert agent.spec_start_refusal("kirocrew-team-lead") is not None

        agent_state.set_fork_info(
            "kirocrew-team-lead", forked_from="kirocrew-conductor", private_to="a-crew"
        )
        import kiro_crew.agent_materialization.fork_refresh as fr

        fr._fork_refresh_settled.set()
        monkeypatch.setattr(fr, "_fork_refresh_failed", frozenset())
        agent.require_fork_governance("kirocrew-team-lead", tmp_path)

        # Control: clear the record and the same drift refuses again.
        agent_state.clear_fork_info("kirocrew-team-lead")
        with pytest.raises(agent.ForkGovernanceUnresolved, match="earlier governance policy"):
            agent.require_fork_governance("kirocrew-team-lead", tmp_path)

    def test_the_drift_check_does_no_io_and_emits_no_audit_on_a_healthy_start(
        self, tmp_path, monkeypatch
    ):
        """It runs on EVERY session start, so its cost is part of the contract. The
        installer's passes are being run outside the installer, and two of them emit an
        install-time SEL record by default -- a per-start copy of that event would be a
        lie in the feed. The strip takes ``audit=False``; the ceiling pass has no such
        switch and emits only when it withholds, which is the refusal path."""
        import subprocess

        _stub_environment(tmp_path, monkeypatch)
        records: list[dict] = []

        class _Recording:
            def log_api_access(self, **kwargs):
                records.append(kwargs)

        monkeypatch.setattr(agent, "sel", lambda: _Recording())
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        spec = json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))
        from kiro_crew.agent_materialization import team_lead_agent as tla

        tla._ceiling_drift_reason(spec)  # warm any caches the config loader holds
        records.clear()
        spawns: list[object] = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawns.append(a), raising=False)
        assert tla._ceiling_drift_reason(spec) is None
        assert not spawns, f"the drift check spawned a subprocess per start: {spawns}"
        assert not records, f"a healthy start emitted install-time audit records: {records}"

    def test_a_model_reset_renews_the_digest_and_leaves_the_agent_startable(
        self, tmp_path, monkeypatch
    ):
        """An ORDINARY user action must not brick the agent. ``reset_agent_model`` edits
        the installed spec in place to drop the model pin, so without renewing the
        recorded ownership digest the file stops matching it, the next rebuild declines
        it as somebody else's, and every later start is refused -- for a reset the user
        was invited to perform, with nothing telling them why.

        The renewal is the mechanism dashboard-author already had; this spec is a second
        entry in one table rather than a second branch beside it."""
        _stub_environment(tmp_path, monkeypatch)
        # A model PIN is the precondition a reset exists for, and the stub template
        # carries none, so it is set here rather than assumed.
        base = agent.build_agent_config()
        base["model"] = "my-pinned-model"
        monkeypatch.setattr(agent, "build_agent_config", lambda: json.loads(json.dumps(base)))
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert json.loads(target.read_text(encoding="utf-8")).get("model"), (
            "the fixture wrote no model pin, so a reset would strip nothing and this "
            "test could not be about the reset"
        )
        assert agent.spec_start_refusal("kirocrew-team-lead") is None

        path, previous = agent.reset_agent_model("kirocrew-team-lead")
        assert path == target and previous
        after = json.loads(target.read_text(encoding="utf-8"))
        assert not after.get("model"), "the reset did not remove the model pin"
        # The finding: the agent is still startable afterwards.
        assert agent.spec_start_refusal("kirocrew-team-lead") is None, (
            "an authorized model reset left the spec unconfirmable, so the user who "
            "performed it can never run this agent again"
        )
        agent.require_fork_governance("kirocrew-team-lead", tmp_path)
        # And it is a real renewal rather than the refusal being skipped: the sidecar
        # now records the digest of the NEW bytes.
        assert agent_state.managed_digest_matches(
            "kirocrew-team-lead", agent_state.spec_digest(after), strict=True
        )

    def test_a_model_reset_on_a_hand_edited_spec_does_not_launder_it(self, tmp_path, monkeypatch):
        """THE control, and why the renewal is keyed on the PRE-EDIT bytes. A reset that
        renewed unconditionally would be a way to LAUNDER a user-authored spec into a
        confirmed one, handing it exactly the provenance the installer refused it. The
        reset may still strip the model; what it must not do is stamp the result ours."""
        _stub_environment(tmp_path, monkeypatch)
        # A model PIN is the precondition a reset exists for, and the stub template
        # carries none, so it is set here rather than assumed.
        base = agent.build_agent_config()
        base["model"] = "my-pinned-model"
        monkeypatch.setattr(agent, "build_agent_config", lambda: json.loads(json.dumps(base)))
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        spec = json.loads(target.read_text(encoding="utf-8"))
        spec["prompt"] = "my own charter"
        target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        assert agent.spec_start_refusal("kirocrew-team-lead") is not None

        agent.reset_agent_model("kirocrew-team-lead")
        after = json.loads(target.read_text(encoding="utf-8"))
        assert after["prompt"] == "my own charter", "the reset rewrote the operator's edit"
        refusal = agent.spec_start_refusal("kirocrew-team-lead")
        assert (
            refusal is not None and "edited by hand" in refusal
        ), f"a model reset laundered a hand-edited spec into a confirmed one: {refusal}"
        assert not agent_state.managed_digest_matches(
            "kirocrew-team-lead", agent_state.spec_digest(after), strict=True
        ), "the digest was renewed for bytes this installer never wrote"

    def test_a_model_reset_aborts_when_ownership_cannot_be_read(self, tmp_path, monkeypatch):
        """A transient read of the ownership record must ABORT the reset, not fall through
        as "not confirmed". The confirmed-ours spec at this stem has no heal path: if a
        failed read were degraded to False, the reset would take the unconfirmed-write
        branch, rewrite the bytes WITHOUT renewing the recorded digest, and every later
        start would then refuse the file forever -- for a user action, with storage that
        had only hiccuped. The fail-safe answer is to do nothing and let the next attempt,
        once the read recovers, succeed."""
        _stub_environment(tmp_path, monkeypatch)
        base = agent.build_agent_config()
        base["model"] = "my-pinned-model"
        monkeypatch.setattr(agent, "build_agent_config", lambda: json.loads(json.dumps(base)))
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        before_bytes = target.read_text(encoding="utf-8")
        assert json.loads(before_bytes).get("model"), (
            "the fixture wrote no model pin, so a reset would strip nothing and this "
            "test could not be about the reset"
        )
        assert agent.spec_start_refusal("kirocrew-team-lead") is None

        # Make the STRICT ownership-sidecar read raise the way a transient I/O error
        # does, exercising the real predicate path (``_attribution_reason`` ->
        # ``_unconfirmed_digest_reason`` -> ``managed_digest_matches(strict=True)``),
        # not a stub of the predicate. Scoped to the ownership-digest read so the reset's
        # other strict reads (capability intent, spec load) still work -- the point is a
        # transient failure of ONLY the ownership confirmation.
        real_matches = agent_state.managed_digest_matches
        strict_reads: list[str] = []

        def _flaky_matches(name, candidate, *, strict=False):
            if strict:
                strict_reads.append(name)
                raise OSError("transient EIO reading the ownership sidecar")
            return real_matches(name, candidate, strict=strict)

        monkeypatch.setattr(agent_state, "managed_digest_matches", _flaky_matches)

        # ``FileNotFoundError`` is the class the reset CONVERTS this to, not the class the
        # injection raises: ``reset_agent_model`` catches
        # ``(_SpecUnusable, OSError, ValueError)`` around ``_confirms_managed_pre_write``
        # and re-raises as ``FileNotFoundError`` (``agent.py``), which is the one failure
        # class its own contract and its callers already handle.
        #
        # MATCHED on the message, and that is not decoration: three other paths in the
        # same function raise the same class -- no spec at the name, an unreadable spec,
        # a spec that is not a JSON object -- so a bare ``raises`` block is satisfied by
        # an abort that happened BEFORE the ownership read, and every assertion below
        # then holds because nothing ran. The read is also asserted to have FIRED, so
        # neither half can go vacuous without failing.
        with pytest.raises(FileNotFoundError, match="could not read ownership record"):
            agent.reset_agent_model("kirocrew-team-lead")
        assert strict_reads == ["kirocrew-team-lead"], (
            "the injected ownership read never fired, so the abort came from somewhere "
            f"else in the reset and this test proves nothing: {strict_reads}"
        )

        # The spec was NOT mutated: the model pin and every byte survive, so when the
        # read recovers the agent is still its confirmed managed write.
        monkeypatch.setattr(agent_state, "managed_digest_matches", real_matches)
        assert (
            target.read_text(encoding="utf-8") == before_bytes
        ), "a failed ownership read rewrote the spec; the reset must abort before mutation"
        assert (
            agent.spec_start_refusal("kirocrew-team-lead") is None
        ), "the spec was left unconfirmable by a transient read failure"
        assert agent_state.managed_digest_matches(
            "kirocrew-team-lead", agent_state.spec_digest(json.loads(before_bytes)), strict=True
        )
        # And once the read recovers, the ordinary reset still works.
        path, previous = agent.reset_agent_model("kirocrew-team-lead")
        assert path == target and previous
        assert not json.loads(target.read_text(encoding="utf-8")).get("model")
        assert agent.spec_start_refusal("kirocrew-team-lead") is None

    def test_a_project_shadow_refuses_the_start_and_names_the_project_file(
        self, tmp_path, monkeypatch
    ):
        """We validated one file while kiro-cli would run another. It resolves
        ``--agent`` against ``<cwd>/.kiro/agents`` BEFORE the global directory, so a
        checkout shipping its own ``kirocrew-team-lead.json`` is the file that starts --
        and every other check in this module reads the GLOBAL spec, which in that
        situation nothing will execute. The global spec here is clean and CONFIRMED, so
        attribution and drift both pass: only the shadow check can refuse."""
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        project = tmp_path / "checkout"
        (project / ".kiro" / "agents").mkdir(parents=True)
        (project / ".kiro" / "agents" / TEAM_LEAD_AGENT_FILENAME).write_text(
            json.dumps(
                {
                    "name": "kirocrew-team-lead",
                    "tools": ["execute_bash"],
                    "allowedTools": ["execute_bash"],
                }
            ),
            encoding="utf-8",
        )
        # Control: the global spec on its own admits, so the refusal below is the
        # shadow and not some other complaint about the installed file.
        assert agent.spec_start_refusal("kirocrew-team-lead") is None

        refusal = agent.spec_start_refusal("kirocrew-team-lead", project_dir=project)
        assert refusal is not None, (
            "the project copy is what kiro-cli would run, and it was admitted on the "
            "strength of a different file"
        )
        assert TEAM_LEAD_AGENT_FILENAME in refusal and ".kiro/agents" in refusal, refusal
        assert (
            "edited by hand" not in refusal and "earlier governance policy" not in refusal
        ), f"the shadow reused another refusal's sentence, whose remedy is wrong: {refusal}"
        with pytest.raises(agent.ForkGovernanceUnresolved, match="project declares its own"):
            agent.require_fork_governance("kirocrew-team-lead", project)

    def test_a_project_shadow_is_refused_under_the_declared_name_too(self, tmp_path, monkeypatch):
        """Both spellings, for the reason the fork check already gives: a binding can
        carry the file STEM where the declared name differs, and the backend resolves
        either against the project directory. Checking only the binding name would let
        the other spelling through."""
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        project = tmp_path / "checkout"
        (project / ".kiro" / "agents").mkdir(parents=True)
        (project / ".kiro" / "agents" / TEAM_LEAD_AGENT_FILENAME).write_text(
            json.dumps({"name": "kirocrew-team-lead", "allowedTools": ["execute_bash"]}),
            encoding="utf-8",
        )
        # The BINDING is some other spelling; the DECLARED name is ours.
        refusal = agent.spec_start_refusal(
            "some-binding-name", "kirocrew-team-lead", project_dir=project
        )
        assert refusal is not None and "project declares its own" in refusal, refusal

    def test_no_project_shadow_admits_the_start(self, tmp_path, monkeypatch):
        """The over-tight control. A refusal that did not actually test the project
        directory would refuse every start in every checkout, which is a total outage
        rather than a guard. Driven with a project dir that EXISTS and declares other
        agents, so the admit is not an artifact of there being nothing to scan."""
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        project = tmp_path / "checkout"
        (project / ".kiro" / "agents").mkdir(parents=True)
        (project / ".kiro" / "agents" / "some-other-agent.json").write_text(
            json.dumps({"name": "some-other-agent"}), encoding="utf-8"
        )
        assert agent.spec_start_refusal("kirocrew-team-lead", project_dir=project) is None, (
            "a project declaring unrelated agents refused this one, so no checkout can " "start it"
        )
        agent.require_fork_governance("kirocrew-team-lead", project)
        # And with no project dir at all.
        assert agent.spec_start_refusal("kirocrew-team-lead", project_dir=None) is None

    def test_a_recorded_fork_keeps_its_own_shadow_refusal(self, tmp_path, monkeypatch):
        """The fork lane stays out, fourth round running. A shadowed FORK is refused by
        the check that was already there, with its own fork-specific sentence -- this
        one must not take that case over, because the remedies differ: a fork can also
        be repaired by dropping the fork record."""
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        project = tmp_path / "checkout"
        (project / ".kiro" / "agents").mkdir(parents=True)
        (project / ".kiro" / "agents" / TEAM_LEAD_AGENT_FILENAME).write_text(
            json.dumps({"name": "kirocrew-team-lead"}), encoding="utf-8"
        )
        agent_state.set_fork_info(
            "kirocrew-team-lead", forked_from="kirocrew-conductor", private_to="a-crew"
        )
        import kiro_crew.agent_materialization.fork_refresh as fr

        fr._fork_refresh_settled.set()
        monkeypatch.setattr(fr, "_fork_refresh_failed", frozenset())
        with pytest.raises(agent.ForkGovernanceUnresolved, match="private template copy"):
            agent.require_fork_governance("kirocrew-team-lead", project)

    def test_a_markdown_sibling_blocks_the_first_install_entirely(self, tmp_path, monkeypatch):
        """A DENY-LOSS, which is why this declines instead of filtering. A ``<stem>.md``
        can set a restriction -- a shell deny, a narrowed tool list -- and the JSON twin
        WINS over it, so writing our JSON does not add an agent beside theirs: it takes
        their file out of service and replaces what it forbade with what we permit.

        So: no JSON written, no ownership recorded, and their file byte-identical. The
        ownership half matters most -- recording a digest for a name we did not install
        is the one step that would make this irreversible."""
        _stub_environment(tmp_path, monkeypatch)
        md = tmp_path / "kirocrew-team-lead.md"
        authored = (
            "---\nname: kirocrew-team-lead\npermissions:\n"
            "  rules:\n    - capability: shell\n      effect: deny\n---\nMy own lead.\n"
        )
        md.write_text(authored, encoding="utf-8")

        assert agent._install_team_lead_agent() is agent.InstallOutcome.DECLINED
        assert not (tmp_path / TEAM_LEAD_AGENT_FILENAME).exists(), (
            "the JSON was written, and it takes precedence over the operator's Markdown "
            "agent -- their shell deny is gone"
        )
        assert md.read_text(encoding="utf-8") == authored, "their Markdown was rewritten"
        assert not agent_state.get_managed_digest("kirocrew-team-lead"), (
            "ownership was recorded for a name this installer did not install, which is "
            "what would make the shadowing irreversible"
        )
        refusal = agent.spec_start_refusal("kirocrew-team-lead")
        assert refusal is not None and "Markdown agent already occupies" in refusal, refusal
        # The fifth sentence reuses none of the other four.
        for other in ("edited by hand", "earlier governance policy", "project declares its own"):
            assert other not in refusal, f"reused another refusal's sentence: {refusal}"

    def test_a_markdown_sibling_is_caught_case_insensitively(self, tmp_path, monkeypatch):
        """On Windows and on macOS by default ``Foo.json`` and ``foo.md`` are ONE name, so
        their overlays would be one file -- the collision that matters most is exactly the
        one an exact-case comparison misses (``agent_spec_format._has_json_twin`` records
        the same reasoning for the twin it checks)."""
        _stub_environment(tmp_path, monkeypatch)
        # Every letter cased the other way, which also keeps the brand gate out of a
        # filename literal: the all-upper form is the env-var prefix, not the brand.
        (tmp_path / "KIROCREW-TEAM-LEAD.MD").write_text(
            "---\nname: kirocrew-team-lead\n---\nMine.\n", encoding="utf-8"
        )
        assert agent._install_team_lead_agent() is agent.InstallOutcome.DECLINED
        assert not (tmp_path / TEAM_LEAD_AGENT_FILENAME).exists()

    def test_no_markdown_sibling_installs_normally(self, tmp_path, monkeypatch):
        """The over-tight control. A decline that did not actually look for the sibling
        would refuse every first installation, which is the agent never shipping at all.
        Driven with OTHER Markdown agents present, so the install is not proceeding
        merely because the directory holds no ``.md`` at all."""
        _stub_environment(tmp_path, monkeypatch)
        (tmp_path / "some-other-agent.md").write_text(
            "---\nname: some-other-agent\n---\nTheirs.\n", encoding="utf-8"
        )
        (tmp_path / "kirocrew-team-lead-notes.md").write_text("Not a spec.\n", encoding="utf-8")
        assert (
            agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        ), "an unrelated Markdown agent blocked this install, so the agent can never ship"
        assert (tmp_path / TEAM_LEAD_AGENT_FILENAME).is_file()
        assert agent.spec_start_refusal("kirocrew-team-lead") is None

    def test_a_crewmate_bound_to_the_template_runs_the_template_file(self):
        """Trap two, answered by reading rather than assumed. ``kirocrew agent create
        --kiro-agent kirocrew-team-lead`` stores a NAME, not a copy of the bytes:
        ``KiroCrewAgentConfig.kiro_agent`` is documented as the "Kiro agent name (modeId
        for session/set_mode)" at ``config/sections.py``, so the crewmate resolves the
        template file at start time.

        So there is no crewmate-with-its-own-bytes case to protect on this path, and
        refusing member starts is not collateral damage -- those sessions are precisely
        the ones that would run the hand-edited file. A private COPY is a different
        mechanism: fork/publish lands its own bytes under its own name with a lineage
        record, which is the lane the test above protects."""
        from kiro_crew.config.sections import KiroCrewAgentConfig

        cfg = KiroCrewAgentConfig(kiro_agent="kirocrew-team-lead")
        assert cfg.kiro_agent == "kirocrew-team-lead"
        assert not hasattr(cfg, "spec"), "a crewmate carrying its own spec bytes"
        fields = {f for f in vars(cfg)}
        assert not any("prompt" in f or "allowedTools" in f for f in fields), (
            f"the crewmate record holds spec content, so it is a copy and not a "
            f"reference: {sorted(fields)}"
        )


class _ReachedTheSend(Exception):
    """Raised from the stubbed transport, so a test can prove the gate ADMITTED.

    The alternative -- asserting the method returned -- would need the whole
    post-activation half stubbed too, and would then pass for a body that never
    called the gate at all.
    """


def _runtime_for_mode_activation(monkeypatch, tmp_path, sent, terminated):
    """A real ``AcpRuntime`` positioned so ``_activate_mode_bracketed`` runs for real.

    ``object.__new__`` plus the few collaborators that one method touches, the shape
    ``test_worker_agent.py`` already uses for this bracket. Spawned as ``kirocrew`` and
    switched to another agent, which IS the bypass: the spawn gate is keyed to
    ``self._agent``, so the agent named at activation passed no gate of its own.
    """
    from kiro_crew.acp import runtime as runtime_mod

    rt = object.__new__(runtime_mod.AcpRuntime)
    rt._agent = "kirocrew"
    rt._work_dir = tmp_path
    rt._native_skill_projection = None
    rt._pid = UNALLOCATABLE_PID

    async def _send_and_await(method, params, timeout=None, **kw):
        sent.append(method)
        raise _ReachedTheSend()

    async def _terminate(session_id):
        terminated.append(session_id)

    rt._send_and_await = _send_and_await  # type: ignore[method-assign]
    rt.terminate_session = _terminate  # type: ignore[method-assign]
    return rt


async def _create(rt, agent_name):
    """``create_session`` for *agent_name* with no cwd, so the resolution is exercised.

    ``mcp_servers=[]`` so the caller's own array is taken and no mirror or pool is
    consulted: what this drives is the admission question, not session composition.
    """
    return await rt.create_session(agent=agent_name, mcp_servers=[])


async def _activate(rt, mode_agent):
    """``set_mode`` for *mode_agent* through the one bracketed helper.

    ``wire_registered=True`` on purpose: that path takes the payload's snapshot and
    calls no freshness gate, so what this drives is the admission gate alone.
    """
    return await rt._activate_mode_bracketed(
        "sid-1",
        mode_agent,
        budget=1.0,
        payload_snapshot=None,
        wire_registered=True,
    )


def _runtime_for_native_activation(tmp_path, sent, terminated):
    """A runtime whose ``set_mode`` SUCCEEDS, so the post-activation half runs.

    The sibling above raises at the send, which is right for pinning a refusal that must
    land BEFORE kiro-cli loads the spec. The native-path guard is the opposite half: it
    runs AFTER the host has consumed the spec, so a send that raises never reaches it and
    a test built on that scaffold would pass for a body with no guard in it at all.

    ``_native_skill_projection`` is ``None``, the host that reads the file itself at
    ``set_mode``: there is no alias to translate, so the send goes out as the plain
    request and the consumed bytes are the ones on disk.
    """
    from kiro_crew.acp import runtime as runtime_mod

    rt = object.__new__(runtime_mod.AcpRuntime)
    rt._agent = "kirocrew"
    rt._work_dir = tmp_path
    rt._native_skill_projection = None
    rt._pid = UNALLOCATABLE_PID

    async def _send_and_await(method, params, timeout=None, **kw):
        sent.append(method)
        return {}

    async def _terminate(session_id):
        terminated.append(session_id)

    rt._send_and_await = _send_and_await  # type: ignore[method-assign]
    rt.terminate_session = _terminate  # type: ignore[method-assign]
    return rt


async def _activate_native(rt, mode_agent):
    """``set_mode`` through the bracket on the NATIVE path -- ``wire_registered=False``.

    That flag is the whole difference: the wire path takes the payload's snapshot and the
    generation its caller threaded in, while this one has neither, which is why the
    bracket has to record its own.
    """
    return await rt._activate_mode_bracketed(
        "sid-1",
        mode_agent,
        budget=1.0,
        payload_snapshot=None,
        wire_registered=False,
    )


def _runtime_for_create_session(monkeypatch, work_dir):
    """A real ``AcpRuntime`` whose ``create_session`` reaches the admission question.

    A REAL runtime with a mocked process, the shape ``test_session_start_gate.py`` uses,
    because the question now sits inside the start permit: a hand-built object thin
    enough to stop before the gate would never reach it. ``session/new`` raises the
    sentinel, so an admitted start is observable as "the request went out" and a refused
    one as "it did not" -- which is the property that matters, the refusal landing before
    any session exists in the host.
    """
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.acp import runtime as runtime_mod
    from kiro_crew.acp.types import ACP_BACKEND_KIRO

    rt = runtime_mod.AcpRuntime(work_dir=work_dir)
    proc = MagicMock()
    proc.stdout = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = UNALLOCATABLE_PID
    rt._initialized = True
    rt._expect_mcp_reports = False
    rt._acp_backend = ACP_BACKEND_KIRO
    rt._session_start_timeout = 0.05
    rt._start_collect_timeout = 0.4

    async def _send_and_await(method, params, timeout=None, **kw):
        raise _ReachedTheSend()

    rt._send_and_await = _send_and_await  # type: ignore[method-assign]
    return rt


def _reached_past_the_check():
    """Raised from the first collaborator AFTER the admission check in ``create_session``.

    Asserting the method returned would need its whole body stubbed, and would then pass
    for a body that never asked the question at all.
    """
    raise _ReachedTheSend()


def _hand_edit(tmp_path) -> None:
    target = tmp_path / TEAM_LEAD_AGENT_FILENAME
    spec = json.loads(target.read_text(encoding="utf-8"))
    spec["prompt"] = "mine"
    target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")


class TestOneDecisionAtThreeEnforcementPoints:
    """``team_lead_start_refusal`` is the one decision; three places enforce it.

    ``session/set_mode`` naming an agent activates that agent's spec, and the product
    says in its own words at ``acp/runtime.py`` that nothing else answered for it: "the
    spawn gate is keyed to ``self._agent``, so a SHARED runtime spawned as one agent and
    switched to another on this line passed no gate of its own". So a shared runtime
    spawned as ``kirocrew`` and switched to ``kirocrew-team-lead`` ran our spec past
    every refusal -- hand-edit, drift, project shadow and the Markdown decline. The same
    holds for ``create_session`` on a runtime that is already up.

    THREE enforcement points, ONE decision function: the session-start gate, the mode
    activation and ``create_session``. Each only calls the shared function and refuses --
    no site re-decides anything and no site reads the lineage sidecar itself. A
    divergence can then only be a difference in WHERE the question is asked, which the
    tests below pin, rather than a difference in the answer, which no test could pin.

    NOT ``require_fork_governance`` at the two new sites: that would apply the fork half
    too -- the refresh wait and its timeout -- newly blocking a mode switch to a fork
    whose refresh has not settled, which this finding does not call for.
    """

    @pytest.mark.asyncio
    async def test_one_decision_refuses_through_all_three_paths(self, tmp_path, monkeypatch):
        """THE pin that makes a second and third placement safe. One prepared spec, one
        refusal text, through every way a start can enter: the session-start gate,
        ``create_session`` on a runtime already up, and ``set_mode`` activation on a
        runtime spawned as a DIFFERENT agent. Compared against each other rather than
        against a sentence copied into this test, so the three cannot drift to different
        answers while all three stay green."""
        from kiro_crew.acp.session_handle import AcpRuntimeError

        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        _hand_edit(tmp_path)

        # 1. the session-start gate
        with pytest.raises(agent.ForkGovernanceUnresolved) as gate:
            agent.require_fork_governance("kirocrew-team-lead", tmp_path)

        # 2. mode activation, on a runtime spawned as ``kirocrew``
        sent: list[str] = []
        terminated: list[str] = []
        rt = _runtime_for_mode_activation(monkeypatch, tmp_path, sent, terminated)
        with pytest.raises(AcpRuntimeError) as mode:
            await _activate(rt, "kirocrew-team-lead")

        # 3. create_session, with no cwd given -- the runtime's own work dir
        created_rt = _runtime_for_create_session(monkeypatch, tmp_path)
        with pytest.raises(AcpRuntimeError) as created:
            await _create(created_rt, "kirocrew-team-lead")

        assert str(mode.value) == str(gate.value) == str(created.value), (
            "the enforcement points gave different answers for one spec, which is the "
            f"divergence a single decision function exists to prevent: "
            f"{gate.value!s} / {mode.value!s} / {created.value!s}"
        )
        assert sent == [], "set_mode went out: once it has, kiro-cli loaded the spec"
        assert terminated == ["sid-1"], (
            "session/new already succeeded, so a session that may have activated a spec "
            "this product refuses has to end rather than be unregistered locally"
        )

    @pytest.mark.parametrize("case", ["hand-edit", "drift", "project-shadow", "markdown"])
    @pytest.mark.asyncio
    async def test_every_refusal_reaches_mode_activation(self, tmp_path, monkeypatch, case):
        """All four, not just the one GPT named. They reach this path by CONSTRUCTION --
        the site calls the gate, the gate calls the one decision function -- and this
        drives each of them through it rather than trusting that construction."""
        _stub_environment(tmp_path, monkeypatch)
        if case == "markdown":
            # The decline leaves no JSON at all, so this is the one case with no
            # installed spec: the operator's Markdown file is what would start.
            (tmp_path / "kirocrew-team-lead.md").write_text(
                "---\nname: kirocrew-team-lead\n---\nMine.\n", encoding="utf-8"
            )
            assert agent._install_team_lead_agent() is agent.InstallOutcome.DECLINED
        else:
            assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        if case == "hand-edit":
            _hand_edit(tmp_path)
        if case == "drift":
            monkeypatch.setattr(
                agent, "_may_auto_approve", lambda r: not r.startswith("@kirocrew-core/")
            )
        if case == "project-shadow":
            # Against ``self._work_dir``, which is what the gate's ``project_dir`` means
            # at this site: the cwd kiro-cli resolves ``--agent`` against.
            (tmp_path / ".kiro" / "agents").mkdir(parents=True)
            (tmp_path / ".kiro" / "agents" / TEAM_LEAD_AGENT_FILENAME).write_text(
                json.dumps({"name": "kirocrew-team-lead", "allowedTools": ["execute_bash"]}),
                encoding="utf-8",
            )

        expected = agent.spec_start_refusal(
            "kirocrew-team-lead",
            project_dir=tmp_path if case == "project-shadow" else None,
        )
        assert expected is not None, f"{case} is not refused at all, so this proves nothing"

        sent: list[str] = []
        terminated: list[str] = []
        rt = _runtime_for_mode_activation(monkeypatch, tmp_path, sent, terminated)
        from kiro_crew.acp.session_handle import AcpRuntimeError

        with pytest.raises(AcpRuntimeError) as caught:
            await _activate(rt, "kirocrew-team-lead")
        assert str(caught.value) == expected, f"{case}: {caught.value}"
        assert sent == []
        assert terminated == ["sid-1"]

    @pytest.mark.asyncio
    async def test_a_recorded_fork_still_starts_through_all_three(self, tmp_path, monkeypatch):
        """The fork lane, fifth round running, now on all three paths. The exclusion is
        ONE lineage read inside the shared decision function, so no enforcement point
        rebuilds that answer for itself -- three sidecar reads would be three answers
        that can disagree. Driven by SETTING a real lineage record, not by stubbing a
        predicate, and cleared again as the control."""
        from kiro_crew.acp.session_handle import AcpRuntimeError
        from kiro_crew.acp.types import METHOD_SET_MODE

        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        _hand_edit(tmp_path)
        assert agent.spec_start_refusal("kirocrew-team-lead") is not None

        agent_state.set_fork_info(
            "kirocrew-team-lead", forked_from="kirocrew-conductor", private_to="a-crew"
        )
        import kiro_crew.agent_materialization.fork_refresh as fr

        fr._fork_refresh_settled.set()
        monkeypatch.setattr(fr, "_fork_refresh_failed", frozenset())

        sent: list[str] = []
        terminated: list[str] = []
        rt = _runtime_for_mode_activation(monkeypatch, tmp_path, sent, terminated)
        created_rt = _runtime_for_create_session(monkeypatch, tmp_path)

        # Admitted on all three: the gate takes its own fork path, and both ACP sites
        # get past the question to the work they do next.
        agent.require_fork_governance("kirocrew-team-lead", tmp_path)
        with pytest.raises(_ReachedTheSend):
            await _activate(rt, "kirocrew-team-lead")
        assert sent == [METHOD_SET_MODE], "the activation never reached the transport"
        with pytest.raises(_ReachedTheSend):
            await _create(created_rt, "kirocrew-team-lead")

        # Control: clear the record and the SAME bytes are refused on all three, so the
        # record is the only thing that moves the answer at any of them.
        agent_state.clear_fork_info("kirocrew-team-lead")
        sent.clear()
        with pytest.raises(agent.ForkGovernanceUnresolved):
            agent.require_fork_governance("kirocrew-team-lead", tmp_path)
        with pytest.raises(AcpRuntimeError):
            await _activate(rt, "kirocrew-team-lead")
        assert sent == []
        with pytest.raises(AcpRuntimeError):
            await _create(created_rt, "kirocrew-team-lead")

    @pytest.mark.asyncio
    async def test_any_other_agent_is_unaffected_at_all_three(self, tmp_path, monkeypatch):
        """THE over-tight control. A call made unconditional -- or one asking about the
        runtime's own agent rather than the selected one -- would gate every mode switch
        and every session create in the product. Driven with the team-lead spec
        hand-edited and refusable on disk, so admitting here is each site answering
        about the agent it was given and not about whatever else is installed."""
        from kiro_crew.acp.types import METHOD_SET_MODE

        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        _hand_edit(tmp_path)
        assert agent.spec_start_refusal("kirocrew-team-lead") is not None

        sent: list[str] = []
        terminated: list[str] = []
        rt = _runtime_for_mode_activation(monkeypatch, tmp_path, sent, terminated)
        created_rt = _runtime_for_create_session(monkeypatch, tmp_path)
        for other in ("kirocrew", "kirocrew-worker", "some-operator-agent"):
            agent.require_fork_governance(other, tmp_path)
            sent.clear()
            with pytest.raises(_ReachedTheSend):
                await _activate(rt, other)
            assert sent == [
                METHOD_SET_MODE
            ], f"activating {other!r} was refused, so this gates every agent switch"
            with pytest.raises(_ReachedTheSend):
                await _create(created_rt, other)

    @pytest.mark.asyncio
    async def test_renaming_the_declared_name_inside_our_file_does_not_admit_it(
        self, tmp_path, monkeypatch
    ):
        """THE axis three rounds of name-keyed checks kept reopening: the name is a field
        INSIDE the bytes under suspicion, and the operator controls it.

        Edit ``name`` in the file this installer wrote to anything else and the file is
        still there, its ungoverned grants still live. ``agent_spec_path`` prefers a
        DECLARED name over the filename, so discovery advertises the new name and the
        backend runs those same bytes -- while a check asking "is the requested name
        ours?" answers about an agent nobody is starting. What this product owns is the
        PATH: it wrote that filename and recorded that file's digest. So the question is
        whether the spec that will run is that file, and it is asked at all three
        enforcement points."""
        from kiro_crew.acp.session_handle import AcpRuntimeError

        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        spec = json.loads(target.read_text(encoding="utf-8"))
        spec["name"] = "renamed-lead"
        spec["prompt"] = "mine"
        target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")

        # The premise, not assumed: resolution really does land on our file for the new
        # name, which is what makes those bytes the ones that run.
        assert agent.agent_spec_path("renamed-lead") == target

        # Name-keyed alone admits it -- the bypass, stated as the reason the path matters.
        assert agent.spec_start_refusal("renamed-lead") is None

        sent: list[str] = []
        terminated: list[str] = []
        rt = _runtime_for_mode_activation(monkeypatch, tmp_path, sent, terminated)
        created_rt = _runtime_for_create_session(monkeypatch, tmp_path)

        with pytest.raises(agent.ForkGovernanceUnresolved, match="will not run it"):
            agent.require_fork_governance("renamed-lead", tmp_path)
        with pytest.raises(AcpRuntimeError, match="will not run it"):
            await _activate(rt, "renamed-lead")
        assert sent == [], "set_mode went out for a spec this product refuses"
        with pytest.raises(AcpRuntimeError, match="will not run it"):
            await _create(created_rt, "renamed-lead")

    @pytest.mark.asyncio
    async def test_the_path_question_admits_every_agent_that_is_not_our_file(
        self, tmp_path, monkeypatch
    ):
        """The over-tight control for the path key. Resolution reaches our filename by
        two routes only -- a file DECLARING the requested name, and the stem fallback,
        which is this spec's own name -- so no other agent can be caught by it. Driven
        with our file renamed (the state the refusal above fires on) AND with other
        agents installed beside it, including one whose own file declares its own name,
        so admitting is the question answering rather than nothing being there to find."""
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        spec = json.loads(target.read_text(encoding="utf-8"))
        spec["name"] = "renamed-lead"
        target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        (tmp_path / "some-operator-agent.json").write_text(
            json.dumps({"name": "some-operator-agent", "allowedTools": ["execute_bash"]}),
            encoding="utf-8",
        )
        (tmp_path / "their-notes.md").write_text(
            "---\nname: their-lead\n---\ntheirs\n", encoding="utf-8"
        )

        for other in ("some-operator-agent", "their-lead", "kirocrew-worker", "kirocrew"):
            assert agent.team_lead_start_refusal(other, tmp_path) is None, (
                f"{other!r} was refused because OUR file is renamed, so one tampered "
                f"spec takes down every other agent"
            )

    @pytest.mark.asyncio
    async def test_a_recorded_fork_is_still_exempt_under_a_renamed_declared_name(
        self, tmp_path, monkeypatch
    ):
        """The fork lane, on the path key too. The exemption is asked BEFORE the path
        question, so a crew's private copy is not caught by it either."""
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        spec = json.loads(target.read_text(encoding="utf-8"))
        spec["name"] = "renamed-lead"
        spec["prompt"] = "the crew's own charter"
        target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        assert agent.team_lead_start_refusal("renamed-lead", tmp_path) is not None

        agent_state.set_fork_info(
            "renamed-lead", forked_from="kirocrew-team-lead", private_to="a-crew"
        )
        import kiro_crew.agent_materialization.fork_refresh as fr

        fr._fork_refresh_settled.set()
        monkeypatch.setattr(fr, "_fork_refresh_failed", frozenset())
        assert agent.team_lead_start_refusal("renamed-lead", tmp_path) is None

        agent_state.clear_fork_info("renamed-lead")
        assert agent.team_lead_start_refusal("renamed-lead", tmp_path) is not None

    def test_the_healthy_install_answers_the_path_question_without_a_scan(
        self, tmp_path, monkeypatch
    ):
        """Its cost is part of the contract: this runs on EVERY start of EVERY agent. A
        file declaring its own name can be selected by one route alone -- its stem, which
        the caller already asked about -- so the healthy install answers on one bounded
        read and never resolves. Pinned by COUNTING the resolver's calls rather than by
        making it raise: the path question swallows a failed resolution by design, so a
        probe that raised would be caught by that handler and prove nothing."""
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN

        real = agent.agent_spec_path
        calls: list[tuple] = []

        def _counting(*a, **k):
            calls.append(a)
            return real(*a, **k)

        monkeypatch.setattr(agent, "agent_spec_path", _counting)
        for other in ("kirocrew", "kirocrew-worker", "some-operator-agent"):
            assert agent.team_lead_start_refusal(other, tmp_path) is None
        assert agent.team_lead_start_refusal("kirocrew-team-lead", tmp_path) is None
        assert calls == [], (
            "a healthy install resolves the whole agents directory on every start of "
            f"every agent: {calls}"
        )

    def test_an_unreadable_spec_of_ours_does_not_break_every_other_agent(
        self, tmp_path, monkeypatch
    ):
        """The blast radius of asking the path question on EVERY start of EVERY agent.

        Deciding whether our file is what would run means reading our file, and that read
        raises the reader's transient class for an EACCES or a looping symlink -- a class
        neither start path maps, so an exception out of here fails an unrelated agent's
        start with an unmapped error. One unreadable spec would take down every agent.

        Answered "not ours" instead, which admits nothing dangerous: a start asking for
        THIS spec's own name never reaches that question, and an unreadable file at this
        name is refused fail-closed by the decision function itself, as the second half
        of this test shows."""
        import os

        _stub_environment(tmp_path, monkeypatch)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        os.symlink(target, target)  # a loop: every read of it gives ELOOP

        for other in ("kirocrew", "kirocrew-worker", "some-operator-agent"):
            assert (
                agent.team_lead_start_refusal(other, tmp_path) is None
            ), f"starting {other!r} broke because OUR spec is unreadable"
            agent.require_fork_governance(other, tmp_path)

        refusal = agent.team_lead_start_refusal("kirocrew-team-lead", tmp_path)
        assert refusal is not None, "an unreadable spec at our own name was admitted"

    @pytest.mark.asyncio
    async def test_the_dashboard_patch_leaves_the_crewmate_startable(self, tmp_path, monkeypatch):
        """The documented customization path must not brick the agent it customizes.

        A ``model``/``skills`` PATCH rewrites the spec in place, so the bytes stop matching
        the recorded ownership digest. Without renewing it, the admission gate refuses every
        later start as hand-edited and the installer declines the file as somebody else's --
        permanently, for an edit the dashboard invites. The renewal is the per-stem TABLE
        ``reset_agent_model`` already uses, so a second owned stem is one entry rather than a
        second branch."""
        from unittest.mock import MagicMock

        from aiohttp import web

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        _stub_environment(tmp_path, monkeypatch)
        # The handler resolves the agents dir through the composed facade function, which
        # reads this override; the installer above is pinned by ``_stub_environment``.
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", tmp_path)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        before = target.read_text(encoding="utf-8")
        assert agent.spec_start_refusal("kirocrew-team-lead") is None

        request = MagicMock(spec=web.Request)
        request.method = "PATCH"
        request.match_info = {"name": "kirocrew-team-lead"}
        request.app = {"state": MagicMock()}

        async def _json():
            return {"model": "claude-new"}

        request.json = _json
        resp = await api_agent_detail(request)
        assert resp.status == 200, resp.status

        after = json.loads(target.read_text(encoding="utf-8"))
        assert after["model"] == "claude-new", "the PATCH did not reach the file"
        assert target.read_text(encoding="utf-8") != before
        assert agent.spec_start_refusal("kirocrew-team-lead") is None, (
            "an authorized model PATCH left the crewmate unstartable, which is the "
            "documented customization path breaking the agent permanently"
        )
        assert agent._install_team_lead_agent() is not agent.InstallOutcome.DECLINED, (
            "the installer now reads its own spec as somebody else's, so it stops "
            "re-filtering those grants against a tightened ceiling"
        )

    @pytest.mark.asyncio
    async def test_a_patched_skill_plus_a_relocation_leaves_the_crewmate_startable(
        self, tmp_path, monkeypatch
    ):
        """The two-step path, composed through the real functions rather than argued about.

        Step one: a dashboard skills PATCH maps a skill, so the spec grows a ``skill://``
        resource it did not ship with. Step two: that builtin skill relocates, and
        ``migrate_relocated_skill_uris`` rewrites the spec on EVERY rebuild to point at the
        new path. Each step is an ordinary user action or an ordinary upgrade; together they
        made a managed writer rewrite this spec while the renewal looked only at
        dashboard-author's stem, so the bytes stopped matching the recorded digest and every
        later start was refused as hand-edited.

        Both steps run for real: the HTTP handler, its skill mapping, and the migration. The
        catalog WALK is the one seam stubbed -- it enumerates real skill roots, and which
        directories exist is not what this test is about."""
        from unittest.mock import MagicMock

        from aiohttp import web

        from kiro_crew import skills as skills_mod
        from kiro_crew.dashboard.handlers import agents as agents_handlers

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        _stub_environment(tmp_path, monkeypatch)
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", tmp_path)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME

        # The relocation this upgrade performs: the old SKILL.md is gone, the new one is
        # installed. The migration counts a move only in that state.
        skills_root = tmp_path / "skills"
        (skills_root / "new-skill").mkdir(parents=True)
        (skills_root / "new-skill" / "SKILL.md").write_text("# moved\n", encoding="utf-8")
        old_md = skills_root / "old-skill" / "SKILL.md"
        monkeypatch.setattr(skills_mod, "skills_dir", lambda: skills_root)
        monkeypatch.setattr(skills_mod, "_RELOCATED_SKILLS", {"old-skill": "new-skill"})
        # The one seam: the catalog WALK. ``apply_skill_mapping`` resolves its own catalog
        # through ``walk_skill_catalog`` in the shared module, so that is what is stubbed --
        # which skill directories exist is not what this test is about, and everything that
        # consumes the catalog stays real.
        from kiro_crew.dashboard.handlers import _shared as shared_mod

        snapshot = shared_mod.SkillCatalogSnapshot(
            entries={"old-skill": old_md}, dir_mtimes={}, walked_at_ns=0
        )
        monkeypatch.setattr(shared_mod, "walk_skill_catalog", lambda *a, **k: snapshot)
        monkeypatch.setattr(
            agents_handlers, "enumerate_skill_catalog", lambda *a, **k: {"old-skill": old_md}
        )

        # STEP ONE, through the real handler: map the skill onto the crewmate.
        request = MagicMock(spec=web.Request)
        request.method = "PATCH"
        request.match_info = {"name": "kirocrew-team-lead"}
        request.app = {"state": MagicMock()}

        async def _json():
            return {"skills": ["old-skill"]}

        request.json = _json
        resp = await api_agent_detail(request)
        assert resp.status == 200, resp.status
        patched = json.loads(target.read_text(encoding="utf-8"))
        assert any(
            str(r).startswith("skill://") for r in patched.get("resources", [])
        ), f"the PATCH mapped no skill, so step one did not happen: {patched.get('resources')}"
        assert agent.spec_start_refusal("kirocrew-team-lead") is None, "step one alone bricked it"

        # STEP TWO, through the real migration.
        assert agent.migrate_relocated_skill_uris() >= 1, "the migration rewrote nothing"
        migrated = json.loads(target.read_text(encoding="utf-8"))
        assert any(
            "new-skill" in str(r) for r in migrated.get("resources", [])
        ), f"the URI was not migrated: {migrated.get('resources')}"

        assert agent.spec_start_refusal("kirocrew-team-lead") is None, (
            "the two steps compose into a bricked crewmate: a mapped skill, then its "
            "relocation, and every later start is refused as hand-edited"
        )
        assert agent._install_team_lead_agent() is not agent.InstallOutcome.DECLINED

    def test_the_hook_sweep_is_keyed_by_the_table_and_is_dormant_today(self, tmp_path, monkeypatch):
        """The same one-line keying error as the skill-URI migration, fixed the same way.

        DORMANT, and the control says so rather than leaving a later reader to assume it was
        live: the sweep removes only ``auto_approve_tools``, and this installer never writes
        that key, so a shipped crewmate spec has nothing for it to strip. What is fixed is
        the keying -- when it does fire on an owned stem, it renews that stem's digest
        instead of dashboard-author's alone."""
        from kiro_crew.agent_materialization import kiro_hooks

        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        shipped = json.loads(target.read_text(encoding="utf-8"))
        assert not set(shipped.get("hooks") or {}) & set(kiro_hooks._LEGACY_KIROCREW_HOOK_KEYS), (
            "the installer writes a legacy hook key, so the sweep is NOT dormant and this "
            "test is the wrong shape for it"
        )

        # Plant the legacy key the sweep exists for, keeping the spec confirmed as ours, and
        # drive the real sweep.
        planted = json.loads(target.read_text(encoding="utf-8"))
        planted["hooks"] = {**(planted.get("hooks") or {}), "auto_approve_tools": ["fs_write"]}
        target.write_text(json.dumps(planted, indent=2) + "\n", encoding="utf-8")
        agent_state.set_managed_digest("kirocrew-team-lead", agent_state.spec_digest(planted))
        assert agent.spec_start_refusal("kirocrew-team-lead") is None

        agent._sanitize_agent_hooks()

        swept = json.loads(target.read_text(encoding="utf-8"))
        assert "auto_approve_tools" not in (swept.get("hooks") or {}), "the sweep did not run"
        assert (
            agent.spec_start_refusal("kirocrew-team-lead") is None
        ), "the sweep rewrote our spec and renewed nothing, so every later start is refused"

    def test_no_renewal_site_stamps_a_spec_that_is_not_ours(self, tmp_path, monkeypatch):
        """THE laundering control, at every site this change touches, and it matters more
        than the brick: a renewal that fired for a file this product did not write would
        hand a user-authored spec the provenance the installer refused it.

        Driven with a spec at our stem that is NOT ours -- no recorded digest -- through
        each in-place writer. None of them may record one."""
        _stub_environment(tmp_path, monkeypatch)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        theirs = {
            "name": "kirocrew-team-lead",
            "prompt": "mine",
            "hooks": {"auto_approve_tools": ["fs_write"]},
            "resources": ["skill://" + str(tmp_path / "skills" / "old-skill" / "SKILL.md")],
            "model_managed": True,
        }
        target.write_text(json.dumps(theirs, indent=2) + "\n", encoding="utf-8")
        assert not agent_state.get_managed_digest("kirocrew-team-lead")

        from kiro_crew import skills as skills_mod

        skills_root = tmp_path / "skills"
        (skills_root / "new-skill").mkdir(parents=True)
        (skills_root / "new-skill" / "SKILL.md").write_text("# moved\n", encoding="utf-8")
        monkeypatch.setattr(skills_mod, "skills_dir", lambda: skills_root)
        monkeypatch.setattr(skills_mod, "_RELOCATED_SKILLS", {"old-skill": "new-skill"})

        agent.migrate_relocated_skill_uris()
        agent._sanitize_agent_hooks()
        agent.migrate_agent_specs()

        assert not agent_state.get_managed_digest("kirocrew-team-lead"), (
            "a writer stamped a user file at this stem, which LAUNDERS their spec into one "
            "this product vouches for"
        )
        assert (
            agent.spec_start_refusal("kirocrew-team-lead") is not None
        ), "their spec at our name is admitted after a sweep touched it"

    def test_both_new_sites_enforce_and_neither_decides(self):
        """What keeps three placements in step, as a source invariant rather than a
        promise: each new body CALLS the shared function and does nothing else about it.

        No copy of the decision (``spec_start_refusal``), no lineage read of its own
        (``get_fork_info``), and not ``require_fork_governance`` either -- that one
        carries the fork refresh wait, which at these sites would newly block a mode
        switch or a session create on a fork whose refresh has not settled.

        The activation call also sits AHEAD of the branch that rations snapshots, so it
        runs on the wire-registered path too."""
        import ast
        import inspect
        import textwrap

        from kiro_crew.acp import runtime as runtime_mod

        for method in (
            runtime_mod.AcpRuntime._activate_mode_bracketed,
            runtime_mod.AcpRuntime.create_session,
        ):
            fn = ast.parse(textwrap.dedent(inspect.getsource(method))).body[0]
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            assert "team_lead_start_refusal" in names, (
                f"{method.__name__} asks nobody whether this product will run the agent "
                f"it is about to start"
            )
            for copied in (
                "require_fork_governance",
                "spec_start_refusal",
                "get_fork_info",
                "team_lead_agent",
            ):
                assert copied not in names, (
                    f"{method.__name__} reaches for {copied}, so there is a second "
                    f"decision rather than one function called from three places"
                )

        activation = ast.parse(
            textwrap.dedent(inspect.getsource(runtime_mod.AcpRuntime._activate_mode_bracketed))
        ).body[0]
        body = [
            st
            for st in activation.body
            if not isinstance(st, (ast.Import, ast.ImportFrom, ast.Expr))
        ]

        def _at(target):
            for k, st in enumerate(body):
                if target in {n.id for n in ast.walk(st) if isinstance(n, ast.Name)}:
                    return k
            raise AssertionError(f"{target} not found")

        assert _at("team_lead_start_refusal") < _at("wire_registered"), (
            "the question sits inside the branch that rations snapshots, so one of the "
            "two consumption paths activates unasked"
        )

    @pytest.mark.asyncio
    async def test_create_session_asks_about_the_cwd_it_resolved_not_the_one_it_was_given(
        self, tmp_path, monkeypatch
    ):
        """``cwd`` is optional at this entry point, and ``None`` is the common case.

        ``_session_work_dir(None)`` resolves to the runtime's own ``_work_dir`` -- the cwd
        the backend process runs with -- so that resolved value is the one kiro-cli
        resolves ``--agent`` against and the only one the project-shadow half can be
        asked about. Asking with the raw parameter would check nothing at all for every
        caller that passes no cwd, which is the silent failure this pins: the shadow
        below lives in ``_work_dir`` and ``cwd`` is never given."""
        from kiro_crew.acp.session_handle import AcpRuntimeError

        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        work = tmp_path / "wd"
        (work / ".kiro" / "agents").mkdir(parents=True)
        (work / ".kiro" / "agents" / TEAM_LEAD_AGENT_FILENAME).write_text(
            json.dumps({"name": "kirocrew-team-lead", "allowedTools": ["execute_bash"]}),
            encoding="utf-8",
        )
        # Control: the GLOBAL spec on its own admits, so the refusal is the shadow.
        assert agent.spec_start_refusal("kirocrew-team-lead") is None

        rt = _runtime_for_create_session(monkeypatch, work)

        with pytest.raises(AcpRuntimeError) as caught:
            await _create(rt, "kirocrew-team-lead")
        assert ".kiro/agents" in str(caught.value), caught.value

        # And the other half: with no shadow it gets PAST the check rather than
        # refusing everything, so this is not a guard that blocks every create.
        for entry in (work / ".kiro" / "agents").iterdir():
            entry.unlink()
        with pytest.raises(_ReachedTheSend):
            await _create(rt, "kirocrew-team-lead")


#: The enforcement sites for this refusal, pinned by exact equality.
#:
#: WHY THIS TABLE EXISTS. Nothing else in the tree owns the LIST of places a spec can
#: start from: there is no registry of start paths, no docstring that claims one is
#: complete, and
#: ``test_acp_harness_contract``'s ``kiro_gates_pass`` proves the gates that ARE called
#: work rather than that every path calls one. A spec can also start without passing the
#: session-start gate at all -- ``create_session`` on a live runtime, and the
#: ``set_mode`` activation a shared runtime reaches when it is switched to another agent.
#: So the set itself is the artifact worth pinning.
#:
#: WHAT IT CATCHES: a site REMOVED, a site MOVED to another function, a site ADDED without this table being updated -- and the
#: decision staying reachable from exactly one place, which is what makes three
#: enforcement points safe.
#:
#: WHAT IT OWNS IS THE SITE LIST, and a site list is not the whole of this refusal
#: being right: one finding here was not a missing site at all but the right site asking
#: the wrong question -- keying on the requested NAME, a field inside the bytes under
#: suspicion, rather than on the resolved path. The last test below pins the key for
#: that reason; the table cannot.
#:
#: WHAT IT CANNOT DO, said plainly so a later reader does not mistake it for proof of
#: completeness: it pins the sites that EXIST. It cannot discover a start path nobody
#: has found yet. A new way to activate a spec, added in a module this table does not
#: name, passes this test and ships the same class of hole. What it buys is that the
#: set cannot drift in silence, not that the set is whole.
#:
#: Modelled on ``test_agent_spec_hardened_reads.TestCallSiteLabelRatchet``, the
#: call-site ratchet this branch already registered an entry in: same exact-equality
#: table keyed by source path, same ``source_corpus`` scan, and the same reason for
#: counting a BY-REFERENCE hand-off -- both ACP sites pass the callee to
#: ``asyncio.to_thread``, which produces no ``Call`` node named after it, so matching
#: direct calls alone would leave every off-loop enforcement point invisible here. The
#: value is the ENCLOSING function rather than a label pair, because what this ratchet
#: is about is which code path asks, and a site moved from one function to another is
#: exactly the drift it has to catch.
_ENFORCEMENT_SITES: dict[str, dict[str, list[str]]] = {
    # The three enforcement points. Each asks about the agent that will actually run --
    # the gate about the one being started, ``create_session`` about its ``agent``
    # parameter, the activation about ``mode_agent`` -- and each only calls and refuses.
    "team_lead_start_refusal": {
        "kiro_crew/acp/runtime.py": ["_activate_mode_bracketed", "create_session"],
        "kiro_crew/agent.py": ["require_fork_governance"],
    },
    # The decision itself, reached from ONE place: the shared function above. A second
    # entry here would mean a caller deciding for itself, which is how the fork
    # exclusion and the project directory come apart between sites.
    "spec_start_refusal": {
        "kiro_crew/agent_materialization/team_lead_agent.py": ["team_lead_start_refusal"],
    },
}


def _refusal_call_sites(target: str) -> dict[str, list[str]]:
    """Every site that calls *target*, as ``path -> enclosing function names``.

    A site that hands *target* off BY REFERENCE counts: ``asyncio.to_thread(target, ...)``
    produces no ``Call`` node named after it, and both ACP enforcement points are that
    shape, so direct-call matching alone would see neither. Positional only, which is how
    a callee is passed in these shapes.

    Attributed to the INNERMOST enclosing function, so a call in a nested helper names
    the helper rather than everything around it.
    """
    import ast

    src = source_corpus.src_root().parent

    def _is_site(call, name):
        func = call.func
        called = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute) else ""
        )
        if called == name:
            return True
        return any(isinstance(arg, ast.Name) and arg.id == name for arg in call.args)

    def _descend(node, enclosing, found):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _descend(child, child.name, found)
                continue
            if isinstance(child, ast.Call) and _is_site(child, target):
                found.add(enclosing)
            _descend(child, enclosing, found)

    sites: dict[str, list[str]] = {}
    for path, _text, tree in source_corpus.parsed_candidates(require_any=(target,)):
        found: set[str] = set()
        _descend(tree, "<module>", found)
        if found:
            sites[path.relative_to(src).as_posix()] = sorted(found)
    return sites


class TestTheEnforcementSiteSetIsPinned:
    """One test whose whole job is the LIST of places this refusal is enforced."""

    @pytest.mark.parametrize("target", sorted(_ENFORCEMENT_SITES))
    def test_the_sites_are_exactly_the_pinned_set(self, target):
        assert _refusal_call_sites(target) == _ENFORCEMENT_SITES[target], (
            f"the set of places that call {target} changed. If a start path was ADDED, "
            f"add it here with the agent and work_dir it asks about; if one was removed, "
            f"that is the hole this pin exists to catch"
        )

    def test_the_decision_is_reached_from_exactly_one_place(self):
        """The invariant that makes three enforcement points safe rather than three
        chances to disagree: ``spec_start_refusal`` has ONE caller, so a divergence
        between the sites can only be a difference in where the question is asked."""
        callers = _refusal_call_sites("spec_start_refusal")
        assert sum(len(v) for v in callers.values()) == 1, callers

    def test_the_refusal_is_keyed_on_the_resolved_path_not_only_on_the_name(self):
        """So this cannot regress to a name check. The requested name is a field inside
        the file being judged and the operator controls it, so the shared decision has to
        ask the real resolver whether the spec that will run is OUR file. Pinned as
        source: the shared function reaches ``agent_spec_path``, and it compares a
        resolved path rather than taking the resolver's answer on trust."""
        import ast
        import inspect
        import textwrap

        from kiro_crew.agent_materialization import team_lead_agent as tla

        reached = set()
        for fn_name in ("team_lead_start_refusal", "_our_file_is_what_would_run"):
            fn = ast.parse(textwrap.dedent(inspect.getsource(getattr(tla, fn_name)))).body[0]
            reached |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            reached |= {
                n.attr
                for n in ast.walk(fn)
                if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)
            }
        assert "agent_spec_path" in reached, (
            "the decision no longer asks which FILE a start resolves to, so renaming the "
            "declared name inside our own spec admits it again"
        )
        assert "_our_file_is_what_would_run" in reached, "the path question is unreachable"

    def test_every_enforcement_site_asks_about_a_selected_agent(self):
        """Not the runtime's own agent. Each site's first argument must be the agent that
        will RUN: the gate's parameter, ``create_session``'s ``agent``, the activation's
        ``mode_agent``. ``self._agent`` there is the bug this change closed -- a shared
        runtime is spawned as one agent and switched to another."""
        import ast
        import inspect
        import textwrap

        from kiro_crew.acp import runtime as runtime_mod

        expected = {
            "_activate_mode_bracketed": "mode_agent",
            "create_session": "agent",
        }
        for method_name, want in expected.items():
            fn = ast.parse(
                textwrap.dedent(inspect.getsource(getattr(runtime_mod.AcpRuntime, method_name)))
            ).body[0]
            asked = [
                node.args[1].id
                for node in ast.walk(fn)
                if isinstance(node, ast.Call)
                and any(
                    isinstance(a, ast.Name) and a.id == "team_lead_start_refusal" for a in node.args
                )
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Name)
            ]
            assert asked == [want], f"{method_name} asks about {asked}, not [{want!r}]"


#: Every site that renews a managed spec's ownership digest, pinned by exact equality.
#:
#: WHY. The same one-line keying error -- asking about ONE owned stem instead of the
#: per-stem table -- shipped in three separate writers and was found one round apart each
#: time. The set of places that renew a digest is therefore a set worth owning, and the
#: value says which RULE each site keys on, because that is the thing that was wrong
#: rather than the site's existence.
#:
#: ``True`` means the site keys through ``_confirms_managed_pre_write``, the per-stem
#: table. Every IN-PLACE writer must: it rewrites a file somebody else's installer may own,
#: so it has to ask whose the PRE-write bytes were, and the answer has to cover every owned
#: stem rather than one. ``False`` is for the two INSTALLERS, which write their own bytes
#: and have no pre-write to confirm -- the digest they record is of what they just
#: generated.
#:
#: WHAT IT CANNOT DO, said plainly: it pins the sites that RENEW, discovered through the
#: ``begin_managed_write`` identifier. It cannot find an in-place writer that renews
#: NOTHING -- and there is no syntactic signature for one, because "writes a managed spec"
#: looks exactly like "installs a generated spec" at the call site (both are
#: ``_atomic_json_write``), so a scan keyed on the write would flag every installer in the
#: tree. The closest expressible thing is this table plus the fact that the tree now
#: contains no in-place writer of a managed spec that renews nothing: all five ask the
#: table, so "renews nothing" is a shape a reviewer can look for rather than a state the
#: code is already in. A sixth writer added tomorrow that renews correctly fails this test
#: until it is listed; one that renews nothing is invisible to it, and that is the limit.
_DIGEST_RENEWAL_SITES: dict[str, dict[str, bool]] = {
    "kiro_crew/agent.py": {
        "_sanitize_agent_hooks": True,
        "migrate_agent_specs": True,
        "migrate_relocated_skill_uris": True,
        "reset_agent_model": True,
    },
    "kiro_crew/agent_materialization/team_lead_agent.py": {"_install_team_lead_agent": False},
    "kiro_crew/agent_materialization/worker_agent.py": {"_write_dashboard_author_spec": False},
    "kiro_crew/dashboard/agent_admin/agent_detail.py": {"_locked_overwrite": True},
}


def _digest_renewal_sites() -> dict[str, dict[str, bool]]:
    """``path -> {enclosing function: keys the renewal through the per-stem table}``.

    Attributed to the innermost enclosing function, like the enforcement-site scan.
    """
    import ast

    src = source_corpus.src_root().parent
    found: dict[str, dict[str, bool]] = {}

    def _calls(node, enclosing, out):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _calls(child, child.name, out)
                continue
            if isinstance(child, ast.Call):
                fn = child.func
                name = (
                    fn.id
                    if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else ""
                )
                if name == "begin_managed_write":
                    out.add(enclosing)
            _calls(child, enclosing, out)

    for path, _text, tree in source_corpus.parsed_candidates(require_any=("begin_managed_write",)):
        renewing: set[str] = set()
        _calls(tree, "<module>", renewing)
        if not renewing:
            continue
        by_name: dict[str, bool] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in renewing:
                continue
            reached = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
                n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
            }
            by_name[node.name] = "_confirms_managed_pre_write" in reached
        found[path.relative_to(src).as_posix()] = by_name
    return found


class TestTheDigestRenewalSitesArePinned:
    """One test whose whole job is the LIST of places a managed digest is renewed."""

    def test_the_renewal_sites_are_exactly_the_pinned_set(self):
        assert _digest_renewal_sites() == _DIGEST_RENEWAL_SITES, (
            "the set of places that renew a managed spec's digest changed. A new in-place "
            "writer must key through _confirms_managed_pre_write and be listed here; a "
            "writer that stopped renewing is the defect this pin exists to catch"
        )

    def test_every_in_place_writer_keys_on_the_table_not_one_stem(self):
        """The CLASS this pins, rather than the three instances of it. An in-place writer
        that asks about one owned stem answers "not mine" for every other owned stem, so it
        rewrites those specs and leaves them matching no recorded digest -- which the
        admission gate reports as hand-edited and the installer declines permanently."""
        sites = _digest_renewal_sites()
        installers = {
            "kiro_crew/agent_materialization/team_lead_agent.py",
            "kiro_crew/agent_materialization/worker_agent.py",
        }
        for path, functions in sites.items():
            for fn_name, keyed_by_table in functions.items():
                if path in installers:
                    assert not keyed_by_table, (
                        f"{path}:{fn_name} is an installer: it writes its own bytes, so "
                        f"there is no pre-write owner to confirm"
                    )
                    continue
                assert keyed_by_table, (
                    f"{path}:{fn_name} rewrites a managed spec in place without asking the "
                    f"per-stem table, so it renews for at most one owned stem"
                )


class TestARebuildKeepsTheOwnerSavesAndStillRegenerates:
    """What a non-clean rebuild owes an operator who customized the crewmate.

    Keeping a patched spec attributable is only half a feature: the next rebuild then
    recognises the file as its own and rewrites it from the template, so a model pin and a
    saved skill disappear while the sidecar still records the model as picked. The other
    half is carrying those two fields over -- and ONLY those two, because every governed
    field has to keep regenerating or an edit becomes a way to hold a revoked grant.

    Shaped after ``worker_agent._install_dashboard_author_agent``, which already does this;
    this installer was the only one carrying nothing.
    """

    @pytest.mark.asyncio
    async def test_a_rebuild_keeps_the_pin_and_the_skill_while_still_revoking_a_grant(
        self, tmp_path, monkeypatch
    ):
        """Both halves in one test, because the point is that they hold together.

        The model and the skill arrive through the REAL dashboard saves, survive a
        non-clean rebuild -- and on that same pass a grant the ceiling has since revoked is
        removed from the spec. A carry-forward that kept governance alive would pass the
        first half and fail the second."""
        from unittest.mock import MagicMock

        from aiohttp import web

        from kiro_crew.dashboard.handlers import _shared as shared_mod
        from kiro_crew.dashboard.handlers import agents as agents_handlers

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        _stub_environment(tmp_path, monkeypatch)
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", tmp_path)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME

        skill_md = tmp_path / "skills" / "a-skill" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("# a skill\n", encoding="utf-8")
        snapshot = shared_mod.SkillCatalogSnapshot(
            entries={"a-skill": skill_md}, dir_mtimes={}, walked_at_ns=0
        )
        monkeypatch.setattr(shared_mod, "walk_skill_catalog", lambda *a, **k: snapshot)
        monkeypatch.setattr(
            agents_handlers, "enumerate_skill_catalog", lambda *a, **k: {"a-skill": skill_md}
        )

        async def _save(body):
            request = MagicMock(spec=web.Request)
            request.method = "PATCH"
            request.match_info = {"name": "kirocrew-team-lead"}
            request.app = {"state": MagicMock()}

            async def _json():
                return body

            request.json = _json
            resp = await api_agent_detail(request)
            assert resp.status == 200, resp.status

        await _save({"model": "a-pinned-model"})
        await _save({"skills": ["a-skill"]})

        saved = json.loads(target.read_text(encoding="utf-8"))
        assert saved["model"] == "a-pinned-model"
        assert any(str(r).startswith("skill://") for r in saved["resources"]), saved["resources"]
        # The pin is the OWNER's, recorded where the dashboard records it.
        assert agent_state.get_model_managed("kirocrew-team-lead") is False
        granted_before = set(saved["allowedTools"])
        revoked = next(r for r in granted_before if r.startswith("@kirocrew-core/"))

        # The ceiling moves under the spec, then an ORDINARY (non-clean) rebuild runs.
        monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: ref != revoked)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN

        rebuilt = json.loads(target.read_text(encoding="utf-8"))
        assert rebuilt["model"] == "a-pinned-model", "the rebuild discarded the owner's pin"
        assert any(
            str(r).startswith("skill://") for r in rebuilt["resources"]
        ), f"the rebuild discarded the saved skill: {rebuilt['resources']}"
        assert revoked not in set(rebuilt["allowedTools"]), (
            "the carry-forward kept a grant the ceiling revoked, which makes an owner edit "
            "a way to hold governance still"
        )
        assert (
            agent.spec_start_refusal("kirocrew-team-lead") is None
        ), "the rebuilt spec does not confirm as ours, so every later start is refused"

    def test_without_an_owner_pin_the_template_model_wins(self, tmp_path, monkeypatch):
        """THE control that keeps the fix from freezing the model. With no pin recorded, a
        rebuild must take the template's model, so a product upgrade lands on this agent
        like any other. A carry-forward made unconditional passes the test above and
        reddens this one."""
        _stub_environment(tmp_path, monkeypatch)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        base = agent.build_agent_config()
        base["model"] = "shipped-model-v1"
        monkeypatch.setattr(agent, "build_agent_config", lambda: json.loads(json.dumps(base)))
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert json.loads(target.read_text(encoding="utf-8"))["model"] == "shipped-model-v1"
        # No pin: the agent still tracks the shipped default.
        assert agent_state.get_model_managed("kirocrew-team-lead") is not False

        # The product ships a new model.
        base["model"] = "shipped-model-v2"
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert json.loads(target.read_text(encoding="utf-8"))["model"] == "shipped-model-v2", (
            "the rebuild carried the previous model forward with no owner pin, so this "
            "agent can never receive a product model upgrade"
        )

    def test_an_unreadable_pin_record_holds_instead_of_dropping_the_pin(
        self, tmp_path, monkeypatch
    ):
        """The finding is the LAST clause: not just that the pin is dropped, but that the
        digest of the replacement is finalized, so the file confirms as ours afterwards and
        nothing ever reports the loss or restores it.

        The lenient read maps an unreadable sidecar to ``None``, the same value as "no
        opinion recorded", so one transient EIO is indistinguishable from "the owner never
        pinned anything". Read strictly, it HOLDS before writing: the pin is still on disk
        and the recorded digest still describes those bytes.

        ONE-SHOT, which is what makes this the finding's case rather than a weaker one: a
        PERSISTENTLY unreadable sidecar is already caught by the strict reads either side
        of this one, so a permanent failure would pass for the wrong reason and say nothing
        about the lenient read in between. The second pass then completes -- HELD means
        work a later pass is expected to finish, and the same read a moment later is
        exactly what finishes it."""
        _stub_environment(tmp_path, monkeypatch)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        base = agent.build_agent_config()
        base["model"] = "shipped-model"
        monkeypatch.setattr(agent, "build_agent_config", lambda: json.loads(json.dumps(base)))
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN

        spec = json.loads(target.read_text(encoding="utf-8"))
        spec["model"] = "a-pinned-model"
        target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        agent_state.set_managed_digest("kirocrew-team-lead", agent_state.spec_digest(spec))
        agent_state.set_model_managed("kirocrew-team-lead", False)
        digest_before = agent_state.get_managed_digest("kirocrew-team-lead")
        bytes_before = target.read_text(encoding="utf-8")

        real_get_model_managed = agent_state.get_model_managed
        failures: list[int] = []

        def _fails_once(name, *, strict=False):
            if strict and not failures:
                failures.append(1)
                raise OSError(5, "EIO")
            return real_get_model_managed(name, strict=strict)

        monkeypatch.setattr(agent_state, "get_model_managed", _fails_once)
        assert (
            agent._install_team_lead_agent() is agent.InstallOutcome.HELD
        ), "a failed read of the pin record settled the install instead of holding it"
        assert (
            target.read_text(encoding="utf-8") == bytes_before
        ), "the install overwrote the spec after failing to read the pin record"
        assert json.loads(target.read_text(encoding="utf-8"))["model"] == "a-pinned-model"
        assert agent_state.get_managed_digest("kirocrew-team-lead") == digest_before, (
            "the digest was replaced, so the file now confirms as ours WITHOUT the pin -- "
            "nothing reports the loss and no rebuild restores it"
        )
        assert failures == [1], "the one-shot failure never fired, so nothing was proven"
        assert agent.spec_start_refusal("kirocrew-team-lead") is None

        # HELD, not settled: the NEXT pass reads the sidecar successfully and finishes the
        # work, with the owner's pin carried forward rather than lost to the first failure.
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert json.loads(target.read_text(encoding="utf-8"))["model"] == "a-pinned-model"

    def test_a_healthy_pin_record_still_carries_the_pin(self, tmp_path, monkeypatch):
        """The control for the strict read: a readable sidecar behaves exactly as before,
        so the strictness costs nothing on the ordinary path."""
        _stub_environment(tmp_path, monkeypatch)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        spec = json.loads(target.read_text(encoding="utf-8"))
        spec["model"] = "a-pinned-model"
        target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        agent_state.set_managed_digest("kirocrew-team-lead", agent_state.spec_digest(spec))
        agent_state.set_model_managed("kirocrew-team-lead", False)

        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert json.loads(target.read_text(encoding="utf-8"))["model"] == "a-pinned-model"

    def test_an_explicit_remove_all_selection_is_not_re_inherited(self, tmp_path, monkeypatch):
        """The empty case, which is the one a non-empty test cannot reach. An explicit
        remove-all-skills save leaves ``resources`` PRESENT and empty; carrying only a
        non-empty list would let the template's own default come back and restore the
        mapping the operator just removed, with nothing telling them."""
        _stub_environment(tmp_path, monkeypatch)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        # A template that HOLDS a default, so re-inheriting it is observable.
        base = agent.build_agent_config()
        base["resources"] = ["file://a-default.md"]
        monkeypatch.setattr(agent, "build_agent_config", lambda: json.loads(json.dumps(base)))
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert json.loads(target.read_text(encoding="utf-8"))["resources"] == [
            "file://a-default.md"
        ]

        # The operator removes everything.
        spec = json.loads(target.read_text(encoding="utf-8"))
        spec["resources"] = []
        target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        agent_state.set_managed_digest("kirocrew-team-lead", agent_state.spec_digest(spec))

        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        assert json.loads(target.read_text(encoding="utf-8"))["resources"] == [], (
            "the rebuild re-inherited the template's default over an explicit "
            "remove-all selection"
        )

    def test_a_clean_rebuild_still_rewrites_from_the_template(self, tmp_path, monkeypatch):
        """``--clean`` means what it says. An operator running it is asking for the
        template, so the two carried fields are exactly what it drops."""
        _stub_environment(tmp_path, monkeypatch)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        spec = json.loads(target.read_text(encoding="utf-8"))
        spec["model"] = "a-pinned-model"
        spec["resources"] = ["skill://somewhere/SKILL.md"]
        target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        agent_state.set_managed_digest("kirocrew-team-lead", agent_state.spec_digest(spec))
        agent_state.set_model_managed("kirocrew-team-lead", False)

        assert agent._install_team_lead_agent(clean=True) is agent.InstallOutcome.WRITTEN
        cleaned = json.loads(target.read_text(encoding="utf-8"))
        assert cleaned.get("model") != "a-pinned-model", "a clean rebuild kept the pin"
        assert "skill://somewhere/SKILL.md" not in (
            cleaned.get("resources") or []
        ), "a clean rebuild kept the saved skill"
        assert agent.spec_start_refusal("kirocrew-team-lead") is None


class TestTeamLeadCanDoTheWorkItself:
    """Capability 1, which is the gap the crewmate exists to close. Asserted
    against the emitted tool list, never against a sentence about it."""

    def test_the_writers_and_shell_are_mounted(self, tmp_path, monkeypatch):
        data = _install(tmp_path, monkeypatch)
        for tool in ("fs_write", "execute_bash", "code"):
            assert tool in data["tools"], tool

    def test_the_tool_list_is_not_a_conductor_literal(self, tmp_path, monkeypatch):
        """Each conductor installer OVERWRITES the template's list with a literal
        that withholds the writers. This installer appends to the template, which
        is what makes the do-it-yourself half true of the spec."""
        data = _install(tmp_path, monkeypatch)
        assert "fs_write" in data["tools"]
        assert "@kirocrew-dashboard" in data["tools"]
        assert "@kirocrew-work" in data["tools"]

    def test_writing_a_file_still_passes_the_gate(self, tmp_path, monkeypatch):
        """Mounted and ungranted, which is the default agent's own posture:
        ``fs_write`` and ``execute_bash`` reach the PreToolUse gate rather than
        skipping it through ``allowedTools``, whose entries are name-scoped with
        no argument matching."""
        allowed = set(_install(tmp_path, monkeypatch)["allowedTools"])
        assert "fs_write" not in allowed
        assert "execute_bash" not in allowed


class TestTeamLeadGrantsAreExactlyTheIntendedTuple:
    def test_every_grant_comes_from_an_existing_tuple(self, tmp_path, monkeypatch):
        """No grant name of this spec's own. Each tuple keeps its own invariant
        comment as the justification, and a reviewer asking what a new
        auto-approve widens has nothing to be shown."""
        data = _install(tmp_path, monkeypatch)
        shipped = {ref for tuple_ in agent._TEAM_LEAD_SHIPPED_GRANTS for ref in tuple_}
        # ``fs_read`` is the template's, and survives; its ``@kirocrew-core`` does
        # not, because this spec grants that server verb by verb.
        assert set(data["allowedTools"]) == shipped | {"fs_read"}
        # Named explicitly, because the comparison above reads the shipped list and
        # so cannot notice a tuple being added to it. ``work_report`` writes into a
        # PARENT's record across a dispatch relationship, which is why every
        # conductor withholds it, and this agent is the root of its own goal and has
        # no parent to report to. ``work_brief`` is granted and is the control: it
        # proves the probe reads the work server's refs rather than matching nothing.
        assert "@kirocrew-work/work_report" not in data["allowedTools"]
        assert "@kirocrew-work/work_brief" in data["allowedTools"]

    def test_the_grant_list_documents_exactly_the_tuples_it_ships(self):
        """A tuple added to the shipped list without a line in the comment above it
        is how a withheld verb arrives unnoticed: the enumeration test reads that
        same list, so only the documentation disagrees. Keep the two in step."""
        path = _REPO_ROOT / "src" / "kiro_crew" / "agent_materialization" / "team_lead_agent.py"
        source = path.read_text(encoding="utf-8")
        # Every lookup below is checked before it is used. This test reads SOURCE
        # TEXT, so a reword of either anchor is a likely future edit, and an
        # unchecked ``index`` answers that edit with a ValueError traceback naming
        # nothing -- which reads as a broken test rather than as a renamed anchor.
        decl = "_TEAM_LEAD_SHIPPED_GRANTS: tuple"
        comment_head = "#: The grant tuples this spec ships"
        head, found, rest = source.partition(decl)
        assert found, f"{path.name} no longer declares {decl!r}; update this test's anchor"
        assert comment_head in head, (
            f"{path.name} no longer opens its grant-list comment with {comment_head!r}; "
            "update this test's anchor"
        )
        comment = head[head.rindex(comment_head) :]
        open_at = rest.find("(")
        close_at = rest.find(")")
        assert 0 <= open_at < close_at, (
            f"the {decl!r} assignment in {path.name} is not the parenthesised tuple "
            "this test reads; update this test"
        )
        listed = [
            line.strip().rstrip(",").removeprefix("agent_mod.")
            for line in rest[open_at:close_at].splitlines()
            if line.strip().startswith("agent_mod.")
        ]
        assert len(listed) == 3, f"the shipped list names {len(listed)} tuples: {listed}"
        for name in listed:
            assert f"``{name}``" in comment, f"{name} is shipped and not documented"

    def test_the_whole_server_grant_does_not_survive_beside_the_named_verbs(
        self, tmp_path, monkeypatch
    ):
        """The narrowing has to be real. Both backends resolve a whole-server
        reference before a per-tool one, so a bare ``@kirocrew-core`` left beside
        the named verbs would auto-approve every verb on that server -- including
        the ones that START work from ingested context -- and would survive the
        governance ceiling's strip of any single verb."""
        data = _install(tmp_path, monkeypatch)
        allowed = set(data["allowedTools"])
        assert "@kirocrew-core" not in allowed
        assert "@kirocrew-core/monitor_start" in allowed
        for never in (
            "@kirocrew-core/task_run",
            "@kirocrew-core/workflow_run",
            "@kirocrew-core/spawn_run",
            "@kirocrew-core/cron_add",
        ):
            assert never not in allowed, never
        # Mounted, so a governed verb still works through the approval gate.
        assert "@kirocrew-core" in data["tools"]

    def test_narrowing_keeps_a_per_verb_entry_on_the_same_server(self):
        """Only an EXACT whole-server match is dropped. A control, because a pass
        that dropped every ref containing the server name would silently remove
        the grants this spec exists to ship."""
        kept = agent._narrow_whole_server_grants(
            ["@kirocrew-core", "@kirocrew-core/monitor_start", "fs_read", "@builder-mcp"]
        )
        assert kept == ["@kirocrew-core/monitor_start", "fs_read", "@builder-mcp"]

    def test_the_dispatch_and_patrol_verbs_are_granted(self, tmp_path, monkeypatch):
        """An unattended patrol cycle must not stall on an approval nobody is
        there to give."""
        allowed = set(_install(tmp_path, monkeypatch)["allowedTools"])
        for ref in (
            "@kirocrew-dashboard/session_create",
            "@kirocrew-dashboard/chat_folder_file_self",
            "@kirocrew-core/monitor_start",
            "@kirocrew-core/resource_status",
            "@kirocrew-core/session_ledger_record",
            "@kirocrew-work/work_ledger_record",
            "@kirocrew-work/work_brief",
        ):
            assert ref in allowed, ref

    def test_the_peer_mutating_verbs_stay_gated_on_the_spec(self, tmp_path, monkeypatch):
        """The three verbs that MUTATE a peer session are earned by an ownership
        fence a spec does not have: ``authorize_target`` refuses a MEMBER caller
        on a session it did not create. A crewmate's own thread is granted them
        from that fence at session establishment, so the spec withholds them."""
        allowed = set(_install(tmp_path, monkeypatch)["allowedTools"])
        for ref in (
            "@kirocrew-dashboard/session_send",
            "@kirocrew-dashboard/session_broadcast",
            "@kirocrew-dashboard/session_stop",
            "@kirocrew-dashboard",
        ):
            assert ref not in allowed, ref
        assert "@kirocrew-dashboard" in _install(tmp_path, monkeypatch)["tools"]

    def test_the_panel_server_is_never_emitted(self, tmp_path, monkeypatch):
        """A spec may not carry it at all: it is ``opt_in`` and host-injected per
        session, and its verbs resolve their crew from the session. A spec that
        listed them would ship tools answering ``no_crew``."""
        data = _install(tmp_path, monkeypatch)
        assert "@kirocrew-panel" not in data["tools"]
        assert "kirocrew-panel" not in data["mcpServers"]
        assert not [ref for ref in data["allowedTools"] if "kirocrew-panel" in ref]

    def test_the_ceiling_filters_the_grants_and_the_derived_rules(self, tmp_path, monkeypatch):
        """``allowedTools`` is the one path that never reaches the gate, so a host
        governing a verb gets a prompt rather than a bypass -- and the KAS rules
        are derived from the FILTERED list, so a stripped grant loses its rule."""
        data = _install(
            tmp_path,
            monkeypatch,
            may_auto_approve=lambda ref: ref != "@kirocrew-core/monitor_start",
        )
        assert "@kirocrew-core/monitor_start" not in data["allowedTools"]
        rules = json.dumps(data["permissions"])
        assert "monitor_start" not in rules
        assert "monitor_update" in rules

    def test_the_two_opt_in_servers_are_assigned_by_hand(self, tmp_path, monkeypatch):
        """Neither spec-writing loop emits an ``opt_in`` set, so naming them here
        IS the per-agent assignment. The template's own servers survive, because
        an agent that runs a build may need them."""
        mcp = _install(tmp_path, monkeypatch)["mcpServers"]
        assert mcp["kirocrew-dashboard"]["args"] == ["mcp-dashboard"]
        assert mcp["kirocrew-work"]["args"] == ["mcp-work"]
        assert "kirocrew-core" in mcp


class TestTeamLeadCharterStatesEachCapability:
    """One assertion per capability the crewmate ships, because for a charter the
    sentence IS the mechanism: an agent that is not told to call the evaluator
    reads the claim instead."""

    def test_it_decides_between_doing_and_dispatching(self, tmp_path, monkeypatch):
        charter = _charter(tmp_path, monkeypatch)
        assert "ONE acceptance condition" in charter
        assert "Everything else is dispatched." in charter

    def test_it_registers_the_work_before_starting(self, tmp_path, monkeypatch):
        """Generic by design: a step the owner configures, never a named crew."""
        charter = _charter(tmp_path, monkeypatch)
        assert "run the owner's intake step" in charter

    def test_it_dispatches_in_the_mandated_order(self, tmp_path, monkeypatch):
        charter = _charter(tmp_path, monkeypatch)
        create = charter.index("`work_ledger_record` `action=create`")
        dispatch = charter.index("`session_create` with a title")
        bind = charter.index("`action=bind`")
        seed = charter.index("`session_send` the seed")
        assert create < dispatch < bind < seed
        assert "Bind before you seed." in charter
        assert "ONE brief file that every seed names by path" in charter

    def test_nesting_is_the_default_and_is_one_level(self, tmp_path, monkeypatch):
        """The promise is graded against what the server permits: a ledger item at
        the third conducting level is refused, so a charter promising more would
        promise what the server refuses."""
        charter = _charter(tmp_path, monkeypatch)
        assert "dispatch a conductor rather than a worker" in charter
        assert "That nesting is one level" in charter

    def test_patrol_is_event_driven(self, tmp_path, monkeypatch):
        charter = _charter(tmp_path, monkeypatch)
        assert 'watch="work-ledger"' in charter

    def test_loop_health_is_a_runtime_reading_not_a_memory(self, tmp_path, monkeypatch):
        """A dead loop reads exactly like a working one, so the charter names the
        two fields the runtime DERIVES -- the ``bind`` reply's ``patrol`` and the
        compact row's ``unpatrolled`` -- and keeps ``monitor_inspect`` as the
        fallback. A rule that tells the agent to remember having armed a loop is
        the failure it is written against."""
        charter = _charter(tmp_path, monkeypatch)
        assert "Loop health is checked, never remembered" in charter
        assert "`unpatrolled`" in charter
        assert "a `patrol` field" in charter
        assert "`monitor_inspect` is the fallback" in charter

    def test_acceptance_is_the_evaluator_and_never_a_claim(self, tmp_path, monkeypatch):
        charter = _charter(tmp_path, monkeypatch)
        assert "scripts/accept_eval.py" in charter
        assert "`action=decide`" in charter
        assert "`action=accept`" in charter
        assert "a CLAIM, never an acceptance" in charter
        assert "Nothing a child can write reaches `verdict`" in charter

    def test_the_head_sha_is_read_in_the_turn_it_is_reported(self, tmp_path, monkeypatch):
        """A child's green is a reading of some head, and a rebase moves it."""
        charter = _charter(tmp_path, monkeypatch)
        assert "names the head sha you read from git in that same turn" in charter

    def test_a_stalled_fleet_is_not_read_as_a_busy_one(self, tmp_path, monkeypatch):
        charter = _charter(tmp_path, monkeypatch)
        assert "`stale` at once is a STOPPED fleet" in charter

    def test_the_seed_quotes_the_ask_and_the_child_echoes_it(self, tmp_path, monkeypatch):
        """Both halves, because one without the other does not catch the failure:
        a verbatim seed nobody reads back is still a seed nobody read."""
        charter = _charter(tmp_path, monkeypatch)
        assert "restates the owner's ask VERBATIM" in charter
        assert "Require the echo." in charter

    def test_it_drives_the_dashboard_and_writes_no_numbers(self, tmp_path, monkeypatch):
        charter = _charter(tmp_path, monkeypatch)
        assert "`dashboard_fields`" in charter
        assert "`dashboard_write`" in charter
        assert "`verdict`" in charter
        assert "`for_you`" in charter
        assert "Write judgement, never arithmetic." in charter

    def test_its_own_state_survives_a_restart(self, tmp_path, monkeypatch):
        charter = _charter(tmp_path, monkeypatch)
        assert "`session_ledger_record`" in charter
        assert "a compaction or a restart resumes the patrol" in charter

    def test_capacity_is_read_and_never_a_number_it_holds(self, tmp_path, monkeypatch):
        charter = _charter(tmp_path, monkeypatch)
        assert "`resource_status`" in charter
        assert "hold no count of your own for how many sessions a goal may run" in charter

    def test_the_charter_carries_no_polling_prose(self, tmp_path, monkeypatch):
        """An event-driven patrol described as a timer is a charter that teaches
        the opposite of what it mounts. The control proves the probe reads."""
        charter = _charter(tmp_path, monkeypatch).lower()
        for banned in ("poll", "on a timer", "re-check in"):
            assert banned not in charter, banned
        assert "work-ledger" in charter

    def test_the_charter_names_its_own_skill_as_the_procedure(self, tmp_path, monkeypatch):
        """Wiring, not wording. A charter pointing at a conductor's skill would
        send this agent to a procedure whose first rule is that it does no work
        itself, which is the one capability it exists to add. The shipped skill
        directory and the name the charter reads must be the same string."""
        charter = _charter(tmp_path, monkeypatch)
        assert "The `team-lead` skill carries the operating procedure" in charter
        assert (_SKILLS / "team-lead" / "SKILL.md").is_file()
        # The two scripts still come from where they are maintained.
        assert "`goal-conductor` skill's `scripts/accept_eval.py`" in charter

    def test_the_retired_verbosity_token_is_absent(self, tmp_path, monkeypatch):
        """Reply style arrives as session-context chrome for every agent, so a
        token left here reaches the model as a literal."""
        assert "{{VERBOSITY_BLOCK}}" not in _install(tmp_path, monkeypatch)["prompt"]


class TestTheCrewmateIsMadeByOneDocumentedCommand:
    """The product ships the TEMPLATE. Binding a crewmate to it is the operator's
    one command, and these tests pin that the command documented is the command
    the CLI actually accepts -- a doc naming a flag the parser does not take is
    worse than no doc, because it fails only when somebody follows it."""

    def test_the_cli_accepts_the_documented_command(self):
        """Read off the parser rather than trusted: ``--name`` is required and
        ``--kiro-agent`` takes the template name."""
        cli = (_REPO_ROOT / "src" / "kiro_crew" / "cli.py").read_text(encoding="utf-8")
        assert 'agent_sub.add_parser("create"' in cli
        assert 'agent_create.add_argument("--kiro-agent"' in cli
        assert '"--name",\n        required=True,' in cli

    def test_the_rfc_and_the_skill_both_name_it(self):
        """Both readers of this feature need it, and neither should invent its own
        spelling: an operator reading the RFC and an agent reading the skill."""
        command = "kirocrew agent create --name <name> --kiro-agent kirocrew-team-lead"
        rfc = (_REPO_ROOT / "docs" / "request-for-change" / "rfc-lead-crewmate.md").read_text(
            encoding="utf-8"
        )
        skill = (_SKILLS / "team-lead" / "SKILL.md").read_text(encoding="utf-8")
        assert command in rfc
        assert command in skill

    def test_no_config_switch_and_no_boot_path_provisioning_remain(self):
        """The product creates no crewmate, so nothing may read a switch for one
        and nothing may run on the boot path for one. Asserted against the three
        files that carried it, with a control proving each probe reads."""
        for rel, control in (
            (("src", "kiro_crew", "config", "sections.py"), "crew_panel"),
            (("src", "kiro_crew", "config", "loader.py"), "crew_panel"),
            (("src", "kiro_crew", "dashboard", "server.py"), "_kick_crewmate_prune"),
        ):
            text = (_REPO_ROOT.joinpath(*rel)).read_text(encoding="utf-8")
            assert control in text, f"{rel[-1]}: the probe does not read"
            assert "team_lead_crewmate" not in text, rel[-1]


class TestTeamLeadSkillReusesRatherThanCopies:
    def test_the_skill_ships_in_the_tree(self):
        assert (_SKILLS / "team-lead" / "SKILL.md").is_file()

    def test_it_delegates_the_shared_procedure_instead_of_restating_it(self):
        """The skill is a DELTA. Dispatch order, patrol, acceptance, stop
        conditions, durable state and capacity live in ``goal-conductor``, and a
        procedure stated twice is one that drifts until a reader follows neither
        copy. So the test is that the pointer is there and the restatement is
        not."""
        skill = (_SKILLS / "team-lead" / "SKILL.md").read_text(encoding="utf-8")
        assert "goal-conductor" in skill
        assert "It is your procedure" in skill
        # The sections it defers rather than repeats, each named by a phrase the
        # long form used. A control follows, so an empty match cannot pass.
        for restated in ("`action=bind`", "`monitor_start`", "`action=verdict`"):
            assert restated not in skill, restated
        assert "do-it-yourself test" in skill

    def test_no_copy_of_either_script_ships_with_it(self):
        """The reuse is the point. A copy is a second file whose later divergence
        from the original nothing in the tree would detect."""
        for script in ("accept_eval.py", "patrol_budget.py"):
            assert not (_SKILLS / "team-lead" / "scripts" / script).exists(), script

    def test_the_skill_carries_no_polling_prose(self):
        """The control is a term this delta carries itself. Patrol belongs to
        ``goal-conductor``, so a term from the patrol section would prove nothing
        about whether this probe reads."""
        skill = (_SKILLS / "team-lead" / "SKILL.md").read_text(encoding="utf-8").lower()
        for banned in ("on a timer", "re-check in", "poll"):
            assert banned not in skill, banned
        assert "goal-conductor" in skill


class TestTeamLeadPayloadGovernanceGenerationGuard:
    """A team-lead KAS payload is built from the on-disk spec BEFORE the admission
    gate's unbounded queue wait. A governance refresh during that wait rebuilds the disk
    spec (stripping any grant the tightened ceiling removes), but the already-built
    payload still carries the old grants and is what ``session/new`` registers. The
    disk-reading admission check cannot see this; the generation the payload was built
    under can. These pin that guard.

    The worker mirror has its own ``DerivedSpecSnapshot`` bracket, so the generation
    guard is deliberately team-lead-only -- answering a generation for any other agent
    would be a second guard over the same consume window.
    """

    def test_only_the_team_lead_stem_gets_a_payload_generation(self, monkeypatch):
        """``None`` for every other agent means the recheck is NOT APPLICABLE rather
        than satisfied -- the worker and the default both mirror a spec and are
        bracketed by the derived-spec snapshot instead."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_answer_generation",
            lambda: 7,
            raising=False,
        )
        assert agent.payload_governance_generation("kirocrew-team-lead") == 7
        assert agent.payload_governance_generation("kirocrew-worker") is None
        assert agent.payload_governance_generation("kirocrew") is None
        assert agent.payload_governance_generation(None) is None
        assert agent.is_team_lead_agent("kirocrew-team-lead") is True
        assert agent.is_team_lead_agent("kirocrew-worker") is False

    def test_an_unchanged_generation_admits_the_start(self, monkeypatch):
        """The payload was built under the ceiling live right now, so the recheck finds
        no drift and the start proceeds (reason is ``None``)."""
        gen = {"value": 4}
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_answer_generation",
            lambda: gen["value"],
            raising=False,
        )
        built_under = agent.payload_governance_generation("kirocrew-team-lead")
        # No refresh landed: the recheck must admit (None).
        assert agent.stale_payload_generation_reason(built_under, "kirocrew-team-lead") is None

    def test_a_generation_bump_during_the_wait_refuses_the_start(self, monkeypatch):
        """THE finding. A governance change -- a ceiling install OR a profile edit --
        landing while the start waited in the admission queue advances the COMBINED
        governance-answer generation; the payload built under the old one must be refused
        rather than shipped, because it may carry an auto-approval the change removed. The
        guard reads ``governance_answer_generation`` (ceiling + profile), not the ceiling
        counter alone, so a profile-only edit is caught too."""
        gen = {"value": 4}
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_answer_generation",
            lambda: gen["value"],
            raising=False,
        )
        built_under = agent.payload_governance_generation("kirocrew-team-lead")
        # A governance change (ceiling or profile) advances the combined token while the
        # start is queued.
        gen["value"] = 5
        reason = agent.stale_payload_generation_reason(built_under, "kirocrew-team-lead")
        assert reason and "start was in progress" in reason and "governance answer" in reason

    def test_a_none_generation_short_circuits_for_another_agent(self, monkeypatch):
        """A non-team-lead payload carries ``None``; the recheck is a no-op and must
        never read the live generation or refuse.

        THE CONTROL for the refusal below. ``None`` admits here and refuses there, and the
        agent is the only thing that differs, so a change that makes the refusal
        unconditional reds this and a change that makes it unreachable reds that one."""
        calls: list[int] = []

        def _tripwire() -> int:
            calls.append(1)
            return 0

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_answer_generation",
            _tripwire,
            raising=False,
        )
        assert agent.stale_payload_generation_reason(None, "kirocrew-worker") is None
        assert agent.stale_payload_generation_reason(None, "kirocrew") is None
        assert agent.stale_payload_generation_reason(None, None) is None
        assert calls == [], "the None short-circuit still read the live generation"

    def test_an_unrecorded_generation_refuses_the_team_lead(self, monkeypatch):
        """THE SECOND finding. ``None`` was two facts wearing one spelling, and only one
        of them may be admitted.

        For another agent ``None`` means NOT APPLICABLE -- it mirrors a default and is
        bracketed by a ``DerivedSpecSnapshot`` instead. For the team lead it means the
        question was never ASKED: on the native kiro-cli path the agent IS the team lead
        and the generation is absent only because that path built no KAS payload. Reading
        the second as "no drift" admits a spec whose grants nothing checked, which is the
        same degrade-an-unanswered-question defect as the lenient ownership read."""
        calls: list[int] = []

        def _tripwire() -> int:
            calls.append(1)
            return 0

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_answer_generation",
            _tripwire,
            raising=False,
        )
        reason = agent.stale_payload_generation_reason(None, "kirocrew-team-lead")
        assert reason, "an unrecorded generation for the team lead was ADMITTED"
        assert "no governance generation was recorded" in reason
        assert calls == [], (
            "the refusal read the live generation: there is nothing to compare it "
            "against, so reading it can only invite a spurious match"
        )

    def test_the_agent_argument_cannot_be_omitted(self):
        """A parameter with a default is a parameter the next enforcement point omits
        silently, which is exactly how the ``None`` hole stayed open. Required, so a
        fourth call site cannot be added without deciding what its ``None`` means."""
        import inspect

        sig = inspect.signature(agent.stale_payload_generation_reason)
        assert list(sig.parameters) == ["generation", "agent"]
        assert sig.parameters["agent"].default is inspect.Parameter.empty, (
            "``agent`` gained a default; a caller that omits it gets the admitting "
            "branch for the team lead, which is the defect this parameter closed"
        )


class TestTheNativePathBracketsTheTeamLeadToo:
    """The native kiro-cli path consumed the team-lead spec inside NO bracket.

    Both of the existing guards answer ``None`` there, for different reasons, and the
    bracket then had nothing left to check:

    * ``require_fresh_derived_spec`` answers ``None`` because the team lead mirrors no
      default -- its SCOPE guard, not a freshness verdict.
    * the generation recheck answered ``None`` because the caller's
      ``payload_generation`` is ``None`` on this path: no KAS payload was ever built, so
      nothing recorded one.

    Two "not applicable"s do not add up to "verified". On this path the agent IS the team
    lead and its spec IS about to be consumed -- kiro-cli reads it from disk at
    ``set_mode`` -- so the window between fixing those bytes and activating them is real
    and was unguarded. The bracket now records the governance answer itself, before the
    read that fixes the consumed bytes, and rechecks it after the activation returns.

    Driven through a send that SUCCEEDS, because this guard runs after the host has
    consumed the spec: on the scaffold whose send raises, the body never reaches it and
    these tests would pass with no guard at all.
    """

    def _ready(self, tmp_path, monkeypatch):
        """A clean installed spec, so ``team_lead_start_refusal`` admits and what these
        tests observe is the generation guard rather than one of the four refusals."""
        _stub_environment(tmp_path, monkeypatch)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN

    @pytest.mark.asyncio
    async def test_no_governance_move_admits_the_native_start(self, tmp_path, monkeypatch):
        """THE OVER-TIGHT CONTROL, and the one that matters most here: a native team-lead
        start with nothing moving must go through. An unconditional recheck, or one that
        treats its own recorded value as stale, reds this and only this -- which is how a
        guard that terminates every session is told apart from one that terminates the
        right ones."""
        self._ready(tmp_path, monkeypatch)
        sent: list[str] = []
        terminated: list[str] = []
        rt = _runtime_for_native_activation(tmp_path, sent, terminated)

        await _activate_native(rt, "kirocrew-team-lead")

        assert sent == [METHOD_SET_MODE], "the activation never went out"
        assert terminated == [], (
            "a native team-lead start was terminated with no governance change at all, "
            "which is a product outage rather than a safety property"
        )

    @pytest.mark.asyncio
    async def test_a_ceiling_bump_during_the_activation_ends_the_session(
        self, tmp_path, monkeypatch
    ):
        """THE finding's first half. A ceiling install landing between the point the
        consumed bytes were fixed and the point the host had activated them leaves the
        session running on grants the install removed, so the session ends.

        The ceiling half is moved by patching ``context.governance_generation``, the
        counter ``_install`` bumps: installing a real context would have to compose a real
        ceiling, and what is under test is the bracket, not composition.
        ``governance_answer_generation`` is NOT patched -- it runs for real and sums the
        two halves, so this also proves the bracket reads the combined token."""
        self._ready(tmp_path, monkeypatch)
        from kiro_crew.acp.session_handle import AcpRuntimeError
        from kiro_crew.platform import context as context_mod

        ceiling = {"value": 11}
        monkeypatch.setattr(
            context_mod, "governance_generation", lambda: ceiling["value"], raising=False
        )

        sent: list[str] = []
        terminated: list[str] = []
        rt = _runtime_for_native_activation(tmp_path, sent, terminated)
        inner_send = rt._send_and_await

        async def _send_then_tighten(method, params, timeout=None, **kw):
            # INSIDE the window: the host has read the spec by the time this returns,
            # and the ceiling moved while it was doing so.
            result = await inner_send(method, params, timeout=timeout, **kw)
            ceiling["value"] += 1
            return result

        rt._send_and_await = _send_then_tighten  # type: ignore[method-assign]

        with pytest.raises(AcpRuntimeError) as refused:
            await _activate_native(rt, "kirocrew-team-lead")

        assert "start was in progress" in str(refused.value)
        assert sent == [METHOD_SET_MODE], (
            "the bump was applied outside the window this guard covers, so this test "
            "would pass for a bracket that records nothing"
        )
        assert terminated == ["sid-1"], (
            "kiro-cli has already loaded the spec, so a local unregister would leave a "
            "live session holding the grants the ceiling withdrew"
        )

    @pytest.mark.asyncio
    async def test_a_profile_bump_during_the_activation_ends_the_session(
        self, tmp_path, monkeypatch
    ):
        """THE finding's second half, and a separate test on purpose: one counter moving
        proves nothing about the other, and the ceiling test above would stay green for a
        bracket that read ``context.governance_generation()`` alone.

        This half moves the REAL counter -- ``governance_profiles.reset_store()`` is one
        of the two writers of ``_PROFILE_GENERATION`` -- so nothing about the profile layer
        is stubbed here."""
        self._ready(tmp_path, monkeypatch)
        from kiro_crew.acp.session_handle import AcpRuntimeError
        from kiro_crew.platform import governance_profiles as profiles_mod

        sent: list[str] = []
        terminated: list[str] = []
        rt = _runtime_for_native_activation(tmp_path, sent, terminated)
        inner_send = rt._send_and_await

        async def _send_then_publish(method, params, timeout=None, **kw):
            result = await inner_send(method, params, timeout=timeout, **kw)
            # A published profile snapshot: the operator edited a profile while the
            # activation was in flight.
            profiles_mod.reset_store()
            return result

        rt._send_and_await = _send_then_publish  # type: ignore[method-assign]

        before = profiles_mod.governance_answer_generation()
        with pytest.raises(AcpRuntimeError) as refused:
            await _activate_native(rt, "kirocrew-team-lead")

        assert profiles_mod.governance_answer_generation() != before, (
            "reset_store did not move the combined token, so this test proves nothing "
            "about the profile half"
        )
        assert "start was in progress" in str(refused.value)
        assert terminated == ["sid-1"]

    @pytest.mark.asyncio
    async def test_a_native_team_lead_start_with_nothing_recorded_is_refused(
        self, tmp_path, monkeypatch
    ):
        """The ``None`` case, at the enforcement point rather than at the function.

        A bracket that reaches activation having recorded nothing has proven nothing about
        the grants it is activating. Before the fix this was the ORDINARY native path and
        it was admitted; now it is refused wherever it occurs, which leaves it as a
        fail-closed backstop for a fifth path added tomorrow that forgets to record."""
        self._ready(tmp_path, monkeypatch)
        from kiro_crew.acp.session_handle import AcpRuntimeError

        # A capture that answers nothing -- the state the native path was in before it
        # had a capture of its own.
        monkeypatch.setattr(agent, "payload_governance_generation", lambda a: None)

        sent: list[str] = []
        terminated: list[str] = []
        rt = _runtime_for_native_activation(tmp_path, sent, terminated)

        with pytest.raises(AcpRuntimeError) as refused:
            await _activate_native(rt, "kirocrew-team-lead")

        assert "no governance generation was recorded" in str(refused.value)
        assert terminated == ["sid-1"]

    @pytest.mark.asyncio
    async def test_a_native_start_of_another_agent_is_unaffected(self, tmp_path, monkeypatch):
        """THE CONTROL that keeps this from terminating every session. Another agent gets
        no recorded generation on this path and must be admitted anyway: it mirrors a
        default and its own bracket is the derived-spec snapshot. Moving the ceiling under
        it changes nothing, because there is no recorded value to compare against."""
        self._ready(tmp_path, monkeypatch)
        from kiro_crew.platform import context as context_mod

        ceiling = {"value": 3}
        monkeypatch.setattr(
            context_mod, "governance_generation", lambda: ceiling["value"], raising=False
        )

        sent: list[str] = []
        terminated: list[str] = []
        rt = _runtime_for_native_activation(tmp_path, sent, terminated)
        inner_send = rt._send_and_await

        async def _send_then_tighten(method, params, timeout=None, **kw):
            result = await inner_send(method, params, timeout=timeout, **kw)
            ceiling["value"] += 1
            return result

        rt._send_and_await = _send_then_tighten  # type: ignore[method-assign]

        # ``kirocrew`` is this runtime's own spawn agent, so the activation is the
        # ordinary same-agent one every shared session performs.
        await _activate_native(rt, "kirocrew")

        assert sent == [METHOD_SET_MODE]
        assert terminated == [], (
            "a governance move terminated a session for an agent this guard does not "
            "cover, which would make every native start fragile"
        )


class TestOwnershipIsSettledBeforeAnyBookkeepingWrite:
    """The dashboard model PATCH asked "is this ours?" AFTER recording the model pin.

    ``_confirms_managed_pre_write`` propagates a transient ownership-read failure by
    contract -- that is the whole point of the strict read, so the caller aborts instead
    of rewriting a spec with a stale digest. But the PATCH asked it below
    ``set_model_managed``, so the raise arrived with the sidecar already saying the model
    was the owner's and with nothing written to the file. The spec then reproduces no
    recorded digest, so the admission gate refuses every later start as hand-edited, and
    the next rebuild carries forward a pin the bytes never received.

    Settled first, the same failure aborts with nothing written at all. The ORDER is the
    fix; the propagation was already there.
    """

    @pytest.mark.asyncio
    async def test_a_transient_ownership_read_leaves_the_pin_and_the_file_alone(
        self, tmp_path, monkeypatch
    ):
        """A one-shot ``OSError`` out of the ownership read during a model PATCH. Nothing
        may be left behind: not the sidecar's ``model_managed``, not the file."""
        from unittest.mock import MagicMock

        from aiohttp import web

        from kiro_crew.dashboard.handlers import agents as detail_mod

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        _stub_environment(tmp_path, monkeypatch)
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", tmp_path)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME

        before_bytes = target.read_bytes()
        before_pin = agent_state.get_model_managed("kirocrew-team-lead")

        reads: list[str] = []

        def _unreadable(name, data):
            reads.append(name)
            raise OSError("ownership record temporarily unavailable")

        monkeypatch.setattr(detail_mod, "_confirms_managed_pre_write", _unreadable)

        request = MagicMock(spec=web.Request)
        request.method = "PATCH"
        request.match_info = {"name": "kirocrew-team-lead"}
        request.app = {"state": MagicMock()}

        async def _json():
            return {"model": "a-pinned-model"}

        request.json = _json

        try:
            resp = await api_agent_detail(request)
        except OSError:
            # Either shape is acceptable: what this pins is that nothing was written,
            # not which status the handler maps a transient storage failure to.
            resp = None
        if resp is not None:
            assert resp.status != 200, "the PATCH reported success on an unanswered read"

        assert reads == ["kirocrew-team-lead"], (
            "the ownership read never fired, so this test would pass for a handler that "
            "writes the pin and never asks"
        )
        assert agent_state.get_model_managed("kirocrew-team-lead") == before_pin, (
            "``model_managed`` was recorded before ownership was settled, so a transient "
            "sidecar failure leaves the pin claimed and the digest unrenewed -- the "
            "half-applied state that makes every later start refuse"
        )
        assert target.read_bytes() == before_bytes, "the spec was rewritten after an abort"

    @pytest.mark.asyncio
    async def test_the_settled_answer_is_read_once_and_reused(self, tmp_path, monkeypatch):
        """ONE read, used twice. Two reads of the same question can disagree across a
        concurrent write, and the handler would then decide under one answer and renew
        under the other -- a third state nobody reasoned about."""
        from unittest.mock import MagicMock

        from aiohttp import web

        from kiro_crew.dashboard.handlers import _shared as shared_mod
        from kiro_crew.dashboard.handlers import agents as agents_handlers
        from kiro_crew.dashboard.handlers import agents as detail_mod

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        _stub_environment(tmp_path, monkeypatch)
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", tmp_path)
        assert agent._install_team_lead_agent() is agent.InstallOutcome.WRITTEN

        skill_md = tmp_path / "skills" / "a-skill" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("# a skill\n", encoding="utf-8")
        snapshot = shared_mod.SkillCatalogSnapshot(
            entries={"a-skill": skill_md}, dir_mtimes={}, walked_at_ns=0
        )
        monkeypatch.setattr(shared_mod, "walk_skill_catalog", lambda *a, **k: snapshot)
        monkeypatch.setattr(
            agents_handlers, "enumerate_skill_catalog", lambda *a, **k: {"a-skill": skill_md}
        )

        real = detail_mod._confirms_managed_pre_write
        calls: list[str] = []

        def _counted(name, data):
            calls.append(name)
            return real(name, data)

        monkeypatch.setattr(detail_mod, "_confirms_managed_pre_write", _counted)

        request = MagicMock(spec=web.Request)
        request.method = "PATCH"
        request.match_info = {"name": "kirocrew-team-lead"}
        request.app = {"state": MagicMock()}

        async def _json():
            return {"model": "a-pinned-model"}

        request.json = _json
        resp = await api_agent_detail(request)
        assert resp.status == 200, resp.status

        assert calls == ["kirocrew-team-lead"], (
            f"ownership was read {len(calls)} times for one write; the answer is taken "
            "once and reused so the decision and the renewal cannot disagree"
        )
        # The write still happened and still renewed, so the reorder did not cost the
        # feature it was protecting.
        saved = json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))
        assert saved["model"] == "a-pinned-model"
        assert (
            agent.spec_start_refusal("kirocrew-team-lead") is None
        ), "the digest was not renewed, so the admission gate refuses every later start"
