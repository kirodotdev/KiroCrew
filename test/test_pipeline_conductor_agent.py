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

    def test_protocol_tag_fires_and_working_stays_quiet(self, tmp_path, capsys, monkeypatch):
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-green", "s-working"])
        self._session(tmp_path / "sessions", "s-green", "GREEN: PR #5 https://x head abc123")
        self._session(tmp_path / "sessions", "s-working", "WORKING: reproducing")
        out = self._run(mod, cfg, capsys)
        assert "s-green" in out and "GREEN" in out
        assert "s-working" not in out  # WORKING is a heartbeat, not a signal
        assert "OK 2 watched, 1 fired" in out

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
        """`-n 4`, `-n=4`, `-n4` and `--numprocesses=4` are all bounded runs;
        flagging any of them would make the conductor stop a healthy worker
        and discard its active turn."""
        mod = self._mod()
        proc = tmp_path / "proc"
        for pid, argv in (
            ("11", b"pytest\x00-n=4\x00test/x.py\x00"),
            ("12", b"pytest\x00-n4\x00test/x.py\x00"),
            ("13", b"pytest\x00--numprocesses=4\x00test/x.py\x00"),
            ("14", b"pytest\x00-n\x00auto\x00test/x.py\x00"),  # unbounded: fires
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv)
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        for quiet in ("pid=11", "pid=12", "pid=13"):
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
