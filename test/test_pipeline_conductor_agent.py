"""Pipeline-conductor agent installer + bundled probe/budget scripts.

The script tests run the real files over fixtures, since they are the deterministic
half of the conductor's patrol. Other ``TestFleetProbe`` sections live in
``test_pipeline_conductor_agent_fleet_reports.py`` and
``test_pipeline_conductor_agent_fleet_scope.py``; budget tests in
``test_pipeline_conductor_agent_credit_spend.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import conductor_agent_checks
from pipeline_conductor_agent_helpers import FleetProbeHarness

from kiro_crew import agent
from kiro_crew.agent_files import (
    OWNED_KIRO_AGENT_FILES,
    PIPELINE_CONDUCTOR_AGENT_FILENAME,
)
from kiro_crew.agent_sdk.drivers.acp import derived_agent_permissions
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

#: A release that accepts a spec ``permissions`` block, and one that refuses it,
#: expressed against the floor so raising it cannot strand these tests.
_ACCEPTS = SPEC_PERMISSIONS_MIN_VERSION
_REFUSES = (SPEC_PERMISSIONS_MIN_VERSION[0], SPEC_PERMISSIONS_MIN_VERSION[1] - 1, 0)
_INHERITED_PERMISSIONS = {"rules": [{"capability": "web_fetch", "effect": "deny"}]}


def _pin_spec_permissions_cli(monkeypatch, which):
    """Pin what the shared writer gate believes the installed kiro-cli is.

    ``_write_derived_permissions`` reads ``installed_kiro_cli_version`` function-locally
    from ``kiro_crew.kiro_cli``, so the patch lands there; without it CI's absent binary
    reads as "unknown". ``which`` is ``"accepts"``, ``"refuses"`` or ``"unknown"``.
    """
    version = {"accepts": _ACCEPTS, "refuses": _REFUSES, "unknown": None}[which]
    monkeypatch.setattr("kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: version)


class TestPipelineConductorInstaller(
    conductor_agent_checks.PromptDoesNotCarryTheRetiredVerbosityToken,
    conductor_agent_checks.PromptDrivesPatrolWithMonitorStartNotWait,
):
    def _install(self, tmp_path, monkeypatch, *, may_auto_approve=None, cli_version="accepts"):
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        _pin_spec_permissions_cli(monkeypatch, cli_version)
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
                "tools": ["fs_write", "@kirocrew-core"],
                "allowedTools": ["@kirocrew-core"],
                "permissions": _INHERITED_PERMISSIONS,
            },
        )
        monkeypatch.setattr(
            agent,
            "_kirocrew_mcp_invocation",
            lambda sub: ("/resolved/kirocrew", [sub]),
        )
        monkeypatch.setattr(agent, "_may_auto_approve", may_auto_approve or (lambda ref: True))
        agent._install_pipeline_conductor_agent()
        return json.loads(
            (tmp_path / PIPELINE_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8")
        )

    def test_identity_and_charter(self, tmp_path, monkeypatch):
        data = self._install(tmp_path, monkeypatch)
        assert data["name"] == "kirocrew-pipeline-conductor"
        assert "work item" in data["prompt"]
        assert "pipeline-conductor" in data["prompt"]  # the skill is the procedure

    def test_filename_is_owned(self):
        """The convergence sweep rewrites only OWNED files; a generated spec
        missing from that allowlist silently rots when Playwright servers move."""
        assert PIPELINE_CONDUCTOR_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES

    def test_prompt_names_the_tools_and_scripts_it_runs_on(self, tmp_path, monkeypatch):
        """The charter mounts whole servers; the prompt must name what each job
        uses, and the two bundled scripts, or the agent re-derives fleet state
        from transcripts — the context flood the probe exists to prevent."""
        prompt = " ".join(self._install(tmp_path, monkeypatch)["prompt"].split())
        for named in (
            "session_create",
            "session_ledger_record",
            "monitor_update",
            "resource_status",
            "ask_question",
            "fleet_probe.py",
            "credit_spend.py",
        ):
            assert named in prompt, named

    def test_no_file_writing_tool(self, tmp_path, monkeypatch):
        """Never-does-the-work-itself is a spec property: neither ``fs_write``
        nor ``code`` (governance classes it under filesystem.write) is mounted."""
        data = self._install(tmp_path, monkeypatch)
        assert "fs_write" not in data["tools"]
        assert "code" not in data["tools"]

    def test_dashboard_grants_are_create_and_read_only(self, tmp_path, monkeypatch):
        """The grant invariant: create/read verbs auto-approved; the verbs that
        mutate a peer session (send/stop/move) and ``execute_bash`` stay mounted
        but gated."""
        data = self._install(tmp_path, monkeypatch)
        allowed = set(data["allowedTools"])
        assert "@kirocrew-dashboard/session_create" in allowed
        assert "@kirocrew-dashboard/session_read_message" in allowed
        # The roster read: narrower than the transcript read above, and asked on
        # every unattended cycle, so it is granted rather than gated.
        assert "@kirocrew-dashboard/session_status" in allowed
        # A dispatcher does not manage folders (rfc-crewmate-workers-under-the-crewmate.md).
        assert not [g for g in allowed if "chat_folder_" in g]
        for gated in (
            "@kirocrew-dashboard/session_send",
            # The fan-out write, withheld on session_send's own reason: the
            # fan-out changes how many sessions ingested text reaches, not
            # whether anything bounds what is sent.
            "@kirocrew-dashboard/session_broadcast",
            "@kirocrew-dashboard/session_stop",
            "@kirocrew-dashboard/chat_folder_move_session",
            "@kirocrew-dashboard",
            "execute_bash",
        ):
            assert gated not in allowed, gated
        assert "@kirocrew-dashboard" in data["tools"]  # mounted, so gated verbs still work
        assert "execute_bash" in data["tools"]

    def test_skill_gives_no_folder_guidance(self):
        """Grant and procedure move together. No folder verb is granted to this
        agent (rfc-crewmate-workers-under-the-crewmate.md, Design 6), so its
        skill must not instruct a folder call that would prompt on every
        unattended cycle."""
        from pathlib import Path

        skill = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "kiro_crew"
            / "builtin_skills"
            / "pipeline-conductor"
            / "SKILL.md"
        )
        assert "chat_folder_" not in skill.read_text(encoding="utf-8")

    def test_core_grants_are_named_verbs_never_the_whole_server(self, tmp_path, monkeypatch):
        """Untrusted content feeds every auto-approved call on an unattended
        cycle, so the core surface is granted verb by verb: reads, the patrol
        loop's own lifecycle, and owner reporting. The verbs that START work
        from ingested context (task_run/workflow_run/cron_add/spawn_run) are
        never auto-approved -- and the server stays mounted so they still work
        under a session-level trust grant."""
        data = self._install(tmp_path, monkeypatch)
        allowed = set(data["allowedTools"])
        assert "@kirocrew-core/monitor_start" in allowed
        assert "@kirocrew-core/resource_status" in allowed
        assert "@kirocrew-core/session_ledger_record" in allowed
        for gated in (
            "@kirocrew-core",
            "@kirocrew-core/task_run",
            "@kirocrew-core/workflow_run",
            "@kirocrew-core/cron_add",
            "@kirocrew-core/spawn_run",
        ):
            assert gated not in allowed, gated
        assert "@kirocrew-core" in data["tools"]

    def test_mcp_servers_are_narrowed(self, tmp_path, monkeypatch):
        """kirocrew-core plus the two hand-built opt-in entries the shared conductor
        base mounts (dashboard and work); inherited third-party servers are dropped
        from this spec."""
        data = self._install(tmp_path, monkeypatch)
        assert set(data["mcpServers"]) == {
            "kirocrew-core",
            "kirocrew-dashboard",
            "kirocrew-work",
        }
        assert data["mcpServers"]["kirocrew-dashboard"]["args"] == ["mcp-dashboard"]
        assert data["mcpServers"]["kirocrew-work"]["args"] == ["mcp-work"]

    def test_the_work_ledger_base_reaches_all_four_surfaces(self, tmp_path, monkeypatch):
        """The base work-ledger surface holds on all four places a spec carries it:
        the mount in ``tools``, the conductor verbs in ``allowedTools``, their rules
        in the derived KAS block, and the shared protocol in the prompt. The worker
        verbs stay off: ``work_report`` writes into a parent's record, and this
        conductor's procedure never calls ``work_brief``."""
        data = self._install(tmp_path, monkeypatch)
        assert "@kirocrew-work" in data["tools"]
        work = [ref for ref in data["allowedTools"] if "kirocrew-work" in ref]
        assert work == list(agent._CONDUCTOR_BASE_WORK_GRANTS)
        match = data["permissions"]["rules"][0]["match"]
        assert {"kirocrew-work/work_ledger_read", "kirocrew-work/work_ledger_record"} <= set(match)
        assert agent._CONDUCTOR_WORK_LEDGER_PROTOCOL in data["prompt"]
        assert data["prompt"].startswith(agent._PIPELINE_CONDUCTOR_SYSTEM_PROMPT)

    def test_governed_host_withholds_and_audits(self, tmp_path, monkeypatch):
        """A ceiling that strips a grant must leave an audit record naming THIS
        installer, or the operator has no record of why the agent now prompts."""
        events: list[dict] = []

        class _Sel:
            def log_api_access(self, **kwargs):
                events.append(kwargs)

        monkeypatch.setattr(agent, "sel", lambda: _Sel())
        data = self._install(
            tmp_path,
            monkeypatch,
            may_auto_approve=lambda ref: ref != "@kirocrew-core/monitor_start",
        )
        assert "@kirocrew-core/monitor_start" not in data["allowedTools"]
        withheld = [e for e in events if e.get("operation") == "mcp_auto_approve_withheld"]
        assert withheld and withheld[0]["source"] == "_install_pipeline_conductor_agent"

    def test_the_permissions_field_is_gated_on_the_installed_kiro_cli(self, tmp_path, monkeypatch):
        """Written on an accepting release, withheld on a refusing or unknown one.

        The pipeline conductor spec gates its ``permissions`` write on the
        installed kiro-cli, sharing the default spec's gate: a kiro-cli whose
        schema predates the field would otherwise refuse the WHOLE spec and fall
        back to broader default grants. ``allowedTools`` is untouched either way.
        """
        accepting = self._install(tmp_path, monkeypatch, cli_version="accepts")
        assert accepting.get("permissions"), "an accepting CLI must get the block"
        assert accepting["permissions"] != _INHERITED_PERMISSIONS
        assert accepting["permissions"] == derived_agent_permissions(
            accepting["allowedTools"], PIPELINE_CONDUCTOR_AGENT_FILENAME
        )
        assert accepting["allowedTools"], "the grant list is never withheld"

        for refusing in ("refuses", "unknown"):
            data = self._install(tmp_path, monkeypatch, cli_version=refusing)
            assert "permissions" not in data, f"{refusing} CLI must get no block"
            assert data["allowedTools"], "the grant list is never withheld"


