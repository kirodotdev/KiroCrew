"""Pipeline-conductor agent installer + bundled probe/budget scripts.

The installer tests mirror ``test_conductor_agent.py``'s shape: stub the agents
dir and ``build_agent_config``, run the installer, assert on the JSON it wrote.
The script tests run the real files over fixtures — they are the deterministic
half of the conductor's patrol, so their vocabulary (protocol tags, handled-set
suppression, budget verdicts) is pinned here.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from skill_script_helpers import load_skill_script

from kiro_crew import agent
from kiro_crew.agent_files import (
    OWNED_KIRO_AGENT_FILES,
    PIPELINE_CONDUCTOR_AGENT_FILENAME,
)

SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "pipeline-conductor"
)


class TestPipelineConductorInstaller:
    def _install(self, tmp_path, monkeypatch, *, may_auto_approve=None):
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
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

    def test_prompt_carries_the_verbosity_placeholder(self, tmp_path, monkeypatch):
        """Custom agents get their OWN prompt, so the token must appear here or
        the user's verbosity setting silently never reaches this agent."""
        data = self._install(tmp_path, monkeypatch)
        assert "{{VERBOSITY_BLOCK}}" in data["prompt"]

    def test_prompt_drives_patrol_with_monitor_start_not_wait(self, tmp_path, monkeypatch):
        data = self._install(tmp_path, monkeypatch)
        prompt = " ".join(data["prompt"].split())
        assert "Patrol with `monitor_start`, never with `wait`" in prompt
        assert "autonudge_stop" in prompt

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
        assert "@kirocrew-dashboard/chat_folder_tree" in allowed
        assert "@kirocrew-dashboard/chat_folder_create" in allowed
        for gated in (
            "@kirocrew-dashboard/session_send",
            "@kirocrew-dashboard/session_stop",
            "@kirocrew-dashboard/chat_folder_move_session",
            "@kirocrew-dashboard",
            "execute_bash",
        ):
            assert gated not in allowed, gated
        assert "@kirocrew-dashboard" in data["tools"]  # mounted, so gated verbs still work
        assert "execute_bash" in data["tools"]

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
        """Only kirocrew-core and the hand-built kirocrew-dashboard entry ship;
        inherited third-party servers are dropped from this spec."""
        data = self._install(tmp_path, monkeypatch)
        assert set(data["mcpServers"]) == {"kirocrew-core", "kirocrew-dashboard"}
        assert data["mcpServers"]["kirocrew-dashboard"]["args"] == ["mcp-dashboard"]

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