class TestFleetProbe(FleetProbeHarness):

    def test_protocol_tag_fires_and_working_stays_quiet(self, tmp_path, capsys, monkeypatch):
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-green", "s-working"])
        self._session(tmp_path / "sessions", "s-green", "GREEN: PR #5 https://x head abc123")
        self._session(tmp_path / "sessions", "s-working", "WORKING: reproducing")
        out = self._run(mod, cfg, capsys)
        assert "s-green" in out and "GREEN" in out
        assert "s-working" not in out  # WORKING is a heartbeat, not a signal
        assert "OK 2 watched, 1 fired" in out

    def test_a_protocol_word_in_prose_is_not_a_report(self, tmp_path, capsys, monkeypatch):
        """The protocol is ``<WORD>:``. A line that merely OPENS with a protocol
        word -- ``PR #<n> is green ...`` -- is prose, and tagging it invents a
        report nobody filed. Among assistant rows matching a bare ``^<WORD>\\b``
        form, many are prose, and a bare ``PR #<n>`` is the common shape."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-prose"])
        self._session(
            tmp_path / "sessions", "s-prose", "PR #6580 is green, waiting on a human review"
        )
        out = self._run(mod, cfg, capsys)
        assert "s-prose" not in out, "prose opening with a protocol word must not fire"
        assert "OK 1 watched, 0 fired" in out

    def test_a_tool_line_carrying_protocol_words_never_classifies(
        self, tmp_path, capsys, monkeypatch
    ):
        """Contract 2e: a tool-call row is not a protocol message.

        ``role`` is the only thing separating a tool card from a spoken line. The tool
        line carries ``STANDDOWN``, ``PR:`` and an error phrase: none may classify."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-tool"])
        self._transcript(
            tmp_path / "sessions",
            "s-tool",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "reading the diff"},
                {
                    "role": "tool",
                    "content": (
                        "🔧 monitor_start message=STANDDOWN: done — PR: https://x "
                        "(retry after dispatch failure)"
                    ),
                },
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "s-tool" not in out, "tool text must not classify"
        for tag in ("STANDDOWN", "PR ", "ERR"):
            assert tag not in out
        assert "OK 1 watched, 0 fired" in out

    def test_a_spoken_report_still_fires_with_a_tool_row_after_it(
        self, tmp_path, capsys, monkeypatch
    ):
        """The other half of 2e: dropping tool rows must not drop the REPORT.
        A worker that files ``GREEN:`` and then calls one more tool is still
        green, and tool rows outnumber spoken ones about two to one."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-green-then-tool"])
        self._transcript(
            tmp_path / "sessions",
            "s-green-then-tool",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "GREEN: https://x/pull/1 abc123 all lanes clean"},
                {"role": "tool", "content": "🔧 autonudge_stop"},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "s-green-then-tool" in out and "GREEN" in out

    def test_idle_alert_fires_past_threshold(self, tmp_path, capsys, monkeypatch):
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-idle"], idle_alert_secs=100)
        self._session(tmp_path / "sessions", "s-idle", "WORKING: still here", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "IDLE" in out

    def test_error_tail_flags_err(self, tmp_path, capsys, monkeypatch):
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-err"])
        self._session(tmp_path / "sessions", "s-err", "Bedrock is throttling requests")
        out = self._run(mod, cfg, capsys)
        assert "ERR" in out

    def test_missing_transcript_reports_gone(self, tmp_path, capsys, monkeypatch):
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-vanished"])
        out = self._run(mod, cfg, capsys)
        assert "GONE" in out and "s-vanished" in out

    def test_fired_lines_carry_metadata_never_transcript_text(self, tmp_path, capsys, monkeypatch):
        """The fired line is key/age/tag/digest ONLY: transcript-derived text
        must never cross into the caller's context, whatever keys the
        (agent-authored) config watches. Content goes through the
        workspace-authorized session tools instead."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        self._session(
            tmp_path / "sessions", "s-1", "GREEN: PR #5 private-payload-XYZZY https://x head abc"
        )
        out = self._run(mod, cfg, capsys)
        assert "GREEN" in out and "d=" in out
        assert "private-payload-XYZZY" not in out
        assert "PR #5" not in out

    def test_mark_handled_suppresses_until_payload_changes(self, tmp_path, capsys, monkeypatch):
        """The handled-set replaces the run's hand-grown grep exclusion: an acted
        signal stays quiet, and a NEW payload under the same tag re-fires."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        self._session(tmp_path / "sessions", "s-1", "GREEN: PR #5 https://x head abc")
        out = self._run(mod, cfg, capsys)
        assert "GREEN" in out
        self._run(mod, cfg, capsys, "--mark-handled", "s-1", "GREEN", self._digest_of(out, "s-1"))
        out = self._run(mod, cfg, capsys)
        assert "GREEN" not in out and "0 fired" in out
        self._session(tmp_path / "sessions", "s-1", "GREEN: PR #9 https://y head def")
        assert "GREEN" in self._run(mod, cfg, capsys)  # new payload = new signal

    def test_mark_with_stale_digest_is_refused(self, tmp_path, capsys, monkeypatch):
        """A new same-tag payload landing between probe and mark must NOT be
        digested unseen: the mark is refused (exit 3), nothing is suppressed,
        and the unseen payload still fires on the next probe."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        self._session(tmp_path / "sessions", "s-1", "GREEN: PR #5 https://x head abc")
        stale = self._digest_of(self._run(mod, cfg, capsys), "s-1")
        self._session(tmp_path / "sessions", "s-1", "GREEN: PR #9 https://y head def")
        assert mod.main(["--config", str(cfg), "--mark-handled", "s-1", "GREEN", stale]) == 3
        capsys.readouterr()
        assert "GREEN" in self._run(mod, cfg, capsys)  # unseen payload still fires

    def _exe_agrees_with_argv0(self, mod, monkeypatch, proc) -> None:
        """Make the fake /proc answer ``exe`` consistently with ``argv[0]``.

        The wrapper exemption requires the kernel's ``exe`` answer, so a fake /proc
        without it models a process hiding its identity and the option-parsing tests
        would assert the spoof path. Fakes ``os.readlink`` because creating symlinks
        needs a privilege this host withholds (WinError 1314).
        """
        real = os.readlink

        def fake_readlink(path, *a, **kw):
            text = str(path)
            if text.endswith("exe"):
                pid_dir = Path(text).parent
                argv0 = (pid_dir / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")[0]
                return argv0 if argv0.startswith("/") else f"/bin/{argv0}"
            return real(path, *a, **kw)

        monkeypatch.setattr(mod.os, "readlink", fake_readlink)

    def test_banned_process_scan_reports_matches(self, tmp_path, capsys, monkeypatch):
        mod = self._mod()
        proc = tmp_path / "proc"
        (proc / "4242").mkdir(parents=True)
        (proc / "4242" / "cmdline").write_bytes(
            b"python\x00-m\x00pytest\x00-n\x004\x00--token\x00secret-token\x00test\x00"
        )
        (proc / "4343").mkdir(parents=True)
        (proc / "4343" / "cmdline").write_bytes(b"pytest\x00-n\x00auto\x00test/x.py\x00")
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=4242" in out  # an explicit count bypasses the budget
        assert "rule=" in out  # which rule fired is reported...
        assert "secret-token" not in out  # ...the argv itself never is
        assert "pid=4343" not in out  # the budgeted -n auto run is legitimate

    def test_every_budgeted_pytest_spelling_is_legitimate(self, tmp_path, capsys, monkeypatch):
        """Budgeted and single-process forms are quiet; an explicit count of two or more
        is reported.

        Here ``auto`` is the bounded spelling (``xdist_budget.py`` sizes it by memory) and
        the gate runner emits it, while ``setup.cfg`` says "an explicit -n <N> bypasses
        the memory budget". ``-n0`` and ``-n 1`` are a single process."""
        mod = self._mod()
        proc = tmp_path / "proc"
        quiet = {
            # The issue's table: gate full-suite argv, gate related-set argv, bare pytest.
            "11": b"python\x00-m\x00pytest\x00-n\x00auto\x00--dist\x00loadgroup\x00test/\x00",
            "12": b"python\x00-m\x00pytest\x00-n\x00auto\x00--dist\x00loadgroup\x00test/x.py\x00",
            "13": b"python\x00-m\x00pytest\x00test/x.py\x00",
            # The single-process spellings.
            "14": b"pytest\x00-n0\x00test/x.py\x00",
            "15": b"python\x00-m\x00pytest\x00-n0\x00test/x.py\x00-x\x00-q\x00",
            "16": b"pytest\x00-n\x000\x00test/x.py\x00",
            "17": b"pytest\x00-n=0\x00test/x.py\x00",
            "18": b"pytest\x00-n\x001\x00test/x.py\x00",
            "19": b"pytest\x00--numprocesses=0\x00test/x.py\x00",
            "20": b"pytest\x00--numprocesses\x001\x00test/x.py\x00",
            # The other hook-resolved spellings.
            "21": b"pytest\x00-n\x00logical\x00test/x.py\x00",
            "22": b"pytest\x00--numprocesses\x00auto\x00test/x.py\x00",
            "23": b"pytest\x00-nauto\x00test/x.py\x00",
            "24": b"pytest\x00-n=auto\x00test/x.py\x00",
            # argparse last-wins: the effective count is auto.
            "25": b"pytest\x00-n\x004\x00-n\x00auto\x00test/x.py\x00",
        }
        loud = {
            # The issue's table: the budget bypass, in every spelling.
            "31": b"pytest\x00-n\x0032\x00test\x00",
            "32": b"pytest\x00-n=4\x00test/x.py\x00",
            "33": b"pytest\x00-n4\x00test/x.py\x00",
            "34": b"pytest\x00-n\x002\x00test/x.py\x00",
            "35": b"pytest\x00--numprocesses=4\x00test/x.py\x00",
            "36": b"pytest\x00--numprocesses\x002\x00test/x.py\x00",
            "37": b"python\x00-m\x00pytest\x00-n\x008\x00--dist\x00loadgroup\x00test/\x00",
            # argparse last-wins: the effective count is 4.
            "38": b"pytest\x00-n\x00auto\x00-n\x004\x00test/x.py\x00",
            # A value pytest rejects at argument parsing: fail-closed for a monitoring
            # control, so it reports rather than exonerates.
            "39": b"pytest\x00-n\x004abc\x00test/x.py\x00",
        }
        for pid, argv in {**quiet, **loud}.items():
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        reported = {
            ln.split("pid=")[1].split()[0] for ln in out.splitlines() if "BANNED pid=" in ln
        }
        assert reported & set(quiet) == set(), sorted(reported & set(quiet))
        assert set(loud) <= reported, sorted(set(loud) - reported)

    def test_readout_separates_a_whole_suite_run_from_a_path_scoped_one(
        self, tmp_path, capsys, monkeypatch
    ):
        """The rule alone cannot rank two matches, and they warrant opposite acts.

        A whole-suite run is worth interrupting; a one-file run merely omitted a flag.
        Trap: ``/usr/bin/python3`` carries a separator, so reading argv from index 0
        would call every POSIX run path-scoped."""
        mod = self._mod()
        proc = tmp_path / "proc"
        for pid, argv in (
            # Absolute interpreter, no target: separators exist but only before
            # the runner token, so this must still read as a whole-suite run.
            ("21", b"/usr/bin/python3\x00-m\x00pytest\x00-n4\x00-q\x00"),
            ("22", b"pytest\x00-n4\x00-q\x00test/x.py\x00"),  # a plain file target
            ("23", b"pytest\x00-n4\x00-q\x00test/x.py::test_one\x00"),  # a node id
            ("24", b"pytest\x00-n4\x00-q\x00-k\x00some_name\x00"),  # a selector, no path
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        line = {
            ln.split("pid=")[1].split()[0]: ln for ln in out.splitlines() if "BANNED pid=" in ln
        }
        assert "scope=suite" in line["21"], line["21"]
        for narrowed in ("22", "23", "24"):
            assert "scope=paths" in line[narrowed], line[narrowed]
        # The classification is DERIVED from the argv; no argument may ride out on
        # it. `some_name` is a selector the operator chose and is as sensitive as
        # any other argument, so its absence is asserted alongside the verdict.
        assert "some_name" not in out
        assert "test/x.py" not in out

    def test_a_bare_directory_target_reads_as_suite_on_purpose(self, tmp_path, capsys, monkeypatch):
        """``pytest test`` is path-scoped and is reported as ``suite`` anyway, on purpose.

        A bare token cannot be told from an option's value (``--token secret test``), so
        counting it as a target would let a whole-suite run print ``scope=paths``.
        Over-stating severity is the fail-closed direction."""
        mod = self._mod()
        proc = tmp_path / "proc"
        (proc / "31").mkdir(parents=True)
        (proc / "31" / "cmdline").write_bytes(
            b"python\x00-m\x00pytest\x00-n4\x00--token\x00sec\x00test\x00"
        )
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "scope=suite" in out
        assert "sec" not in out.replace("scope=", "")  # the value never escapes

    def test_scope_admits_it_cannot_tell_rather_than_guessing(self, tmp_path, capsys, monkeypatch):
        """A custom rule can match a cmdline naming no runner token this knows.

        Reporting ``suite`` there would invent the highest severity from no
        evidence, and ``paths`` would invent the lowest. ``unknown`` is the only
        honest third answer, and a reader who sees it knows to look rather than
        trusting a fabricated rank."""
        mod = self._mod()
        proc = tmp_path / "proc"
        (proc / "41").mkdir(parents=True)
        (proc / "41" / "cmdline").write_bytes(b"some-runner\x00--all\x00")
        cfg = self._config(tmp_path, monkeypatch, [], banned_process_res=[r"\bsome-runner\b"])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=41" in out
        assert "scope=unknown" in out

    def test_the_two_disclosed_residuals_land_on_the_declared_side(
        self, tmp_path, capsys, monkeypatch
    ):
        """The classifier's docstring names two forms it gets approximately right; pin both.

        ``--pyargs kiro_crew.mod`` reads ``suite`` (fail-closed). A module flag glued onto
        the runner name reads ``unknown``, and the default rule needs a word boundary
        there, so only a custom rule reaches it."""
        mod = self._mod()
        proc = tmp_path / "proc"
        glued = b"-m" + b"pytest"
        for pid, argv in (
            ("61", b"pytest\x00-n4\x00--pyargs\x00kiro_crew.mod\x00"),
            ("62", b"/usr/bin/python3\x00" + glued + b"\x00-q\x00"),
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        cfg = self._config(
            tmp_path,
            monkeypatch,
            [],
            banned_process_res=[glued.decode(), *mod.DEFAULT_BANNED_RES],
        )
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        line = {
            ln.split("pid=")[1].split()[0]: ln for ln in out.splitlines() if "BANNED pid=" in ln
        }
        assert "scope=suite" in line["61"], line["61"]
        assert "scope=unknown" in line["62"], line["62"]
        # The package name is an argument like any other and must not ride out.
        assert "kiro_crew.mod" not in out

    def test_an_option_value_is_not_a_target(self, tmp_path, capsys, monkeypatch):
        """``--cov src/kiro_crew`` and ``-W ignore::X`` are whole-suite runs.

        Both carry a separator or ``::`` in the value of an option, so without
        ``_VALUE_TAKING_OPTS`` they would print the low-priority ``scope=paths``."""
        mod = self._mod()
        proc = tmp_path / "proc"
        for pid, argv in (
            # A path-shaped value: the separator belongs to `--cov`, not a target.
            ("51", b"/usr/bin/python3\x00-m\x00pytest\x00-n4\x00--cov\x00src/kiro_crew\x00-q\x00"),
            # A `::`-shaped value: a warning filter, not a node id.
            ("52", b"pytest\x00-n4\x00-W\x00ignore::DeprecationWarning\x00-q\x00"),
            # An `--ignore <path>` narrows nothing about the run's fan-out.
            ("53", b"pytest\x00-n4\x00--ignore\x00test/slow\x00"),
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        line = {
            ln.split("pid=")[1].split()[0]: ln for ln in out.splitlines() if "BANNED pid=" in ln
        }
        for pid in ("51", "52", "53"):
            assert "scope=suite" in line[pid], line[pid]
        # The option values are arguments like any other and must not ride out on
        # the derived word.
        assert "src/kiro_crew" not in out
        assert "DeprecationWarning" not in out
        assert "test/slow" not in out

    #: ``_VALUE_TAKING_OPTS`` as DOCUMENTED, held independently of the module on
    #: purpose. A list read back out of the code under test cannot notice a
    #: DELETION -- it just stops generating that option's case -- so the frozen
    #: copy is what makes a dropped entry a failure rather than a silent gap.
    _DOCUMENTED_VALUE_TAKING_OPTS = frozenset(
        {
            "-c",
            "-n",
            "-o",
            "-p",
            "-r",
            "-W",
            "--basetemp",
            "--confcutdir",
            "--cov",
            "--cov-config",
            "--cov-report",
            "--deselect",
            "--dist",
            "--durations",
            "--ignore",
            "--ignore-glob",
            "--import-mode",
            "--junitxml",
            "--log-file",
            "--log-level",
            "--maxfail",
            "--override-ini",
            "--rootdir",
            "--tb",
            "--tx",
        }
    )

    def test_every_value_taking_option_consumes_its_value(self, tmp_path, capsys, monkeypatch):
        """The three examples above, widened to every documented member.

        Each value carries both a separator and a ``::`` node id, so a dropped table
        entry must read as ``scope=paths``. Driven from the frozen list, not the module,
        because a self-derived list is blind to a dropped entry.
        """
        mod = self._mod()
        assert mod._VALUE_TAKING_OPTS == self._DOCUMENTED_VALUE_TAKING_OPTS
        proc = tmp_path / "proc"
        expected: dict[str, tuple[str, str]] = {}
        for offset, opt in enumerate(sorted(self._DOCUMENTED_VALUE_TAKING_OPTS)):
            pid = str(6100 + offset)
            value = f"vt{offset}/x.py::T"
            expected[pid] = (opt, value)
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(
                b"pytest\x00-n4\x00" + opt.encode() + b"\x00" + value.encode() + b"\x00"
            )
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        line = {
            ln.split("pid=")[1].split()[0]: ln for ln in out.splitlines() if "BANNED pid=" in ln
        }
        for pid, (opt, value) in expected.items():
            assert pid in line, f"{opt}: no BANNED line"
            assert "scope=suite" in line[pid], f"{opt}: {line[pid]}"
            assert value not in out, f"{opt}: the value rode out on the readout"

    def test_a_shell_running_a_command_string_is_not_the_tool_it_names(
        self, tmp_path, capsys, monkeypatch
    ):
        """``bash -c 'cd x && pytest -q y.py'`` is a shell, not the offending pytest.

        The probe walks every /proc entry, so the wrapped tool is caught on its own pid.
        Every spelling the fleet produces is asserted, since the check is a basename
        comparison; each ``exe`` resolves into a system binary directory, as required."""
        mod = self._mod()
        proc = tmp_path / "proc"
        for pid, argv in (
            ("21", b"bash\x00-c\x00cd /x && pytest -n 4 -q test/y.py\x00"),
            ("22", b"/bin/bash\x00-c\x00pytest -n 4 test/y.py\x00"),
            ("23", b"sh\x00-c\x00vitest run\x00"),
            ("24", b"/usr/bin/busybox\x00-c\x00pytest -n4\x00"),
            ("25", b"/usr/bin/bash.exe\x00-c\x00pytest -n 4 -q\x00"),
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        self._exe_agrees_with_argv0(mod, monkeypatch, proc)
        out = self._run(mod, cfg, capsys)
        assert "BANNED" not in out
        assert "banned 0" in out
        for quiet in ("pid=21", "pid=22", "pid=23", "pid=24", "pid=25"):
            assert quiet not in out, quiet

    def test_a_clustered_shell_option_carries_a_command_string_too(
        self, tmp_path, capsys, monkeypatch
    ):
        """``bash -lc 'pytest -q'`` is the same misattribution as ``bash -c``.

        A shell takes its flags GROUPED, so an equality test against ``-c`` let the
        commonest spelling of all straight through -- a login shell -- and the
        conductor would stop a healthy worker on the strength of it. Every cluster
        the fleet plausibly produces is asserted, plus ``--noprofile`` ahead of the
        cluster, because a long option must not end the scan of leading options."""
        mod = self._mod()
        proc = tmp_path / "proc"
        for pid, argv in (
            ("41", b"bash\x00-lc\x00pytest -n 4 -q test/y.py\x00"),
            ("42", b"bash\x00-ic\x00pytest -n 4 test/y.py\x00"),
            ("43", b"sh\x00-euxc\x00vitest run\x00"),
            ("44", b"/bin/bash\x00--noprofile\x00-lc\x00pytest -n4\x00"),
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        self._exe_agrees_with_argv0(mod, monkeypatch, proc)
        out = self._run(mod, cfg, capsys)
        assert "BANNED" not in out
        assert "banned 0" in out
        for quiet in ("pid=41", "pid=42", "pid=43", "pid=44"):
            assert quiet not in out, quiet

    def test_a_dash_c_inside_the_carried_command_string_does_not_decide_it(
        self, tmp_path, capsys, monkeypatch
    ):
        """Only the LEADING option run may suppress, never the shell's payload.

        ``bash /tmp/run.sh -c`` is a shell running a SCRIPT FILE, and the ``-c``
        belongs to that script, not to bash -- so the arg-shaped rule still applies
        to it. Scanning the whole cmdline for ``-c`` would quietly exempt it, which
        is the opposite error to the one the cluster fix repairs."""
        mod = self._mod()
        proc = tmp_path / "proc"
        (proc / "51").mkdir(parents=True)
        (proc / "51" / "cmdline").write_bytes(b"bash\x00/tmp/run.sh\x00-c\x00pytest -n4\x00")
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        self._exe_agrees_with_argv0(mod, monkeypatch, proc)
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=51" in out

    def test_a_busybox_applet_and_an_option_with_an_operand_still_suppress(
        self, tmp_path, capsys, monkeypatch
    ):
        """The two spellings a leading-option scan alone gets wrong.

        ``busybox sh -c`` names its applet in argv[1], and ``-o`` consumes the next entry
        (``bash -o pipefail -c``), so a naive scan never reaches the ``-c``."""
        mod = self._mod()
        proc = tmp_path / "proc"
        for pid, argv in (
            ("61", b"busybox\x00sh\x00-c\x00pytest -n 4 -q test/y.py\x00"),
            ("62", b"/bin/busybox\x00ash\x00-c\x00vitest run\x00"),
            ("63", b"bash\x00-o\x00pipefail\x00-c\x00pytest -n 4 test/y.py\x00"),
            ("64", b"bash\x00-o\x00pipefail\x00-lc\x00pytest -n4\x00"),
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        self._exe_agrees_with_argv0(mod, monkeypatch, proc)
        out = self._run(mod, cfg, capsys)
        assert "BANNED" not in out
        assert "banned 0" in out
        for quiet in ("pid=61", "pid=62", "pid=63", "pid=64"):
            assert quiet not in out, quiet

    def test_a_symlink_invoked_busybox_applet_is_judged_as_the_applet(
        self, tmp_path, capsys, monkeypatch
    ):
        """The standard busybox form must not buy the shell exemption.

        Via a symlink ``argv[0]`` is the applet and ``exe`` is busybox, so ``wget -c URL``
        must not read as a shell holding a command string. pid 103 is a real
        ``busybox sh -c`` wrapper and reports because its rule is custom (see
        ``test_a_custom_banned_rule_does_not_exempt_the_shell_that_wraps_it``)."""
        mod = self._mod()
        proc = tmp_path / "proc"
        for pid, argv in (
            # Symlink form: the applet is argv[0], `-c` is wget's own continue flag.
            ("101", b"/usr/bin/wget\x00-c\x00https://example.invalid/x\x00"),
            # Explicit multi-call dispatch of the same banned applet.
            ("102", b"busybox\x00wget\x00-c\x00https://example.invalid/x\x00"),
            # A genuine wrapper: the command string is the shell's payload.
            ("103", b"busybox\x00sh\x00-c\x00wget -c https://example.invalid/x\x00"),
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        real = os.readlink

        def fake_readlink(path, *a, **kw):
            if str(path).endswith("exe"):
                return "/bin/busybox"  # every one of them IS busybox
            return real(path, *a, **kw)

        monkeypatch.setattr(mod.os, "readlink", fake_readlink)
        cfg = self._config(tmp_path, monkeypatch, [], banned_process_res=[r"\bwget\b"])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=101" in out, out
        assert "BANNED pid=102" in out, out
        assert "BANNED pid=103" in out, out

    def test_a_busybox_shell_wrapper_is_exempt_under_a_built_in_rule(
        self, tmp_path, capsys, monkeypatch
    ):
        """The applet scan still buys the exemption where it is earned.

        ``busybox sh -c 'pytest -q'`` names its applet in argv[1], and resolving it
        is what tells the probe this pid is a shell rather than the pytest its
        command string names. The previous test asserts the same shape REPORTS under
        a custom rule; both directions have to hold, or the applet scan and the
        rule-origin gate cannot be told apart."""
        mod = self._mod()
        proc = tmp_path / "proc"
        (proc / "104").mkdir(parents=True)
        (proc / "104" / "cmdline").write_bytes(
            b"busybox\x00sh\x00-c\x00pytest -n 4 -q test/y.py\x00"
        )
        real = os.readlink

        def fake_readlink(path, *a, **kw):
            if str(path).endswith("exe"):
                return "/bin/busybox"
            return real(path, *a, **kw)

        monkeypatch.setattr(mod.os, "readlink", fake_readlink)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "pid=104" not in out, out
        assert "banned 0" in out, out

    def test_a_custom_banned_rule_does_not_exempt_the_shell_that_wraps_it(
        self, tmp_path, capsys, monkeypatch
    ):
        """A custom rule can name a command too short-lived to be sampled on its own pid.

        Built-in rules name long-running test runners, but a shell that sleeps then runs
        ``curl`` once is visible almost only as the shell, so exempting it loses the
        only sample."""
        mod = self._mod()
        proc = tmp_path / "proc"
        for pid, argv in (
            ("81", b"bash\x00-c\x00sleep 120; curl https://example.invalid/x\x00"),
            ("82", b"/bin/bash\x00-lc\x00curl https://example.invalid/x\x00"),
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        cfg = self._config(tmp_path, monkeypatch, [], banned_process_res=[r"\bcurl\b"])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        self._exe_agrees_with_argv0(mod, monkeypatch, proc)
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=81" in out, out
        assert "BANNED pid=82" in out, out

    def test_a_built_in_rule_still_exempts_its_wrapper_when_custom_rules_exist(
        self, tmp_path, capsys, monkeypatch
    ):
        """Rule ORIGIN decides the exemption, not the mere presence of custom rules.

        A custom list currently REPLACES the built-in one, so this asserts the gate
        is written against the rule that MATCHED rather than against
        ``cfg["banned_process_res"]`` being set at all -- if that config ever becomes
        additive, the built-in rules keep the fix instead of silently losing it."""
        mod = self._mod()
        proc = tmp_path / "proc"
        (proc / "83").mkdir(parents=True)
        (proc / "83" / "cmdline").write_bytes(
            b"bash\x00-c\x00cd /x && pytest -n 4 -q test/y.py\x00"
        )
        cfg = self._config(
            tmp_path,
            monkeypatch,
            [],
            banned_process_res=[r"\bcurl\b", *mod.DEFAULT_BANNED_RES],
        )
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        self._exe_agrees_with_argv0(mod, monkeypatch, proc)
        out = self._run(mod, cfg, capsys)
        assert "pid=83" not in out, out
        assert "banned 0" in out, out

    def test_a_long_option_with_an_operand_does_not_hide_the_command_string(
        self, tmp_path, capsys, monkeypatch
    ):
        """``bash --rcfile /dev/null -c '...'`` is still a wrapper.

        Two bash long options take an operand, which must be skipped too or the scan
        stops on it. The ``--opt=value`` spelling is one entry and must keep working."""
        mod = self._mod()
        proc = tmp_path / "proc"
        for pid, argv in (
            ("81", b"bash\x00--rcfile\x00/dev/null\x00-c\x00pytest -n 4 -q test/y.py\x00"),
            ("82", b"/bin/bash\x00--init-file\x00/tmp/rc\x00-lc\x00vitest run\x00"),
            ("83", b"bash\x00--rcfile=/dev/null\x00-c\x00pytest -n4\x00"),
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        self._exe_agrees_with_argv0(mod, monkeypatch, proc)
        out = self._run(mod, cfg, capsys)
        assert "BANNED" not in out
        assert "banned 0" in out
        for quiet in ("pid=81", "pid=82", "pid=83"):
            assert quiet not in out, quiet

    def test_an_argument_containing_a_space_does_not_forge_an_option(
        self, tmp_path, capsys, monkeypatch
    ):
        """Why the check reads real argv instead of the joined cmdline.

        One argv entry may contain spaces, so a script-path operand embedding ``-c``
        could pose as the shell's flag in a joined string; it must stay a non-wrapper."""
        mod = self._mod()
        proc = tmp_path / "proc"
        (proc / "71").mkdir(parents=True)
        (proc / "71" / "cmdline").write_bytes(b"bash\x00/tmp/a b -c pytest -n4\x00")
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        self._exe_agrees_with_argv0(mod, monkeypatch, proc)
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=71" in out

    def test_a_spoofed_argv0_cannot_disguise_a_real_pytest_as_a_shell(
        self, tmp_path, capsys, monkeypatch
    ):
        """``exec -a bash`` must not buy an exemption.

        argv[0] is chosen by the process; kernel-maintained `/proc/<pid>/exe` decides.
        `os.readlink` is patched because creating a symlink needs a privilege this host
        withholds."""
        mod = self._mod()
        proc = tmp_path / "proc"
        # argv[0] claims bash and carries a -c, so the pre-fix check exempted it.
        (proc / "91").mkdir(parents=True)
        (proc / "91" / "cmdline").write_bytes(b"bash\x00-c\x00pytest -n 4 test/y.py\x00")
        real = os.readlink

        def fake_readlink(path, *a, **kw):
            if str(path).endswith("exe"):
                return "/usr/bin/python3.12"  # what it ACTUALLY is
            return real(path, *a, **kw)

        monkeypatch.setattr(mod.os, "readlink", fake_readlink)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "pid=91" in out, out

    def test_a_genuine_shell_is_still_exempt_when_the_kernel_confirms_it(
        self, tmp_path, capsys, monkeypatch
    ):
        """The negative control: trusting `exe` must not start reporting real wrappers.

        Without it, 'consult exe' could be satisfied by simply reporting everything."""
        mod = self._mod()
        proc = tmp_path / "proc"
        (proc / "92").mkdir(parents=True)
        (proc / "92" / "cmdline").write_bytes(b"bash\x00-c\x00pytest -n 4 test/y.py\x00")
        real = os.readlink

        def fake_readlink(path, *a, **kw):
            if str(path).endswith("exe"):
                return "/usr/bin/bash"
            return real(path, *a, **kw)

        monkeypatch.setattr(mod.os, "readlink", fake_readlink)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "pid=92" not in out, out

    def test_an_unreadable_exe_reports_instead_of_being_exempted(
        self, tmp_path, capsys, monkeypatch
    ):
        """An unreadable `exe` buys no exemption.

        A process can hide `exe` by going non-dumpable (`prctl(PR_SET_DUMPABLE, 0)`), so
        the kernel must positively name a shell. Pids this uid cannot inspect are usually
        another user's, which `_owner_class` sorts into `foreign`/`unknown`."""
        mod = self._mod()
        proc = tmp_path / "proc"
        (proc / "93").mkdir(parents=True)
        (proc / "93" / "cmdline").write_bytes(b"bash\x00-c\x00pytest -n 4 test/y.py\x00")
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        # No `exe` entry, so readlink raises OSError exactly as it does for a process
        # that made itself non-dumpable.
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=93" in out, out

    def test_a_shell_named_file_outside_a_system_bin_dir_is_not_a_shell(
        self, tmp_path, capsys, monkeypatch
    ):
        """A shell basename is a claim about a filename, not about a program.

        The exemption also requires `exe` inside a system binary directory, where writing
        needs root. Copies under `/tmp` and a home directory report; a genuine
        `/usr/bin/bash` doing the same stays exempt."""
        mod = self._mod()
        proc = tmp_path / "proc"
        exes = {
            "111": "/tmp/bash",  # an interpreter copied to a shell-shaped path
            "112": "/home/user/.local/bin/sh",  # same trick, writable without root
            "113": "/usr/bin/bash",  # the real thing: still exempt
        }
        for pid, argv in (
            ("111", b"bash\x00-c\x00pytest -n 4 test/y.py\x00"),
            ("112", b"sh\x00-c\x00pytest -n 4 test/y.py\x00"),
            ("113", b"bash\x00-c\x00pytest -n 4 test/y.py\x00"),
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        real = os.readlink

        def fake_readlink(path, *a, **kw):
            text = str(path)
            if text.endswith("exe"):
                return exes[Path(text).parent.name]
            return real(path, *a, **kw)

        monkeypatch.setattr(mod.os, "readlink", fake_readlink)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=111" in out, out
        assert "BANNED pid=112" in out, out
        assert "pid=113" not in out, out

    def test_a_shell_upgraded_mid_session_keeps_its_wrapper_exemption(
        self, tmp_path, capsys, monkeypatch
    ):
        """procfs marks an unlinked binary, and the marker must not cost the exemption.

        A package upgrade makes `exe` read `/usr/bin/bash (deleted)`. Stripping the marker
        must not widen the exemption: a deleted `pytest` or `/tmp/bash` still reports."""
        mod = self._mod()
        proc = tmp_path / "proc"
        exes = {
            "114": "/usr/bin/bash (deleted)",  # upgraded mid-session: still a shell
            "115": "/usr/bin/pytest (deleted)",  # not a shell, marker or not
            "116": "/tmp/bash (deleted)",  # marker must not bypass the dir gate
        }
        for pid, argv in (
            ("114", b"bash\x00-c\x00pytest -n 4 test/y.py\x00"),
            ("115", b"pytest\x00-n4\x00test/y.py\x00"),
            ("116", b"bash\x00-c\x00pytest -n 4 test/y.py\x00"),
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        real = os.readlink

        def fake_readlink(path, *a, **kw):
            text = str(path)
            if text.endswith("exe"):
                return exes[Path(text).parent.name]
            return real(path, *a, **kw)

        monkeypatch.setattr(mod.os, "readlink", fake_readlink)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "pid=114" not in out, out
        assert "BANNED pid=115" in out, out
        assert "BANNED pid=116" in out, out

    def test_an_interior_empty_argv_entry_is_not_silently_dropped(
        self, tmp_path, capsys, monkeypatch
    ):
        """`cmdline` is NUL-TERMINATED, so only the LAST split element is an artefact.

        An interior empty entry is a real argument. Dropping it re-spaces the joined
        string that a custom `banned_process_res` pattern is matched against, so a
        rule an operator wrote against the real command line quietly stops matching.
        Here the rule needs the double space that the empty entry produces."""
        mod = self._mod()
        proc = tmp_path / "proc"
        (proc / "94").mkdir(parents=True)
        # argv = ["mytool", "", "--full"] -> joined "mytool  --full" (two spaces).
        (proc / "94" / "cmdline").write_bytes(b"mytool\x00\x00--full\x00")
        cfg = self._config(tmp_path, monkeypatch, [], banned_process_res=[r"mytool\s\s--full"])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=94" in out, out

    def test_an_unbudgeted_pytest_run_directly_still_fires(self, tmp_path, capsys, monkeypatch):
        """Skipping shell wrappers must not quiet the case the rule exists for.
        A pytest whose explicit worker count bypasses the budget is reported whether
        it was spawned as the program itself or through the interpreter."""
        mod = self._mod()
        proc = tmp_path / "proc"
        for pid, argv in (
            ("31", b"pytest\x00-n4\x00-q\x00test/y.py\x00"),
            ("32", b"python\x00-m\x00pytest\x00-n\x004\x00test/y.py\x00"),
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=31" in out
        assert "BANNED pid=32" in out

    def test_a_bare_full_suite_vitest_run_still_fires(self, tmp_path, capsys, monkeypatch):
        """``\\bvitest\\b\\s+run\\s*$`` is a statement about the ARGUMENTS -- what
        makes the run a full-suite one is the absence of file arguments after
        ``run``. It stays matched against the whole cmdline, so narrowing the scan
        to program paths is not an option and the rule keeps working."""
        mod = self._mod()
        proc = tmp_path / "proc"
        (proc / "41").mkdir(parents=True)
        (proc / "41" / "cmdline").write_bytes(b"vitest\x00run\x00")
        (proc / "42").mkdir(parents=True)
        (proc / "42" / "cmdline").write_bytes(b"vitest\x00run\x00src/x.test.ts\x00")
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=41" in out
        assert "pid=42" not in out  # a named file is a scoped run

    def test_raw_slot_key_matches_surface_prefixed_transcript(self, tmp_path, capsys, monkeypatch):
        """session_create answers slot keys while the store writes
        ``dashboard_<slot>.jsonl``; a raw key must classify, not read GONE --
        a false GONE reclaims and duplicate-dispatches an active item."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["chat-9-1"])
        self._session(
            tmp_path / "sessions", "dashboard_chat-9-1", "GREEN: PR #7 https://x head abc"
        )
        out = self._run(mod, cfg, capsys)
        assert "GREEN" in out and "GONE" not in out

    def test_sessions_dir_config_key_is_ignored(self, tmp_path, capsys, monkeypatch):
        """Transcripts are read from THIS gateway's own store only: a
        config-chosen sessions dir would let the agent-authored config point
        the probe at another trust domain's transcripts."""
        mod = self._mod()
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        cfg = self._config(tmp_path, monkeypatch, ["s-x"], sessions_dir=str(foreign))
        self._session(foreign, "s-x", "GREEN: PR #8 https://y head def")
        out = self._run(mod, cfg, capsys)
        assert "GONE" in out  # only the derived store is consulted

    def test_typed_misconfiguration_is_malformed_config(self, tmp_path, capsys, monkeypatch):
        """`{"err_res": 1}` is malformed config (exit 2), never an uncaught
        TypeError mid-patrol."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, [], err_res=1)
        assert mod.main(["--config", str(cfg)]) == 2

    def test_traversal_session_keys_are_malformed_config(self, tmp_path, capsys, monkeypatch):
        """A key is a filename stem, never a path: traversal/absolute spellings
        are rejected as malformed config before any file is touched."""
        mod = self._mod()
        outside = tmp_path / "outside.jsonl"
        outside.write_text(
            json.dumps({"role": "assistant", "content": "GREEN: leak"}) + "\n", encoding="utf-8"
        )
        for bad in ("../outside", "/etc/hostname", "a/b"):
            cfg = self._config(tmp_path, monkeypatch, [bad])
            assert mod.main(["--config", str(cfg)]) == 2, bad
        assert mod.main(["--config", str(cfg), "--mark-handled", "../outside", "GONE", "d0"]) == 2

    def test_corrupt_handled_map_degrades_to_empty(self, tmp_path, capsys, monkeypatch):
        """`{"handled": []}` in the state file must read as empty (a handled
        signal re-fires once), never crash the patrol or the mark."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        self._session(tmp_path / "sessions", "s-1", "GREEN: PR #5 https://x head abc")
        Path(f"{cfg}.state.json").write_text('{"handled": []}', encoding="utf-8")
        out = self._run(mod, cfg, capsys)
        assert "GREEN" in out  # probe survives
        self._run(mod, cfg, capsys, "--mark-handled", "s-1", "GREEN", self._digest_of(out, "s-1"))
        assert "GREEN" not in self._run(mod, cfg, capsys)  # and repairs the map

    def test_gone_is_suppressible_via_mark_handled(self, tmp_path, capsys, monkeypatch):
        """An acted-on GONE (item reclaimed) must not re-fire every cycle."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-vanished"])
        out = self._run(mod, cfg, capsys)
        assert "GONE" in out
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-vanished",
            "GONE",
            self._digest_of(out, "s-vanished"),
        )
        assert "GONE" not in self._run(mod, cfg, capsys)

    def test_symlink_out_of_the_store_reads_gone(self, tmp_path, capsys, monkeypatch):
        """A transcript-shaped symlink resolving outside the session store is
        MISSING, never read: the store is the containment boundary."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-link"])
        outside = tmp_path / "outside.jsonl"
        outside.write_text(
            json.dumps({"role": "assistant", "content": "GREEN: leaked"}) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "sessions" / "s-link.jsonl").symlink_to(outside)
        out = self._run(mod, cfg, capsys)
        assert "GONE" in out and "leaked" not in out

    def test_nonfinite_numeric_config_is_malformed(self, tmp_path, capsys, monkeypatch):
        """JSON permits NaN and bools are ints: neither is a usable threshold,
        and int(NaN) would crash the patrol -- both are malformed config."""
        mod = self._mod()
        for bad in ("NaN", "true", "-5"):
            cfg_path = tmp_path / "probe-config.json"
            monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
            monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(tmp_path / "no-proc"))
            cfg_path.write_text('{"sessions": [], "tail_bytes": ' + bad + "}", encoding="utf-8")
            assert mod.main(["--config", str(cfg_path)]) == 2, bad

    def test_malformed_config_exits_2(self, tmp_path, capsys):
        mod = self._mod()
        bad = tmp_path / "bad.json"
        bad.write_text("[]", encoding="utf-8")
        assert mod.main(["--config", str(bad)]) == 2

    def test_invalid_configured_regex_is_malformed_config(self, tmp_path, capsys, monkeypatch):
        """A regex typo is malformed config (exit 2 with a message), never an
        uncaught crash mid-patrol."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, [], banned_process_res=["(unclosed"])
        assert mod.main(["--config", str(cfg)]) == 2

    def test_state_path_config_key_is_ignored(self, tmp_path, capsys, monkeypatch):
        """The handled-set destination is DERIVED from the config path, never
        taken from the config: a config-chosen destination would let this
        no-write agent's one approved writer replace an arbitrary file."""
        mod = self._mod()
        victim = tmp_path / "victim.json"
        victim.write_text("precious", encoding="utf-8")
        cfg = self._config(tmp_path, monkeypatch, ["s-1"], state_path=str(victim))
        self._session(tmp_path / "sessions", "s-1", "GREEN: PR #5 https://x head abc")
        out = self._run(mod, cfg, capsys)
        self._run(mod, cfg, capsys, "--mark-handled", "s-1", "GREEN", self._digest_of(out, "s-1"))
        assert victim.read_text(encoding="utf-8") == "precious"
        assert Path(f"{cfg}.state.json").exists()