class TestFleetProbe:
    def _mod(self):
        return load_skill_script("fleet_probe", SKILL_DIR / "scripts" / "fleet_probe.py")

    def _session(self, sessions_dir: Path, key: str, text: str, *, age_secs: int = 0) -> None:
        path = sessions_dir / f"{key}.jsonl"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": text},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        if age_secs:
            stamp = time.time() - age_secs
            os.utime(path, (stamp, stamp))

    def _transcript(
        self, sessions_dir: Path, key: str, rows: list[dict], *, age_secs: int = 0
    ) -> None:
        """``_session`` for a tail whose ROLES matter -- a tool card, an error
        row, a nudge -- rather than one assistant line. The rows are written as
        given so a test can pin the shape the real writers produce."""
        path = sessions_dir / f"{key}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        if age_secs:
            stamp = time.time() - age_secs
            os.utime(path, (stamp, stamp))

    def _config(self, tmp_path: Path, monkeypatch, sessions: list[str], **extra) -> Path:
        """Config file + the env the script derives its paths from: transcripts
        under $KIROCREW_HOME/sessions, /proc behind the test-only env seam --
        neither is a config key (containment: the config is agent-authored)."""
        (tmp_path / "sessions").mkdir(exist_ok=True)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(tmp_path / "no-proc"))
        cfg = {"sessions": sessions, **extra}
        cfg_path = tmp_path / "probe-config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg_path

    def _run(self, mod, cfg_path: Path, capsys, *extra_args: str) -> str:
        rc = mod.main(["--config", str(cfg_path), *extra_args])
        assert rc == 0
        return capsys.readouterr().out

    @staticmethod
    def _digest_of(out: str, key: str) -> str:
        """The d= field of KEY's fired line -- what mark-handled must echo back."""
        line = next(ln for ln in out.splitlines() if key in ln and "d=" in ln)
        match = re.search(r"d=(\S+)", line)
        assert match is not None, line
        return match.group(1)

    @staticmethod
    def _index_of(out: str, key: str) -> int:
        """The i= field of KEY's fired line -- the tail position."""
        line = next(ln for ln in out.splitlines() if key in ln and "i=" in ln)
        match = re.search(r"i=(\d+)", line)
        assert match is not None, line
        return int(match.group(1))

    @staticmethod
    def _handled(cfg_path: Path) -> dict:
        """The handled map out of the DERIVED state file beside the config."""
        state = json.loads(Path(f"{cfg_path}.state.json").read_text(encoding="utf-8"))
        return state["handled"]

    @staticmethod
    def _age_mark(cfg_path: Path, key: str, secs: int) -> None:
        """Backdate KEY's disposition. NOPROGRESS requires the mark to be at least
        one idle budget old, so a test that marks and probes in the same second
        has to move the clock the way real time would."""
        state_path = Path(f"{cfg_path}.state.json")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["handled"][key]["ts"] = int(time.time()) - secs
        state_path.write_text(json.dumps(state), encoding="utf-8")

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
        word -- ``PR #6580 is green ...`` -- is prose, and tagging it invents a
        report nobody filed. Measured over the 60 most recent transcripts on the
        development host, 20 of the 94 assistant rows that matched the old
        ``^<WORD>\\b`` form were prose, 13 of them a bare ``PR #<n>``."""
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

        ``role`` is the transcript's own discriminator -- the presentation class
        is not persisted, so nothing else separates a tool card from a spoken
        line. The tag half already read assistant rows alone; the ERROR half read
        the last row of ANY role, and a tool row is last on roughly one
        transcript in ten, so an error phrase quoted inside a tool title raised
        ERR on a healthy worker. Here the tool line carries ``STANDDOWN``,
        ``PR:`` AND an error phrase: none of the three may classify."""
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

    def test_banned_process_scan_reports_matches(self, tmp_path, capsys, monkeypatch):
        mod = self._mod()
        proc = tmp_path / "proc"
        (proc / "4242").mkdir(parents=True)
        (proc / "4242" / "cmdline").write_bytes(
            b"python\x00-m\x00pytest\x00--token\x00secret-token\x00test\x00"
        )
        (proc / "4343").mkdir(parents=True)
        (proc / "4343" / "cmdline").write_bytes(b"pytest\x00-n\x004\x00test/x.py\x00")
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=4242" in out  # unbounded pytest
        assert "rule=" in out  # which rule fired is reported...
        assert "secret-token" not in out  # ...the argv itself never is
        assert "pid=4343" not in out  # bounded -n 4 run is legitimate

    def test_every_bounded_pytest_spelling_is_legitimate(self, tmp_path, capsys, monkeypatch):
        """`-n 4`, `-n=4`, `-n4`, `--numprocesses=4` and `-n0` are all bounded
        runs; flagging any of them would make the conductor stop a healthy worker
        and discard its active turn.

        ``-n0`` earns its own case because it is the form the fleet is REQUIRED
        to use: it is the repo's documented override (``setup.cfg``: "Override
        with -n0 for debugging"), it is genuinely in-process, and a rule that
        flagged the mandated form would stop every worker obeying it. It is
        covered by the same ``\\d`` branch as ``-n4``, which is asserted here
        rather than assumed."""
        mod = self._mod()
        proc = tmp_path / "proc"
        for pid, argv in (
            ("11", b"pytest\x00-n=4\x00test/x.py\x00"),
            ("12", b"pytest\x00-n4\x00test/x.py\x00"),
            ("13", b"pytest\x00--numprocesses=4\x00test/x.py\x00"),
            ("14", b"pytest\x00-n\x00auto\x00test/x.py\x00"),  # unbounded: fires
            ("15", b"python\x00-m\x00pytest\x00-n0\x00test/x.py\x00-x\x00-q\x00"),
            ("16", b"pytest\x00-n\x000\x00test/x.py\x00"),
            ("17", b"pytest\x00--numprocesses=0\x00test/x.py\x00"),
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        for quiet in ("pid=11", "pid=12", "pid=13", "pid=15", "pid=16", "pid=17"):
            assert quiet not in out, quiet
        assert "pid=14" in out  # -n auto is the unbounded case

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
        outside.write_text(json.dumps({"role": "assistant", "content": "GREEN: leak"}) + "\n")
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

    # ── 2a: the tail index ────────────────────────────────────────────────────

    def test_every_fired_line_carries_the_index_before_the_digest(
        self, tmp_path, capsys, monkeypatch
    ):
        """Field ORDER is part of the contract: the conductor's action table
        parses these lines positionally, so ``i=`` goes before ``d=``."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        self._session(tmp_path / "sessions", "s-1", "GREEN: https://x/pull/1 abc123")
        line = next(ln for ln in self._run(mod, cfg, capsys).splitlines() if "s-1" in ln)
        assert re.search(r"\bi=\d+\s+d=\S+", line), line

    def test_an_unchanged_index_across_two_probes_is_no_progress(
        self, tmp_path, capsys, monkeypatch
    ):
        """The index is the no-progress discriminator: it must hold still while
        the session is silent and move the moment it speaks. A wall-clock age
        cannot answer this -- anything that touches the file ages it."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-1", "BLOCKED: waiting on a ruling")
        first = self._index_of(self._run(mod, cfg, capsys), "s-1")
        # Same transcript, second probe: no progress, so the index must not move.
        assert self._index_of(self._run(mod, cfg, capsys), "s-1") == first
        with (sessions / "s-1.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"role": "assistant", "content": "BLOCKED: still"}) + "\n")
        assert self._index_of(self._run(mod, cfg, capsys), "s-1") == first + 1

    def test_the_index_stays_monotonic_past_the_parse_cap(self, tmp_path, capsys, monkeypatch):
        """Counted from the START of the file, not the start of the parse window.

        A window-relative count saturates once a transcript passes ``tail_bytes``
        and then stays frozen while the session talks -- reading, at exactly the
        sizes real worker sessions reach, as the deadlock the index exists to
        detect. Here the transcript is deliberately far larger than the cap, and
        the rows are the session's OWN so they count.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-big"], tail_bytes=2000)
        sessions = tmp_path / "sessions"
        rows = [{"role": "assistant", "content": "x" * 200} for _ in range(60)]
        rows.append({"role": "assistant", "content": "PROPOSAL: https://x/issues/1"})
        self._transcript(sessions, "s-big", rows)
        first = self._index_of(self._run(mod, cfg, capsys), "s-big")
        assert first == len(rows) - 1, "the index must count from the file start"
        assert first > 20, "a window-relative count would have saturated well below this"
        with (sessions / "s-big.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"role": "assistant", "content": "PROPOSAL: v2"}) + "\n")
        assert self._index_of(self._run(mod, cfg, capsys), "s-big") == first + 1

    def test_mark_handled_records_the_index_and_keeps_its_signature(
        self, tmp_path, capsys, monkeypatch
    ):
        """``--mark-handled KEY TAG DIGEST`` stays exactly three positional
        arguments; the index rides along in the state entry."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        self._session(tmp_path / "sessions", "s-1", "GREEN: https://x/pull/1 abc123")
        out = self._run(mod, cfg, capsys)
        self._run(mod, cfg, capsys, "--mark-handled", "s-1", "GREEN", self._digest_of(out, "s-1"))
        entry = self._handled(cfg)["s-1"]
        assert entry["index"] == self._index_of(out, "s-1")
        assert entry["tag"] == "GREEN"

    # ── 2b: TERMINAL ──────────────────────────────────────────────────────────

    def test_a_terminal_report_then_unprefixed_text_is_terminal_not_idle(
        self, tmp_path, capsys, monkeypatch
    ):
        """A worker that filed STANDDOWN and then wrote one unprefixed line is
        FINISHED. Ageing it into IDLE says the opposite, and the two readings
        call for opposite actions -- close the item, or nudge and reclaim it."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-done"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-done", "STANDDOWN: covered by an already-merged PR")
        out = self._run(mod, cfg, capsys)
        assert "STANDDOWN" in out
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-done",
            "STANDDOWN",
            self._digest_of(out, "s-done"),
        )
        # ... then one unprefixed line, and the clock runs past the idle threshold.
        self._session(sessions, "s-done", "thanks, nothing further from me", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" in out
        assert "IDLE" not in out

    def test_a_resumed_worker_reporting_working_is_not_terminal(
        self, tmp_path, capsys, monkeypatch
    ):
        """TERMINAL replaces IDLE only for a tail with NO protocol prefix.

        ``WORKING:`` is a protocol message and it means active work. A worker
        that stood down, was re-seeded, and is now reporting WORKING must not
        read as finished: TERMINAL closes the item, so this inversion would
        abandon live work. The non-firing set holds BOTH ``-`` and ``WORKING``,
        which is how they came to share a branch."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-resumed"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-resumed", "STANDDOWN: nothing further")
        out = self._run(mod, cfg, capsys)
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-resumed",
            "STANDDOWN",
            self._digest_of(out, "s-resumed"),
        )
        # Re-seeded: the worker is working again and says so. APPENDED, because a
        # real transcript only ever grows -- and the index is a count of produced
        # rows, so a rewrite that happened to keep the count would read as a stall.
        self._transcript(
            sessions,
            "s-resumed",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "STANDDOWN: covered by an already-merged PR"},
                {"role": "assistant", "content": "WORKING: re-seeded, reproducing"},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" not in out, "an active WORKING report must not read as finished"
        assert "s-resumed" not in out  # WORKING is a heartbeat, not a signal
        # And the clock still governs a WORKING tail that then goes silent.
        self._transcript(
            sessions,
            "s-resumed",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "STANDDOWN: covered by an already-merged PR"},
                {"role": "assistant", "content": "WORKING: re-seeded, reproducing"},
                {"role": "assistant", "content": "WORKING: still reproducing"},
            ],
            age_secs=500,
        )
        out = self._run(mod, cfg, capsys)
        assert "IDLE" in out and "TERMINAL" not in out

    # ── 2b extension: sticky BLOCKED ──────────────────────────────────────────

    def test_blocked_survives_the_heartbeats_that_follow_it(self, tmp_path, capsys, monkeypatch):
        """A ruling owed must not be lost to SAMPLING.

        The probe samples; it does not subscribe. The protocol tells a blocked
        worker to keep reporting status, so the worker's own next ``WORKING:``
        overwrites the only message a newest-message classifier reads, and the
        debt becomes invisible on both sides: the worker holds position waiting
        for a ruling, the conductor never learns it owes one. Nothing is
        suppressed here and nothing is deferred -- the signal is simply never
        observed. So ``BLOCKED`` is sticky, and a heartbeat cannot clear it.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-blocked"])
        self._transcript(
            tmp_path / "sessions",
            "s-blocked",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "BLOCKED: need a ruling on the baseline entry"},
                {"role": "assistant", "content": "WORKING: holding position"},
                {"role": "assistant", "content": "WORKING: still holding"},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out, "two heartbeats must not clear a ruling owed"
        # A probe that did NOT mark it handled leaves it owed, so it fires again.
        assert "BLOCKED" in self._run(mod, cfg, capsys)

    def test_a_sticky_blocked_keeps_one_digest_across_new_heartbeats(
        self, tmp_path, capsys, monkeypatch
    ):
        """Sticky must not mean noisy. The digest is keyed on the BLOCKED report's
        own text, so a worker that keeps filing heartbeats does not re-fire the
        same ruling every cycle: the ruling quiets it, and only a genuinely NEW
        blocker re-fires."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-blocked"])
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: need a ruling on scope"},
        ]
        self._transcript(sessions, "s-blocked", rows)
        first = self._digest_of(self._run(mod, cfg, capsys), "s-blocked")
        # More heartbeats arrive. Same unanswered blocker, so the same digest.
        self._transcript(
            sessions,
            "s-blocked",
            rows + [{"role": "assistant", "content": "WORKING: holding position"}],
        )
        out = self._run(mod, cfg, capsys)
        assert self._digest_of(out, "s-blocked") == first
        # Delivering the ruling is what quiets it.
        self._run(mod, cfg, capsys, "--mark-handled", "s-blocked", "BLOCKED", first)
        assert "BLOCKED" not in self._run(mod, cfg, capsys)
        # A genuinely new blocker is a new signal.
        self._transcript(
            sessions,
            "s-blocked",
            rows + [{"role": "assistant", "content": "BLOCKED: a second, different ruling"}],
        )
        assert "BLOCKED" in self._run(mod, cfg, capsys)

    def test_a_real_report_clears_a_sticky_blocked(self, tmp_path, capsys, monkeypatch):
        """Only a heartbeat is transparent. A worker that was blocked and then
        files ``PR``/``GREEN``/``STANDDOWN`` has moved on, and continuing to
        demand a ruling for it would manufacture the opposite error.

        What is asserted is the CLEARING, not what fires in its place. A report
        followed by a heartbeat has always been quiet -- the heartbeat is the
        newest message -- and that is pre-existing behaviour this extension does
        not change; ``test_a_real_report_after_a_blocker_fires_on_its_own`` covers
        the case where the report IS the newest message.
        """
        mod = self._mod()
        for tag, line in (
            ("PR", "PR: https://x/pull/9"),
            ("GREEN", "GREEN: https://x/pull/9 abc123 all lanes clean"),
            ("STANDDOWN", "STANDDOWN: item withdrawn"),
        ):
            key = f"s-moved-{tag.lower()}"
            cfg = self._config(tmp_path, monkeypatch, [key])
            self._transcript(
                tmp_path / "sessions",
                key,
                [
                    {"role": "user", "content": "seed"},
                    {"role": "assistant", "content": "BLOCKED: need a ruling"},
                    {"role": "assistant", "content": line},
                    {"role": "assistant", "content": "WORKING: tidying up"},
                ],
            )
            out = self._run(mod, cfg, capsys)
            assert "BLOCKED" not in out, f"{tag} must clear the ruling owed"
            assert key not in out, f"{tag} then a heartbeat is quiet, as it always was"

    def test_a_real_report_after_a_blocker_fires_on_its_own(self, tmp_path, capsys, monkeypatch):
        """The same clearing, with the report as the newest message: the new tag
        fires and the ruling owed is gone."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-unblocked"])
        self._transcript(
            tmp_path / "sessions",
            "s-unblocked",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "BLOCKED: need a ruling"},
                {"role": "assistant", "content": "PR: https://x/pull/9"},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "PR" in out and "s-unblocked" in out
        assert "BLOCKED" not in out

    def test_unprefixed_text_does_not_clear_a_sticky_blocked(self, tmp_path, capsys, monkeypatch):
        """Unprefixed text is not a report at all, so it clears nothing -- and it
        must not let a blocked worker age into IDLE, which reads as "nudge me"
        when the truth is "answer me"."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-blocked"], idle_alert_secs=100)
        self._transcript(
            tmp_path / "sessions",
            "s-blocked",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "BLOCKED: need a ruling"},
                {"role": "assistant", "content": "still waiting, nothing new from me"},
            ],
            age_secs=500,
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out
        assert "IDLE" not in out and "TERMINAL" not in out

    def test_sticky_blocked_outranks_a_recorded_terminal_disposition(
        self, tmp_path, capsys, monkeypatch
    ):
        """TERMINAL and sticky BLOCKED are deliberately not the same tag: one says
        close me, the other says a ruling is owed. A worker that stood down and
        then found a blocker is owed the ruling, so BLOCKED wins."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-both"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-both", "STANDDOWN: nothing further")
        out = self._run(mod, cfg, capsys)
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-both",
            "STANDDOWN",
            self._digest_of(out, "s-both"),
        )
        self._transcript(
            sessions,
            "s-both",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "STANDDOWN: nothing further"},
                {"role": "assistant", "content": "BLOCKED: actually, one ruling is needed"},
                {"role": "assistant", "content": "WORKING: holding"},
            ],
            age_secs=500,
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out
        assert "TERMINAL" not in out

    # ── 2e extension: decoration on the prefix ────────────────────────────────

    def test_a_bolded_prefix_still_classifies(self, tmp_path, capsys, monkeypatch):
        """A tag matched at position zero is defeated by ANY leading decoration.

        ``**BLOCKED:**`` puts an asterisk where the tag has to be, so the match
        fails and the line falls through as no-prefix -- and on a fresh transcript
        IDLE does not fire either, so the report is not delayed, it is silent.
        Emphasis is ordinary formatting habit rather than a protocol violation, so
        the reader normalises instead of the writer remembering.
        """
        mod = self._mod()
        cases = {
            "s-bold": ("**BLOCKED:** need a ruling on scope", "BLOCKED"),
            "s-quote": ("> BLOCKED: quoted escalation", "BLOCKED"),
            "s-bullet": ("- **GREEN:** https://x/pull/1 abc123", "GREEN"),
            "s-heading": ("### PROPOSAL: https://x/issues/9", "PROPOSAL"),
            "s-numbered": ("1. STANDDOWN: item withdrawn", "STANDDOWN"),
            "s-underscore": ("__PR:__ https://x/pull/2", "PR"),
            "s-ticked": ("`BLOCKED:` need a ruling", "BLOCKED"),
        }
        for key, (line, tag) in cases.items():
            cfg = self._config(tmp_path, monkeypatch, [key])
            self._session(tmp_path / "sessions", key, line)
            out = self._run(mod, cfg, capsys)
            assert tag in out, f"{key}: {line!r} must classify as {tag}"
            assert key in out

    def test_a_bolded_blocked_still_survives_two_heartbeats(self, tmp_path, capsys, monkeypatch):
        """The two rules composing, which is the case that actually goes silent:
        the worker writes a decorated escalation, then obeys the protocol by
        continuing to report status. Emphasis must not lose the tag, and the
        heartbeats must not clear it."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-both"], idle_alert_secs=100)
        self._transcript(
            tmp_path / "sessions",
            "s-both",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "**BLOCKED:** need a ruling before I can push"},
                {"role": "assistant", "content": "WORKING: holding position"},
                {"role": "assistant", "content": "WORKING: still holding"},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out
        assert "IDLE" not in out and "TERMINAL" not in out

    def test_decoration_stripping_is_not_a_substring_search(self, tmp_path, capsys, monkeypatch):
        """The anchor SURVIVES normalisation: decoration comes off the front only.
        A bolded protocol word mid-sentence is a worker talking ABOUT a report,
        and tagging it would fire on every message that mentioned one."""
        mod = self._mod()
        for key, line in (
            ("s-mid", "I think **BLOCKED:** is the right call here"),
            ("s-tail", "waiting on review, then **GREEN:** follows"),
            ("s-prose", "the **PR:** convention confuses me"),
        ):
            cfg = self._config(tmp_path, monkeypatch, [key])
            self._session(tmp_path / "sessions", key, line)
            out = self._run(mod, cfg, capsys)
            assert key not in out, f"{line!r} must not fire"
            assert "OK 1 watched, 0 fired" in out

    def test_a_tool_row_with_a_bolded_prefix_still_never_classifies(
        self, tmp_path, capsys, monkeypatch
    ):
        """2e's original requirement, unaffected by normalisation: a tool row is
        not a protocol message however it is formatted."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-tool"])
        self._transcript(
            tmp_path / "sessions",
            "s-tool",
            [
                {"role": "assistant", "content": "reading the diff"},
                {"role": "tool", "content": "🔧 send_message body=**PR:** https://x/pull/3"},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "s-tool" not in out and "PR" not in out
        assert "OK 1 watched, 0 fired" in out

    def test_the_digest_is_computed_over_what_the_worker_wrote(self, tmp_path, capsys, monkeypatch):
        """Normalisation is for MATCHING only. The digest still covers the raw
        text, so a decorated report and an undecorated one with the same words are
        distinct payloads -- and ``--mark-handled`` keeps working, since it
        digests the same raw text the probe printed."""
        mod = self._mod()
        sessions = tmp_path / "sessions"
        cfg_plain = self._config(tmp_path, monkeypatch, ["s-plain"])
        self._session(sessions, "s-plain", "BLOCKED: need a ruling")
        plain = self._digest_of(self._run(mod, cfg_plain, capsys), "s-plain")
        cfg_bold = self._config(tmp_path, monkeypatch, ["s-styled"])
        self._session(sessions, "s-styled", "**BLOCKED:** need a ruling")
        out = self._run(mod, cfg_bold, capsys)
        styled = self._digest_of(out, "s-styled")
        assert styled != plain, "the digest must reflect the text as written"
        # And the mark round-trips on the decorated payload.
        self._run(mod, cfg_bold, capsys, "--mark-handled", "s-styled", "BLOCKED", styled)
        assert "BLOCKED" not in self._run(mod, cfg_bold, capsys)

    def test_terminal_fires_once_then_suppresses_by_digest(self, tmp_path, capsys, monkeypatch):
        """TERMINAL is an ordinary tag in the firing set: dispositioned once, it
        goes quiet, and a NEW payload re-fires like any other."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-done"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-done", "PROPOSAL: https://x/issues/9")
        out = self._run(mod, cfg, capsys)
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-done", "PROPOSAL", self._digest_of(out, "s-done")
        )
        self._session(sessions, "s-done", "handing back", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" in out
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-done", "TERMINAL", self._digest_of(out, "s-done")
        )
        assert "TERMINAL" not in self._run(mod, cfg, capsys)

    def test_a_non_protocol_disposition_does_not_erase_the_terminal_tag(
        self, tmp_path, capsys, monkeypatch
    ):
        """The defect 2b closes: the handled set keeps ONE entry per key, so a
        later IDLE or GONE disposition used to overwrite the terminal report and
        the finished worker read as wedged again on the next cycle."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-done"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-done", "STANDDOWN: item withdrawn")
        out = self._run(mod, cfg, capsys)
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-done",
            "STANDDOWN",
            self._digest_of(out, "s-done"),
        )
        self._session(sessions, "s-done", "ok", age_secs=500)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-done", "TERMINAL", self._digest_of(out, "s-done")
        )
        assert self._handled(cfg)["s-done"]["settled"]["tag"] == "STANDDOWN"
        # A fresh unprefixed payload is still TERMINAL, not IDLE.
        self._session(sessions, "s-done", "signing off for real", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" in out and "IDLE" not in out

    def test_a_non_terminal_report_still_ages_into_idle(self, tmp_path, capsys, monkeypatch):
        """The negative half: WORKING is not terminal, so a silent worker that
        last said WORKING must still raise IDLE."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-wedged"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-wedged", "WORKING: reproducing")
        # WORKING never fires, so dispose of it the way the conductor would:
        # through a probe cycle that records nothing, then let the clock run.
        self._run(mod, cfg, capsys)
        self._session(sessions, "s-wedged", "hmm", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "IDLE" in out and "TERMINAL" not in out

    # ── 2c: delivery counters ─────────────────────────────────────────────────

    def test_delivery_counters_appear_on_the_ok_line(self, tmp_path, capsys, monkeypatch):
        """Load and memory can both read healthy while the fleet cannot deliver.
        These two counters are the honest admission instrument, so they are
        counted for every watched session -- fired or not."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-init", "s-stall", "s-fine"])
        sessions = tmp_path / "sessions"
        self._transcript(
            sessions,
            "s-init",
            [
                {"role": "assistant", "content": "WORKING: starting"},
                {"role": "error", "content": "initialize timed out on respawn"},
            ],
        )
        self._transcript(
            sessions,
            "s-stall",
            [
                {"role": "assistant", "content": "WORKING: running the suite"},
                {"role": "inject", "content": "[Tool stall - automatic recovery] resume"},
            ],
        )
        self._session(sessions, "s-fine", "WORKING: healthy")
        out = self._run(mod, cfg, capsys)
        assert "deliver init-timeout 1, watchdog 1" in out
        assert "| foreign 0 |" in out  # 2d's counter sits before deliver

    def test_delivery_patterns_are_configurable(self, tmp_path, capsys, monkeypatch):
        mod = self._mod()
        cfg = self._config(
            tmp_path,
            monkeypatch,
            ["s-1"],
            init_timeout_res=["never-delivered-handshake"],
            watchdog_res=["gave-up-waiting"],
        )
        self._transcript(
            tmp_path / "sessions",
            "s-1",
            [
                {"role": "assistant", "content": "WORKING: fine so far"},
                {"role": "user", "content": "never-delivered-handshake"},
                {"role": "inject", "content": "gave-up-waiting"},
            ],
        )
        assert "deliver init-timeout 1, watchdog 1" in self._run(mod, cfg, capsys)

    def test_a_tool_row_never_feeds_the_delivery_counters(self, tmp_path, capsys, monkeypatch):
        """Same rule as classification: an error phrase quoted in a tool card is
        quoted text. Measured over 60 real transcripts, no timeout or
        stall-watchdog line ever landed on a tool row."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        self._transcript(
            tmp_path / "sessions",
            "s-1",
            [
                {"role": "assistant", "content": "WORKING: grepping"},
                {"role": "tool", "content": "🔧 grep initialize timed out"},
            ],
        )
        assert "deliver init-timeout 0, watchdog 0" in self._run(mod, cfg, capsys)

    def test_a_bad_delivery_regex_is_malformed_config(self, tmp_path, capsys, monkeypatch):
        """Same validation as ``err_res`` / ``banned_process_res``: a bad regex
        is malformed config (exit 2), never a crash mid-cycle."""
        mod = self._mod()
        for key in ("init_timeout_res", "watchdog_res"):
            cfg = self._config(tmp_path, monkeypatch, [], **{key: ["(unclosed"]})
            assert mod.main(["--config", str(cfg)]) == 2, key
            cfg = self._config(tmp_path, monkeypatch, [], **{key: [7]})
            assert mod.main(["--config", str(cfg)]) == 2, key

    # ── 2d: cwd-scoped banned scan ────────────────────────────────────────────

    def _proc(self, tmp_path: Path, pid: str, argv: bytes, cwd: Path | None) -> Path:
        """One fake ``/proc/<pid>``. ``cwd`` is written as a SYMLINK because that
        is what the kernel exposes and what the probe reads."""
        proc = tmp_path / "proc"
        (proc / pid).mkdir(parents=True, exist_ok=True)
        (proc / pid / "cmdline").write_bytes(argv)
        if cwd is not None:
            cwd.mkdir(parents=True, exist_ok=True)
            os.symlink(str(cwd), str(proc / pid / "cwd"))
        return proc

    def test_a_banned_match_outside_the_fleet_is_foreign_not_banned(
        self, tmp_path, capsys, monkeypatch
    ):
        """A banned command SHAPE is only a banned OPERATION when the fleet owns
        it. The same unbounded pytest in an unrelated checkout is this machine's
        business, and counting it made the conductor stop a worker that was not
        the offender."""
        mod = self._mod()
        mine = tmp_path / "fleet" / "wt-a"
        theirs = tmp_path / "somebody-else" / "repo"
        proc = self._proc(tmp_path, "5001", b"python\x00-m\x00pytest\x00test/x.py\x00", mine)
        self._proc(tmp_path, "5002", b"python\x00-m\x00pytest\x00test/y.py\x00", theirs)
        cfg = self._config(
            tmp_path, monkeypatch, [], fleet_worktrees=[str(tmp_path / "fleet" / "wt-a")]
        )
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=5001 rule=" in out and "cwd=fleet" in out
        assert "pid=5002" not in out, "an unrelated checkout is not reported"
        assert "banned 1 | foreign 1" in out

    def test_a_delivered_worker_goes_quiet_rather_than_idle(self, tmp_path, capsys, monkeypatch):
        """A delivered, dispositioned worker fires NOTHING once it ages.

        Raised by a review lane as "`TERMINAL` never fires when unprefixed text is
        appended". The first half is true and the harm is not, which is only
        visible by running it. A real transcript APPENDS, so the `GREEN:` row stays
        inside the window: the tag remains `GREEN`, the digest is unchanged, and the
        signal is suppressed. The lane's inference was that the session then ages
        into `IDLE` and the conductor re-dispatches a worker that has already
        delivered. It does not -- the suppressed `GREEN` keeps `IDLE` from being the
        reading too, so the line is silent, which is the correct output for a worker
        whose completion was already reported and acted on.

        `TERMINAL` is therefore the reading for a finished worker whose report is no
        longer VISIBLE while the state file still remembers it. Both paths reach the
        property that matters -- never read as merely idle, never re-dispatched --
        and the prescribed fix (demote any trailing unprefixed row to `-`) would
        make a plainly stated `GREEN:` depend on the state file to be understood.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-done"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "GREEN: https://example.invalid/pull/1 abc1234"},
        ]
        self._transcript(sessions, "s-done", rows)
        out = self._run(mod, cfg, capsys)
        assert "GREEN" in out, "the report itself is the reading while it is visible"
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-done", "GREEN", self._digest_of(out, "s-done")
        )
        # Appends, as a real transcript does: the GREEN row does not go away.
        self._transcript(
            sessions,
            "s-done",
            rows + [{"role": "assistant", "content": "One more note, no prefix."}],
            age_secs=500,
        )
        out = self._run(mod, cfg, capsys)
        assert "IDLE" not in out, "a delivered worker must never be read as merely idle"
        assert "0 fired" in out, "and it must not be re-presented either"

    def test_terminal_covers_the_case_the_transcript_no_longer_states(
        self, tmp_path, capsys, monkeypatch
    ):
        """The other half of the same boundary: once the terminal report scrolls
        out of the window the transcript alone cannot say the worker finished, and
        that is where the remembered tag earns its keep -- `TERMINAL` rather than
        `IDLE`, so an aged finished worker is not nudged."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-gone"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-gone", "GREEN: delivered abc1234")
        out = self._run(mod, cfg, capsys)
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-gone", "GREEN", self._digest_of(out, "s-gone")
        )
        # The report is no longer in the window; only the state file knows.
        self._session(sessions, "s-gone", "trailing chatter with no prefix", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" in out
        assert "IDLE" not in out

    def test_a_reseeded_worker_does_not_read_as_finished(self, tmp_path, capsys, monkeypatch):
        """The compliant re-seed case the protocol requires: a worker handed new
        work states a prefix, and that newer report supersedes the terminal one.

        The transcript is aged so `TERMINAL` WOULD be the reading if the terminal
        report still won; the assertion is that it does not. `WORKING` is a
        heartbeat and deliberately fires nothing, so the session goes quiet rather
        than reporting -- quiet being the correct output for a worker that is
        working.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-again"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._transcript(
            sessions,
            "s-again",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "GREEN: delivered abc1234"},
                {"role": "nudge", "content": "new item assigned"},
                {"role": "assistant", "content": "WORKING: picked up the new item"},
            ],
            age_secs=500,
        )
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" not in out, "a re-seeded worker is not finished"

    def test_any_later_disposition_preserves_the_answered_payload(
        self, tmp_path, capsys, monkeypatch
    ):
        """Not just condition tags: ANY later mark must preserve the payload.

        The first version of this fix keyed on ``IDLE``/``NOPROGRESS`` only, which
        left the same hole open one door down. A session whose ``BLOCKED`` was
        answered can hit an error row, get its ``ERR`` dispositioned, and then post
        a heartbeat -- at which point the error branch stops matching, the sticky
        ``BLOCKED`` is the reading again, and the ``ERR`` mark has overwritten the
        record that the ruling was delivered. The answered ruling re-presents and
        the conductor re-adjudicates a decision it already made.

        A ``GONE`` mark cannot reach this: marking refuses a digest no probe
        reported, and a GONE session has no transcript left to re-read a report
        from. ``ERR`` is the reachable one, which is why it is the one tested.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-err"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: ruling needed"},
        ]
        self._transcript(sessions, "s-err", rows)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-err", "BLOCKED", self._digest_of(out, "s-err")
        )
        assert "BLOCKED" not in self._run(mod, cfg, capsys)
        rows = rows + [{"role": "error", "content": "tool crashed"}]
        self._transcript(sessions, "s-err", rows)
        out = self._run(mod, cfg, capsys)
        assert "ERR" in out
        self._run(mod, cfg, capsys, "--mark-handled", "s-err", "ERR", self._digest_of(out, "s-err"))
        # The heartbeat stops the error row being last, so the sticky BLOCKED is
        # the reading again -- and it is still answered.
        self._transcript(
            sessions, "s-err", rows + [{"role": "assistant", "content": "WORKING: back on it"}]
        )
        assert "BLOCKED" not in self._run(mod, cfg, capsys), "answered rulings stay answered"

    def test_the_shipped_state_shape_upgrades_without_losing_terminal(
        self, tmp_path, capsys, monkeypatch
    ):
        """The upgrade path that actually exists.

        The shipped writer records ``{tag, digest, ts}``, so a session whose last
        mark was a terminal payload still reports ``TERMINAL`` after an upgrade,
        recovered from the entry's own tag -- no nudge for a worker that already
        delivered. An earlier draft also read a ``proto`` field; measurement showed
        no released version ever wrote one (it existed only in intermediate commits
        of this branch), so that path and its test were removed rather than left
        asserting behaviour for an input that cannot occur.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-old"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-old", "trailing chatter with no prefix", age_secs=500)
        state_path = Path(f"{cfg}.state.json")
        state_path.write_text(
            json.dumps({"handled": {"s-old": {"tag": "GREEN", "digest": "d0", "ts": 1}}}),
            encoding="utf-8",
        )
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" in out, "the shipped shape still names a delivered worker"
        assert "IDLE" not in out

    def test_a_legacy_answered_payload_survives_a_later_mark(self, tmp_path, capsys, monkeypatch):
        """The upgrade path must not lose the DIGEST, only the field name changed.

        A state file written before ``settled`` existed records an answered payload
        as the entry's own ``tag``/``digest``. Carrying only the legacy ``proto``
        tag forward keeps the terminal reading but drops the digest, and
        suppression needs both halves -- so a pre-upgrade answered ``BLOCKED``
        followed by any later mark presented the ruling again on the first cycle
        after the upgrade. Raised by a review lane against the compatibility path
        added to prevent exactly this.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-up"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: ruling needed"},
        ]
        self._transcript(sessions, "s-up", rows)
        out = self._run(mod, cfg, capsys)
        digest = self._digest_of(out, "s-up")
        # The pre-upgrade shape: the answered payload IS the entry, no `settled`.
        Path(f"{cfg}.state.json").write_text(
            json.dumps(
                {
                    "handled": {
                        "s-up": {
                            "tag": "BLOCKED",
                            "digest": digest,
                            "ts": int(time.time()),
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        assert "BLOCKED" not in self._run(mod, cfg, capsys), "the legacy record still suppresses"
        rows = rows + [{"role": "error", "content": "tool crashed"}]
        self._transcript(sessions, "s-up", rows)
        out = self._run(mod, cfg, capsys)
        self._run(mod, cfg, capsys, "--mark-handled", "s-up", "ERR", self._digest_of(out, "s-up"))
        assert (
            self._handled(cfg)["s-up"]["settled"]["digest"] == digest
        ), "digest carried, not dropped"
        self._transcript(
            sessions, "s-up", rows + [{"role": "assistant", "content": "WORKING: back on it"}]
        )
        assert "BLOCKED" not in self._run(mod, cfg, capsys), "answered rulings survive the upgrade"

    def test_a_delivered_worker_is_not_reclassified_as_stalled(self, tmp_path, capsys, monkeypatch):
        """A finished worker produces nothing BY DEFINITION, so absence of output
        is not a stall.

        The common shape: a worker files `GREEN:`, the conductor marks it, and the
        transcript then never changes again. The tag stays `GREEN` -- a firing tag,
        so the terminal/idle ladder is never reached -- the report is suppressed by
        digest, and the index is unchanged because nothing more was written. Without
        a guard that makes it `NOPROGRESS`, which re-fires every cycle because it
        expires, sending the conductor to nudge a session that already delivered.

        The neighbouring test escapes this only because its fixture APPENDS a line,
        which moves the index. That is exactly the kind of accident a common-path
        fixture hides, so this one changes nothing after the mark.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-shipped"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-shipped", "GREEN: https://example.invalid/pull/1 abc1234")
        out = self._run(mod, cfg, capsys)
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-shipped",
            "GREEN",
            self._digest_of(out, "s-shipped"),
        )
        # Nothing further is written; only the clock moves.
        self._age_mark(cfg, "s-shipped", 500)
        (sessions / "s-shipped.jsonl").touch()
        os.utime(sessions / "s-shipped.jsonl", (time.time() - 500, time.time() - 500))
        out = self._run(mod, cfg, capsys)
        assert "NOPROGRESS" not in out, "a delivered worker is finished, not stalled"
        assert "0 fired" in out

    def test_a_surfaced_ruling_can_actually_be_dispositioned(self, tmp_path, capsys, monkeypatch):
        """A signal the conductor cannot mark is worse than no signal.

        Surfacing the sticky ``BLOCKED`` from under a handled ``ERR`` prints a
        digest computed over the RULING. ``--mark-handled`` recomputes the payload
        from the transcript, where the error row is still last, so it digests the
        ERR text instead and refuses the mark as a changed payload. The ruling then
        fires every cycle forever, undismissable -- a signal that cannot be
        dispositioned is a worse failure than the one surfacing it fixed.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-mark"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: ruling needed"},
            {"role": "error", "content": "tool crashed"},
        ]
        self._transcript(sessions, "s-mark", rows)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-mark", "ERR", self._digest_of(out, "s-mark")
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out
        # The mark must be ACCEPTED, and the ruling must then go quiet.
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-mark", "BLOCKED", self._digest_of(out, "s-mark")
        )
        assert "BLOCKED" not in self._run(mod, cfg, capsys)

    def test_a_symlinked_fleet_root_cannot_smuggle_a_wider_scope(self, tmp_path, monkeypatch):
        """Validation must judge what the root RESOLVES to, not how it is spelled.

        The classifier compares `realpath` as well as the literal path -- that
        second chance exists so a symlinked worktree still matches -- so a root
        spelled as a symlink to a filesystem root passes a literal-only check and
        then matches every cwd on the host. The widening returns by the same door
        the convenience opened.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, [])
        store = mod._sessions_dir()
        for target in (Path(os.sep), store.parent):
            link = tmp_path / f"link-{abs(hash(str(target))) % 1000}"
            if link.is_symlink() or link.exists():
                link.unlink()
            os.symlink(str(target), str(link))
            cfg.write_text(
                json.dumps({"sessions": [], "fleet_worktrees": [str(link)]}), encoding="utf-8"
            )
            assert mod.main(["--config", str(cfg)]) == 2, target

    def test_a_filesystem_root_as_a_fleet_root_is_refused(self, tmp_path, monkeypatch):
        """A root of ``/`` makes every cwd on the host read as fleet-owned.

        ``fleet_worktrees`` comes from a config the conductor authors, so this is a
        trust boundary and not merely a typo: ``cwd=fleet`` is the one class that
        STOPS a session, so a root that matches everything turns the ownership
        guard into a false-stop generator against unrelated processes -- the exact
        failure scoping the scan was introduced to prevent. Refused at load time
        with a message, the same discipline as the NUL byte.

        Which RULE refuses each of these differs, and mutation testing is what
        showed it: on POSIX ``/`` is always an ancestor of the session store, so
        the store rule catches it and the filesystem-root rule is redundant here;
        ``C:\\`` is not absolute on POSIX, so the absolute-path rule catches that.
        The root rule earns its place on Windows, where the store can sit on
        another drive and a drive root is both absolute and not an ancestor of it.
        This test asserts the OUTCOME -- every one of these is refused on every
        platform -- rather than pretending to isolate one branch.
        """
        mod = self._mod()
        for bad in ("/", "//", "/.", "C:\\", "C:/"):
            cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[bad])
            assert mod.main(["--config", str(cfg)]) == 2, bad

    def test_an_ancestor_of_the_session_store_is_refused_as_a_fleet_root(
        self, tmp_path, monkeypatch
    ):
        """A root that CONTAINS the session store is the same widening one level up.

        It cannot be a worktree -- the store is the conductor's own data directory --
        and accepting it makes every process working anywhere beneath it, including
        the conductor itself, read as a fleet worker eligible to be stopped.
        """
        mod = self._mod()
        # The env that decides where the store lives is set by _config, so the
        # store has to be resolved AFTER it -- reading it first answers for the
        # real home directory and the test silently checks the wrong path.
        cfg = self._config(tmp_path, monkeypatch, [])
        store = mod._sessions_dir()
        for ancestor in (store.parent, store.parent.parent):
            cfg.write_text(
                json.dumps({"sessions": [], "fleet_worktrees": [str(ancestor)]}), encoding="utf-8"
            )
            assert mod.main(["--config", str(cfg)]) == 2, ancestor

    def test_ownership_fails_toward_the_non_stopping_class(self, tmp_path, capsys, monkeypatch):
        """When ownership cannot be established, answer ``unknown``, never ``fleet``.

        ``unknown`` re-injects the directive without stopping anyone; ``fleet``
        stops a worker's turn. The safe direction for a wrong answer is therefore
        toward NOT enforcing, so a root that somehow reaches the classifier without
        being usable must not promote a process to the stopping class.
        """
        mod = self._mod()
        proc = self._proc(tmp_path, "8001", b"pytest\x00-n\x00auto\x00", tmp_path / "elsewhere")
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(tmp_path / "fleet")])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        # A root that cannot be spelled the way any cwd is spelled: the comparison
        # cannot establish ownership, so the answer must be the non-stopping one.
        assert mod._owner_class(proc / "8001", ["\0/broken"], "") != "fleet"
        assert mod._program_class("", [str(tmp_path / "fleet")]) == "unknown"
        out = self._run(mod, cfg, capsys)
        assert "cwd=fleet" not in out

    def test_a_nul_in_a_fleet_root_is_malformed_config_not_a_crash(self, tmp_path, monkeypatch):
        """A NUL byte in a path is malformed config, and the contract for this
        script is that typed misconfiguration exits 2 with a message rather than
        raising. ``os.path`` and ``os.readlink`` both raise ``ValueError`` -- not
        ``OSError`` -- on an embedded NUL, so it escapes the scan's error handling
        and takes the whole cycle down, losing every other session's reading with
        it."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=["/fleet/wt-\0a"])
        assert mod.main(["--config", str(cfg)]) == 2

    def test_a_dispositioned_err_does_not_bury_an_unanswered_ruling(
        self, tmp_path, capsys, monkeypatch
    ):
        """An answered ERR must not mask a ruling nobody has answered.

        The error branch is checked before the sticky walk, so a session that
        reports ``BLOCKED:`` and then hits a persistent error row reads as ``ERR``.
        Once that ``ERR`` is dispositioned it goes quiet by digest -- and while the
        error row stays last, the unanswered ``BLOCKED`` underneath it never
        surfaces. Sticky ``BLOCKED`` exists precisely so a ruling cannot be lost to
        whatever the session said next.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-buried"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: ruling needed"},
            {"role": "error", "content": "tool crashed"},
        ]
        self._transcript(sessions, "s-buried", rows)
        out = self._run(mod, cfg, capsys)
        assert "ERR" in out
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-buried", "ERR", self._digest_of(out, "s-buried")
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out, "the unanswered ruling must surface once the ERR is answered"

    def test_a_report_after_a_delivery_notice_clears_the_counter(
        self, tmp_path, capsys, monkeypatch
    ):
        """The delivery counters count UNRECOVERED failures only.

        A session that filed a protocol report after an init-timeout notice
        evidently got a turn through, so the walk stops at that report. Otherwise a
        recovery report that QUOTES the notice keeps its own session in the
        undelivered column, and the counter becomes a permanent accusation instead
        of an admission.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-recovered"])
        sessions = tmp_path / "sessions"
        self._transcript(
            sessions,
            "s-recovered",
            [
                {"role": "user", "content": "seed"},
                {"role": "error", "content": "initialize timed out after 120s"},
                {"role": "assistant", "content": "WORKING: recovered from initialize timed out"},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "init-timeout 0" in out, "a report after the notice means it was recovered"

    def _venv(self, root: Path) -> Path:
        """A venv interpreter path with the ``pyvenv.cfg`` marker beside it, which
        is what makes an interpreter attributable to one checkout."""
        (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (root / ".venv" / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
        interpreter = root / ".venv" / "bin" / "python"
        interpreter.write_text("", encoding="utf-8")
        return interpreter

    def test_an_unreadable_cwd_falls_back_to_the_program_path(self, tmp_path, capsys, monkeypatch):
        """Both attributable BANNED lines seen in the field had an unreadable cwd
        and a readable cmdline, so cwd alone is not a sufficient ownership signal.

        ``/proc/<pid>/cwd`` needs the access a debugger would have; the cmdline
        does not. A venv interpreter belongs to the checkout that created it, so
        it attributes the process even when the cwd link cannot be followed.
        """
        mod = self._mod()
        mine = tmp_path / "fleet" / "wt-a"
        theirs = tmp_path / "somebody-else" / "repo"
        theirs.mkdir(parents=True, exist_ok=True)
        their_python = self._venv(theirs)
        # cwd=None is the unreadable case: no symlink for the probe to follow.
        proc = self._proc(
            tmp_path, "7001", f"{their_python}\0-m\0pytest\0test_sandbox_x.py\0".encode(), None
        )
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(mine)])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "pid=7001" not in out, "provably not the fleet's, so not the fleet's problem"
        assert "banned 0 | foreign 1" in out

    def test_a_fleet_interpreter_is_owned_even_with_an_unreadable_cwd(
        self, tmp_path, capsys, monkeypatch
    ):
        """The other direction: a program path UNDER a fleet worktree is
        conclusive, because nothing outside that checkout runs its interpreter."""
        mod = self._mod()
        mine = tmp_path / "fleet" / "wt-a"
        mine.mkdir(parents=True, exist_ok=True)
        my_python = self._venv(mine)
        proc = self._proc(tmp_path, "7002", f"{my_python}\0-m\0pytest\0test/x.py\0".encode(), None)
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(mine)])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=7002 rule=" in out and "cwd=fleet" in out
        assert "banned 1 | foreign 0" in out

    def test_a_system_interpreter_attributes_nothing_and_stays_unknown(
        self, tmp_path, capsys, monkeypatch
    ):
        """The correction that keeps this refinement from muting the signal it
        exists to sharpen.

        Treating "program path not under a fleet worktree" as ``foreign`` looks
        symmetric and is not. A fleet worktree here has NO ``.venv`` and its
        workers run a global ``python3`` shim, so a real banned run INSIDE the
        fleet has a program path outside every worktree. Calling that ``foreign``
        would drop it silently -- the exact harm 2d exists to prevent -- so a
        shared interpreter yields ``unknown`` and the match is still reported.
        """
        mod = self._mod()
        mine = tmp_path / "fleet" / "wt-a"
        shim = tmp_path / "shims" / "python3"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("", encoding="utf-8")
        proc = self._proc(tmp_path, "7003", f"{shim}\0-m\0pytest\0test/x.py\0".encode(), None)
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(mine)])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=7003 rule=" in out and "cwd=unknown" in out
        assert "banned 1 | foreign 0" in out, "no venv means no evidence, not evidence of absence"

    def test_the_program_path_is_read_for_a_decision_and_never_printed(
        self, tmp_path, capsys, monkeypatch
    ):
        """Reading argv is not printing it. The fallback compares the program
        path; a secret riding in an ARGUMENT must still never reach the output."""
        mod = self._mod()
        theirs = tmp_path / "somebody-else" / "repo"
        theirs.mkdir(parents=True, exist_ok=True)
        their_python = self._venv(theirs)
        secret = "hunter2-do-not-print"
        proc = self._proc(
            tmp_path,
            "7004",
            f"{their_python}\0-m\0pytest\0--token={secret}\0-n\0auto\0".encode(),
            None,
        )
        cfg = self._config(
            tmp_path, monkeypatch, [], fleet_worktrees=[str(tmp_path / "fleet" / "wt-a")]
        )
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert secret not in out
        assert str(their_python) not in out, "the program path decides; it is not emitted"
        assert "foreign 1" in out

    def test_a_subdirectory_of_a_fleet_worktree_is_fleet_owned(self, tmp_path, capsys, monkeypatch):
        """Workers run tests from inside the tree, not at its root, so the
        comparison is prefix-wise -- and on a path BOUNDARY, so a sibling named
        ``wt-a-old`` is not swallowed by ``wt-a``."""
        mod = self._mod()
        root = tmp_path / "fleet" / "wt-a"
        proc = self._proc(tmp_path, "6001", b"pytest\x00test/x.py\x00", root / "test" / "deep")
        self._proc(tmp_path, "6002", b"pytest\x00test/y.py\x00", tmp_path / "fleet" / "wt-a-old")
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(root)])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "pid=6001" in out and "cwd=fleet" in out
        assert "pid=6002" not in out
        assert "banned 1 | foreign 1" in out

    def test_a_windows_path_spelling_still_matches_its_fleet_root(self, tmp_path, monkeypatch):
        """Two spellings of one directory must not read as two directories.

        This class of bug is invisible on Linux and misfiled EVERY match on the
        Windows lane: ``os.readlink`` there can answer an extended-length path
        (``\\\\?\\D:\\...``) that no configured root will ever spell. Misfiling
        is not cosmetic -- a fleet-owned banned run reported as ``foreign`` is
        not printed at all, so the conductor never learns of it.

        Only the platform-independent halves are asserted here. Stripping the
        extended prefix is this script's own work, so it is pinned directly. Case
        and separator folding is delegated to ``os.path.normcase``, which is a
        no-op on Linux by design, so what is pinned is the DELEGATION rather than
        Windows semantics this runner cannot exhibit.
        """
        mod = self._mod()
        root = r"D:\a\KiroCrew\fleet\wt-a"
        assert mod._norm_path("\\\\?\\" + root) == mod._norm_path(root)
        assert mod._norm_path("\\\\?\\" + root) == os.path.normcase(os.path.normpath(root))
        # Trailing separators and redundant segments fold on every platform.
        assert mod._norm_path("/fleet/wt-a/") == mod._norm_path("/fleet/wt-a")
        assert mod._norm_path("/fleet/./wt-a") == mod._norm_path("/fleet/wt-a")
        assert mod._norm_path("/fleet/x/../wt-a") == mod._norm_path("/fleet/wt-a")

    def test_a_sibling_root_is_not_swallowed_by_a_prefix_match(self, tmp_path, monkeypatch):
        """The boundary test is a separator, not a bare prefix: ``wt-a-old`` is
        not inside ``wt-a``, and attributing its runs to ``wt-a`` would stop the
        wrong worker."""
        mod = self._mod()
        root = mod._norm_path("/fleet/wt-a")
        assert mod._under(mod._norm_path("/fleet/wt-a"), root)
        assert mod._under(mod._norm_path("/fleet/wt-a/test/deep"), root)
        assert not mod._under(mod._norm_path("/fleet/wt-a-old"), root)
        assert not mod._under(mod._norm_path("/fleet/wt-ab"), root)

    def test_a_symlinked_fleet_root_still_matches(self, tmp_path, capsys, monkeypatch):
        """A worktree reached through a symlink is the same worktree. The literal
        comparison cannot see that, so the classifier gets a second chance
        through ``realpath`` -- a match missed here means a banned run inside the
        fleet is filed as somebody else's and never printed."""
        mod = self._mod()
        real = tmp_path / "real-fleet" / "wt-a"
        real.mkdir(parents=True)
        link = tmp_path / "via-link"
        os.symlink(str(tmp_path / "real-fleet"), str(link))
        # The process reports the REAL path; the config names the symlinked one.
        proc = self._proc(tmp_path, "5100", b"pytest\x00test/x.py\x00", real)
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(link / "wt-a")])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "pid=5100" in out and "cwd=fleet" in out
        assert "banned 1 | foreign 0" in out

    # ── 2a extension: NOPROGRESS ───────────────────────────────────────────────

    def test_noprogress_fires_when_nothing_came_out_since_the_last_action(
        self, tmp_path, capsys, monkeypatch
    ):
        """The probe answers the no-progress question instead of posing it.

        Printing ``i=`` and expecting a human to diff two cycles is a decision
        living in prose: nothing enforces it, so it may never happen. Here the
        session is held WARM by inbound nudges it never answers -- so the clock
        never trips IDLE -- while producing nothing of its own.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-stalled"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: need a ruling"},
        ]
        self._transcript(sessions, "s-stalled", rows)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-stalled",
            "BLOCKED",
            self._digest_of(out, "s-stalled"),
        )
        # Nudged twice. The transcript is fresh, so IDLE cannot see this.
        self._transcript(
            sessions,
            "s-stalled",
            rows
            + [
                {"role": "nudge", "content": "resume; re-state your protocol prefix"},
                {"role": "nudge", "content": "still waiting"},
            ],
        )
        # Not yet: a disposition seconds old would fire this on every key every
        # cycle, since nothing has come out in those seconds by definition.
        assert "NOPROGRESS" not in self._run(mod, cfg, capsys)
        self._age_mark(cfg, "s-stalled", 500)
        out = self._run(mod, cfg, capsys)
        assert "NOPROGRESS" in out
        assert "IDLE" not in out

    def test_one_produced_row_clears_noprogress(self, tmp_path, capsys, monkeypatch):
        """The negative half: anything the session emits is progress, so a single
        message or one tool call clears it. Otherwise the tag would accuse a
        worker that is simply mid-task."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-moving"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: need a ruling"},
        ]
        self._transcript(sessions, "s-moving", rows)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-moving",
            "BLOCKED",
            self._digest_of(out, "s-moving"),
        )
        self._transcript(
            sessions,
            "s-moving",
            rows + [{"role": "nudge", "content": "resume"}, {"role": "tool", "content": "🔧 grep"}],
        )
        assert "NOPROGRESS" not in self._run(mod, cfg, capsys)

    def test_a_named_tag_outranks_noprogress(self, tmp_path, capsys, monkeypatch):
        """A tag that says WHY always wins: a live sticky ``BLOCKED`` names the
        reason, and reporting NOPROGRESS instead would hide a ruling owed behind
        a weaker observation."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-named"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "WORKING: starting"},
        ]
        self._transcript(sessions, "s-named", rows)
        self._run(mod, cfg, capsys)  # WORKING never fires, so nothing is marked
        # Give it a recorded observation via an IDLE disposition, then a blocker.
        self._transcript(sessions, "s-named", rows, age_secs=500)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-named", "IDLE", self._digest_of(out, "s-named")
        )
        self._transcript(
            sessions,
            "s-named",
            rows + [{"role": "assistant", "content": "BLOCKED: found a real blocker"}],
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out and "NOPROGRESS" not in out

    def test_noprogress_is_suppressible_and_needs_a_prior_observation(
        self, tmp_path, capsys, monkeypatch
    ):
        """It is an ordinary tag: dispositioned once it goes quiet. And with no
        recorded observation there is nothing to compare against, so a session the
        conductor has never acted on cannot be stalled yet."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-fresh"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        # Never marked: no recorded index, so no NOPROGRESS however quiet it is.
        self._transcript(
            sessions,
            "s-fresh",
            [{"role": "user", "content": "seed"}, {"role": "nudge", "content": "resume"}],
        )
        assert "NOPROGRESS" not in self._run(mod, cfg, capsys)
        # Give it an observation, then stall it, then dispose of the tag.
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: ruling needed"},
        ]
        self._transcript(sessions, "s-fresh", rows)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-fresh",
            "BLOCKED",
            self._digest_of(out, "s-fresh"),
        )
        self._transcript(sessions, "s-fresh", rows + [{"role": "nudge", "content": "resume"}])
        self._age_mark(cfg, "s-fresh", 500)
        out = self._run(mod, cfg, capsys)
        assert "NOPROGRESS" in out
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-fresh",
            "NOPROGRESS",
            self._digest_of(out, "s-fresh"),
        )
        assert "NOPROGRESS" not in self._run(mod, cfg, capsys)

    def test_a_second_disposition_does_not_destroy_the_first(self, tmp_path, capsys, monkeypatch):
        """An answered report must not re-present because a later tag was marked.

        The handled set holds ONE entry per key, so marking a condition tag
        (``IDLE``/``NOPROGRESS``) over an answered payload tag used to overwrite
        the record that the payload was dealt with -- and the answered ruling then
        fired again, sending the conductor to re-adjudicate something it had
        already decided. The payload disposition is now preserved beside the new
        entry, which is the shape ``proto`` already uses for the terminal reading.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-two"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: ruling needed"},
        ]
        self._transcript(sessions, "s-two", rows)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-two", "BLOCKED", self._digest_of(out, "s-two")
        )
        assert "BLOCKED" not in self._run(mod, cfg, capsys)
        self._transcript(sessions, "s-two", rows + [{"role": "nudge", "content": "resume"}])
        self._age_mark(cfg, "s-two", 500)
        out = self._run(mod, cfg, capsys)
        assert "NOPROGRESS" in out
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-two", "NOPROGRESS", self._digest_of(out, "s-two")
        )
        assert self._handled(cfg)["s-two"]["settled"]["tag"] == "BLOCKED"
        # The answered ruling stays answered, and the stall stays quiet too.
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" not in out and "NOPROGRESS" not in out
        # A NEW blocker is still a new signal: preservation is not deafness.
        self._transcript(
            sessions,
            "s-two",
            rows + [{"role": "assistant", "content": "BLOCKED: a second, different ruling"}],
        )
        assert "BLOCKED" in self._run(mod, cfg, capsys)

    def test_a_row_quoting_transcript_json_does_not_advance_the_index(
        self, tmp_path, capsys, monkeypatch
    ):
        """The needle matches a row's own opening, not text inside one.

        Nudges quote things, and a nudge that quoted a transcript row would
        otherwise advance the index -- defeating the detector in exactly the
        inbound-row case it guards. Valid JSONL already prevents the obvious
        version (``json.dumps`` escapes the inner quotes), so this pins the
        line-anchoring that makes the count correct WITHOUT relying on that.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-quoted"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        base = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: need a ruling"},
        ]
        self._transcript(sessions, "s-quoted", base)
        first = self._index_of(self._run(mod, cfg, capsys), "s-quoted")
        # A nudge whose CONTENT is a transcript row, quoted verbatim.
        self._transcript(
            sessions,
            "s-quoted",
            base
            + [
                {
                    "role": "nudge",
                    "content": 'your last row was {"role": "assistant", "content": "x"}',
                }
            ],
        )
        assert self._index_of(self._run(mod, cfg, capsys), "s-quoted") == first
        # Raw, unescaped: the needle must still only match a line opening.
        path = sessions / "s-quoted.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"role": "user", "content": "x"}) + "\n")
        assert self._index_of(self._run(mod, cfg, capsys), "s-quoted") == first

    def test_an_inbound_nudge_does_not_advance_the_index(self, tmp_path, capsys, monkeypatch):
        """The index counts what the SESSION produced, never what arrived.

        A transcript holds nudges, injected recovery notices and user turns
        alongside the session's own rows. Counting all of them means the
        conductor's own nudge advances the index of the worker it just nudged, so
        a wedged session reads as progress at exactly the moment the conductor
        pokes it -- the false negative the index exists to prevent. Tool rows DO
        count: a session running tools is working even while it is silent.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-poked"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        base = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: waiting on a ruling"},
        ]
        self._transcript(sessions, "s-poked", base)
        first = self._index_of(self._run(mod, cfg, capsys), "s-poked")
        # The conductor nudges it, and a recovery notice is injected. Neither is
        # the worker speaking, so neither is progress.
        self._transcript(
            sessions,
            "s-poked",
            base
            + [
                {"role": "nudge", "content": "resume; re-state your protocol prefix"},
                {"role": "inject", "content": "[Stalled turn - automatic recovery] resume"},
                {"role": "user", "content": "any update?"},
            ],
        )
        assert self._index_of(self._run(mod, cfg, capsys), "s-poked") == first
        # A tool call IS the session working, even with nothing said.
        self._transcript(
            sessions,
            "s-poked",
            base + [{"role": "nudge", "content": "resume"}, {"role": "tool", "content": "🔧 grep"}],
        )
        assert self._index_of(self._run(mod, cfg, capsys), "s-poked") == first + 1

    def test_a_recovered_delivery_failure_stops_being_counted(self, tmp_path, capsys, monkeypatch):
        """The counters answer "is this session undelivered NOW".

        Scanning the window for any historical match keeps a healed session in
        the undelivered column until the notice scrolls out of 200 KB, which
        turns an admission instrument into a permanent accusation. A protocol
        report filed after the notice is proof a turn got through.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-healed"])
        sessions = tmp_path / "sessions"
        self._transcript(
            sessions,
            "s-healed",
            [
                {"role": "error", "content": "initialize timed out on respawn"},
                {"role": "assistant", "content": "still stuck"},
            ],
        )
        assert "deliver init-timeout 1, watchdog 0" in self._run(mod, cfg, capsys)
        # It recovered and said so. The old notice is history, not a live failure.
        self._transcript(
            sessions,
            "s-healed",
            [
                {"role": "error", "content": "initialize timed out on respawn"},
                {"role": "assistant", "content": "WORKING: resumed, re-running the tests"},
            ],
        )
        assert "deliver init-timeout 0, watchdog 0" in self._run(mod, cfg, capsys)

    def test_a_failure_after_the_last_report_is_still_counted(self, tmp_path, capsys, monkeypatch):
        """The negative half: a notice NEWER than the last report is outstanding,
        so recovery is judged by order rather than by mere presence."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-broke"])
        self._transcript(
            tmp_path / "sessions",
            "s-broke",
            [
                {"role": "assistant", "content": "WORKING: running the suite"},
                {"role": "inject", "content": "[Tool stall - automatic recovery] resume"},
            ],
        )
        assert "watchdog 1" in self._run(mod, cfg, capsys)

    def test_the_protocol_regex_is_derived_from_the_tag_set(self, tmp_path, monkeypatch):
        """One list, not two. Every word in ``PROTO_TAGS`` must match in its
        protocol form, and a word that is a PREFIX of another must not swallow it:
        ``PR`` and ``PROPOSAL`` share two characters, so a naive alternation can
        report the shorter one."""
        mod = self._mod()
        for tag in mod.PROTO_TAGS:
            match = mod.PROTO.match(f"{tag}: payload")
            assert match is not None, tag
            assert match.group(1) == tag, f"{tag} matched as {match.group(1)}"
        assert mod.PROTO.match("PROPOSAL: x").group(1) == "PROPOSAL"
        assert mod.PROTO.match("PR: x").group(1) == "PR"

    def test_an_unreadable_cwd_is_unknown_and_still_reported(self, tmp_path, capsys, monkeypatch):
        """A process that exited between the scan and the read, or one owned by
        another user, has no readable cwd. Dropping it would be how a real banned
        run inside the fleet goes unreported, so unknown is reported and
        counted."""
        mod = self._mod()
        proc = self._proc(tmp_path, "7001", b"pytest\x00test/x.py\x00", None)
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(tmp_path / "fleet")])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=7001" in out and "cwd=unknown" in out
        assert "banned 1 | foreign 0" in out

    def test_an_undeclared_fleet_scope_reports_every_match(self, tmp_path, capsys, monkeypatch):
        """No ``fleet_worktrees`` declares no scope. Scoping against an empty set
        would file every match as foreign and mute the banned signal entirely --
        a failure the conductor cannot see -- so unscoped keeps the pre-2d
        behaviour: every match reported, every match counted."""
        mod = self._mod()
        proc = self._proc(tmp_path, "8001", b"pytest\x00test/x.py\x00", tmp_path / "anywhere")
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=8001" in out and "cwd=unknown" in out
        assert "banned 1 | foreign 0" in out

    def test_the_banned_line_still_withholds_argv_under_scoping(
        self, tmp_path, capsys, monkeypatch
    ):
        """A command line can carry credentials or a presigned URL, and this line
        lands in the conductor's model context. Adding the cwd class must not
        smuggle the argv in with it."""
        mod = self._mod()
        mine = tmp_path / "fleet" / "wt-a"
        proc = self._proc(
            tmp_path, "9001", b"pytest\x00--token\x00secret-token\x00test/x.py\x00", mine
        )
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(mine)])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "pid=9001" in out
        assert "secret-token" not in out

    def test_a_relative_fleet_worktree_is_malformed_config(self, tmp_path, capsys, monkeypatch):
        """A relative root can never match an absolute ``/proc/<pid>/cwd``
        target, so every banned run inside the fleet would be filed as foreign.
        Say so at load time instead of silently muting the signal."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=["relative/path"])
        assert mod.main(["--config", str(cfg)]) == 2
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[42])
        assert mod.main(["--config", str(cfg)]) == 2

    # ── 2f: backward compatibility ────────────────────────────────────────────

    def test_a_state_file_from_the_old_version_still_suppresses(
        self, tmp_path, capsys, monkeypatch
    ):
        """The old handled entry has no ``index`` and no ``settled``. Both are
        additive metadata outside the digest, so an old state file must suppress
        exactly as it did -- missing fields read as absent, never a crash."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"], idle_alert_secs=100)
        self._session(tmp_path / "sessions", "s-1", "GREEN: https://x/pull/1 abc123")
        digest = self._digest_of(self._run(mod, cfg, capsys), "s-1")
        # Hand-write the PRE-2a entry shape, the way the shipped version writes it.
        Path(f"{cfg}.state.json").write_text(
            json.dumps({"handled": {"s-1": {"tag": "GREEN", "digest": digest, "ts": 1}}}),
            encoding="utf-8",
        )
        out = self._run(mod, cfg, capsys)
        assert "GREEN" not in out and "0 fired" in out
        # The old entry carries no `settled`, but its own tag IS the last
        # dispositioned report -- so a delivered worker reads TERMINAL rather than
        # ageing into IDLE and being nudged. The old shape is sufficient for this;
        # what it cannot recover is a terminal report the old writer had already
        # overwritten, which is the loss the field exists to stop going forward.
        self._session(tmp_path / "sessions", "s-1", "quiet now", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" in out and "IDLE" not in out

    def test_an_old_config_with_no_new_keys_still_runs(self, tmp_path, capsys, monkeypatch):
        """``--config`` keeps its shape: a config written before 2c/2d has no
        delivery patterns and no fleet scope, and must still produce a full OK
        line off the in-code defaults."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        self._session(tmp_path / "sessions", "s-1", "WORKING: fine")
        out = self._run(mod, cfg, capsys)
        assert re.search(
            r"OK 1 watched, 0 fired \| .*\| banned 0 \| foreign 0 \| "
            r"deliver init-timeout 0, watchdog 0",
            out,
        ), out

    # ── standing behaviours these changes must not break ──────────────────────

    def test_a_handled_idle_mark_expires_and_re_alerts(self, tmp_path, capsys, monkeypatch):
        """An IDLE disposition expires: a nudged-but-still-silent worker has to
        re-alert rather than vanish behind its own disposition.

        The session emits one more line after the mark, so the index moves and
        this stays a test of the CLOCK. A session that produces nothing after a
        disposition is the NOPROGRESS case, which is a different tag with the same
        expiry.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-quiet"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-quiet", "hmm", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "IDLE" in out
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-quiet", "IDLE", self._digest_of(out, "s-quiet")
        )
        # One more line: the session HAS produced something, so this is the clock.
        self._transcript(
            sessions,
            "s-quiet",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "hmm"},
                {"role": "assistant", "content": "still thinking"},
            ],
            age_secs=500,
        )
        out = self._run(mod, cfg, capsys)
        assert "IDLE" in out and "NOPROGRESS" not in out
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-quiet", "IDLE", self._digest_of(out, "s-quiet")
        )
        assert "IDLE" not in self._run(mod, cfg, capsys)  # freshly marked: quiet
        # Age the MARK past the threshold; the worker is still silent.
        state_path = Path(f"{cfg}.state.json")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["handled"]["s-quiet"]["ts"] = int(time.time()) - 500
        state_path.write_text(json.dumps(state), encoding="utf-8")
        assert "IDLE" in self._run(mod, cfg, capsys)

    def test_mark_handled_rejects_a_key_that_is_a_path(self, tmp_path, capsys, monkeypatch):
        """A session key is a filename STEM. The mark path validates it too, or
        the probe's one approved writer becomes an arbitrary-path writer."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        assert mod.main(["--config", str(cfg), "--mark-handled", "../../etc/x", "GREEN", "d"]) == 2

    def test_the_ok_line_reports_available_memory(self, tmp_path, capsys, monkeypatch):
        """``mem`` is read from ``MemAvailable`` in the /proc seam, so the host
        posture half of the OK line is exercised rather than assumed."""
        mod = self._mod()
        proc = tmp_path / "proc"
        proc.mkdir()
        (proc / "meminfo").write_text(
            "MemTotal:       65805864 kB\nMemAvailable:   12582912 kB\n", encoding="ascii"
        )
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        assert "mem 12G" in self._run(mod, cfg, capsys)

    def test_block_shaped_content_classifies_like_a_string(self, tmp_path, capsys, monkeypatch):
        """A row whose ``content`` is a list of text blocks is read by joining the
        blocks. Real transcripts on this host carry plain strings, so this is the
        defensive path -- pinned so a writer that starts emitting blocks does not
        silently read as a session with nothing to say."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-blocks"])
        self._transcript(
            tmp_path / "sessions",
            "s-blocks",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": [{"text": "BLOCKED: need a ruling"}]},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out and "s-blocks" in out


class TestCreditSpend:
    def _mod(self):
        return load_skill_script("credit_spend", SKILL_DIR / "scripts" / "credit_spend.py")

    def _shard(self, usage_dir: Path, name: str, rows: list[dict]) -> None:
        usage_dir.mkdir(parents=True, exist_ok=True)
        (usage_dir / name).write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )

    def _run(self, mod, capsys, *args: str) -> dict:
        assert mod.main(list(args)) == 0
        return json.loads(capsys.readouterr().out)

    def test_sums_credits_and_counts_rows_as_turns(self, tmp_path, capsys):
        """Production rows are per-turn with a literal ``turns: 0`` field
        (measured on real shards), so turns are counted per accepted row --
        summing the field would report zero for every active session."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(
            usage,
            "2026-08-30.jsonl",
            [
                {"_type": "tokens", "slot": "a", "credits": 2.5, "turns": 0},
                {"_type": "tokens", "slot": "b", "credits": 99, "turns": 0},  # not ours
                {"_type": "other", "slot": "a", "credits": 50},  # not a tokens row
                {"_type": "tokens", "slot": "a", "credits": 1.5, "turns": 0},
            ],
        )
        out = self._run(mod, capsys, "--slots", "a", "--usage-dir", str(usage))
        assert out["slots"]["a"]["credits"] == 4.0
        assert out["slots"]["a"]["turns"] == 2  # two accepted rows = two turns
        assert out["total_credits"] == 4.0

    def test_budget_verdicts(self, tmp_path, capsys):
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-30.jsonl", [{"_type": "tokens", "slot": "a", "credits": 40}])
        within = self._run(
            mod, capsys, "--slots", "a", "--budget", "100", "--usage-dir", str(usage)
        )
        assert within["verdict"] == "within" and within["remaining"] == 60.0
        exhausted = self._run(
            mod, capsys, "--slots", "a", "--budget", "40", "--usage-dir", str(usage)
        )
        assert exhausted["verdict"] == "exhausted"

    def test_unmetered_when_no_shard_mentions_the_slots(self, tmp_path, capsys):
        """Absent metering must read as UNKNOWN, never as zero spend: today's
        shards only carry chat-runner turns, so a session outside them burns
        invisibly and 'within budget' would be a false comfort."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-30.jsonl", [{"_type": "tokens", "slot": "other", "credits": 1}])
        out = self._run(
            mod, capsys, "--slots", "ghost", "--budget", "100", "--usage-dir", str(usage)
        )
        assert out["verdict"] == "unmetered"
        assert out["slots"]["ghost"]["unmetered"] == 1.0

    def test_missing_usage_dir_is_unmetered_not_a_crash(self, tmp_path, capsys):
        mod = self._mod()
        out = self._run(
            mod,
            capsys,
            "--slots",
            "a",
            "--budget",
            "5",
            "--usage-dir",
            str(tmp_path / "absent"),
        )
        assert out["verdict"] == "unmetered" and out["shards_scanned"] == 0

    def test_max_shards_keeps_newest(self, tmp_path, capsys):
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-01.jsonl", [{"_type": "tokens", "slot": "a", "credits": 7}])
        self._shard(usage, "2026-08-30.jsonl", [{"_type": "tokens", "slot": "a", "credits": 3}])
        out = self._run(mod, capsys, "--slots", "a", "--usage-dir", str(usage), "--max-shards", "1")
        assert out["total_credits"] == 3.0  # newest shard only
        assert out["truncated"] is True  # and the omission is reported

    def test_default_scan_is_all_shards(self, tmp_path, capsys):
        """The cost bound is opt-in: with no --max-shards every retained shard
        counts, so old spend can never silently vanish from the total."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-01.jsonl", [{"_type": "tokens", "slot": "a", "credits": 7}])
        self._shard(usage, "2026-08-30.jsonl", [{"_type": "tokens", "slot": "a", "credits": 3}])
        out = self._run(mod, capsys, "--slots", "a", "--usage-dir", str(usage))
        assert out["total_credits"] == 10.0 and out["truncated"] is False

    def test_truncated_scan_never_reports_within(self, tmp_path, capsys):
        """Under budget on a partial view is not a verdict: spend older than
        the window could flip it, so the answer is `truncated` — while
        exhaustion is monotone and stands even when truncated."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-01.jsonl", [{"_type": "tokens", "slot": "a", "credits": 90}])
        self._shard(usage, "2026-08-30.jsonl", [{"_type": "tokens", "slot": "a", "credits": 5}])
        partial = self._run(
            mod,
            capsys,
            "--slots",
            "a",
            "--budget",
            "50",
            "--usage-dir",
            str(usage),
            "--max-shards",
            "1",
        )
        assert partial["verdict"] == "truncated"  # 5 < 50 but 90 was skipped
        full = self._run(mod, capsys, "--slots", "a", "--budget", "50", "--usage-dir", str(usage))
        assert full["verdict"] == "exhausted"  # the real answer
        over = self._run(
            mod,
            capsys,
            "--slots",
            "a",
            "--budget",
            "4",
            "--usage-dir",
            str(usage),
            "--max-shards",
            "1",
        )
        assert over["verdict"] == "exhausted"  # monotone: valid even truncated

    def test_unreadable_shard_never_yields_a_complete_within(self, tmp_path, capsys):
        """A shard that cannot be read makes the total incomplete: readable
        spend still counts (exhaustion is monotone), but an under-budget
        answer degrades to `truncated`, never a false `within`."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-29.jsonl", [{"_type": "tokens", "slot": "a", "credits": 5}])
        unreadable = usage / "2026-08-30.jsonl"
        unreadable.mkdir()  # a directory named like a shard -> OSError on read
        out = self._run(mod, capsys, "--slots", "a", "--budget", "50", "--usage-dir", str(usage))
        assert out["verdict"] == "truncated"
        assert out["total_credits"] == 5.0

    def test_torn_row_never_yields_a_complete_within(self, tmp_path, capsys):
        """An interrupted append leaves a torn row: readable spend still counts,
        but the verdict degrades to `truncated` -- a blank line, by contrast,
        is not corruption and costs nothing."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        usage.mkdir(parents=True)
        (usage / "2026-08-29.jsonl").write_text(
            json.dumps({"_type": "tokens", "slot": "a", "credits": 5})
            + "\n\n"  # blank line: benign
            + '{"_type": "tokens", "slot": "a", "cred',  # torn mid-write
            encoding="utf-8",
        )
        out = self._run(mod, capsys, "--slots", "a", "--budget", "50", "--usage-dir", str(usage))
        assert out["verdict"] == "truncated"
        assert out["total_credits"] == 5.0

    def test_mixed_metering_is_unmetered_not_within(self, tmp_path, capsys):
        """One metered slot under budget + one slot with no rows at all: the
        under-budget answer is unknowable, so the verdict is `unmetered` --
        never `within`. Exhaustion still wins when the metered spend alone
        crosses the budget (monotone)."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-29.jsonl", [{"_type": "tokens", "slot": "a", "credits": 5}])
        out = self._run(
            mod, capsys, "--slots", "a,ghost", "--budget", "50", "--usage-dir", str(usage)
        )
        assert out["verdict"] == "unmetered"
        out = self._run(
            mod, capsys, "--slots", "a,ghost", "--budget", "4", "--usage-dir", str(usage)
        )
        assert out["verdict"] == "exhausted"  # metered spend alone crosses it

    def test_invalid_credits_on_matched_row_degrades_verdict(self, tmp_path, capsys):
        """A matched tokens row with NaN/negative/boolean credits is corrupt:
        its spend is unknown, so a complete `within` may not be claimed."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(
            usage,
            "2026-08-29.jsonl",
            [
                {"_type": "tokens", "slot": "a", "credits": 5},
                {"_type": "tokens", "slot": "a", "credits": float("nan")},
            ],
        )
        out = self._run(mod, capsys, "--slots", "a", "--budget", "50", "--usage-dir", str(usage))
        assert out["verdict"] == "truncated"
        assert out["total_credits"] == 5.0

    def test_nonfinite_or_negative_budget_is_malformed(self, tmp_path, capsys):
        """`--budget nan` would compare false against everything and print
        `within`; non-finite and non-positive budgets are malformed input."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-29.jsonl", [{"_type": "tokens", "slot": "a", "credits": 5}])
        for bad in ("nan", "inf", "-10", "0"):
            rc = mod.main(["--slots", "a", "--budget", bad, "--usage-dir", str(usage)])
            assert rc == 2, bad
            capsys.readouterr()

    def test_no_slots_exits_2(self, tmp_path):
        mod = self._mod()
        assert mod.main(["--slots", " , "]) == 2
