"""``pod up --approval`` / ``--crons`` / ``--no-embeddings``: recorded per pod and applied at boot.

The fixtures come from ``pod_helpers.py``.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path

import pytest
from pod_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _cp,
    _fake_node_toolchain,
    _isolate_pod_host_state,
    _ready_worktree,
    _systemd_backend,
    requires_posix_pod_lifecycle,
)

from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig


@requires_posix_pod_lifecycle
class TestBootTimeSettings:
    """``pod up --approval`` / ``--crons`` / ``--no-embeddings`` are persisted per
    pod and applied at boot.

    None of them can ride the unit file: both backends re-enter the pod as
    ``kirocrew pod _run <name>`` with no flags. On systemd one template unit is
    shared by every instance, so it cannot carry per-pod flags; launchd writes a
    per-pod plist but still execs that same flagless argv. So they travel through
    the per-pod env file, exactly as ``SEED`` does.

    ``--approval`` and ``--crons`` land on the gateway ARGV; ``--no-embeddings``
    lands in the gateway's ENV instead, because the setting it expresses is an env
    var the embedder reads rather than a flag the gateway parses. Hence two boot
    helpers below.
    """

    def _booted_argv(
        self, root: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
    ) -> list[str]:
        """Boot a ready pod with *env* merged into its env file; return the exec argv."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(root / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(root / "pods"))
        c = PodConfig.load()
        rt.pin_checkout(c, "x", _ready_worktree(root, "x"))
        if env:
            rt.write_env_file(c, "x", env)
        seen: list[list[str]] = []
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(os, "execve", lambda path, argv, e: seen.append(argv))
        # flake-ok: boot runs the real child probe only where PATH has kiro-cli -- moved verbatim
        rt.boot(c, "x")
        assert len(seen) == 1, "boot did not exec exactly once"
        return seen[0][1:]  # drop argv[0], the venv binary path

    def _booted_env(
        self,
        root: Path,
        monkeypatch: pytest.MonkeyPatch,
        env: dict[str, str],
        *,
        inherit_skip: bool = False,
    ) -> dict[str, str]:
        """Boot a ready pod with *env* merged into its env file; return the exec ENV.

        The sibling of :meth:`_booted_argv` for a setting that travels in the
        environment instead of the argv. Both are needed: asserting one says
        nothing about the other, and an env-only option that accidentally grew an
        argv flag (or the reverse) would pass a single-sided check.
        """
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(root / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(root / "pods"))
        # A value inherited from the runner's own environment would make an
        # assertion about what boot exports unfalsifiable.
        monkeypatch.delenv(rt.SKIP_MODEL_DOWNLOAD_ENV, raising=False)
        if inherit_skip:
            # The boot environment (not the env file) already carries the switch.
            monkeypatch.setenv(rt.SKIP_MODEL_DOWNLOAD_ENV, "1")
        c = PodConfig.load()
        rt.pin_checkout(c, "x", _ready_worktree(root, "x"))
        if env:
            rt.write_env_file(c, "x", env)
        seen: list[dict[str, str]] = []
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(os, "execve", lambda path, argv, e: seen.append(e))
        # flake-ok: boot runs the real child probe only where PATH has kiro-cli -- moved verbatim
        rt.boot(c, "x")
        assert len(seen) == 1, "boot did not exec exactly once"
        return seen[0]

    def test_boot_forwards_the_recorded_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv = self._booted_argv(tmp_path, monkeypatch, {"APPROVAL": "reads"})
        assert argv == ["gateway", "--no-crons", "--no-tunnel", "--approval", "reads"]

    def test_boot_argv_unchanged_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The no-regression pin: a pod created before the APPROVAL key existed,
        # or created without it, boots with no --approval at all rather than
        # picking one.
        argv = self._booted_argv(tmp_path, monkeypatch, {})
        assert argv == ["gateway", "--no-crons", "--no-tunnel"]

    def test_boot_always_refuses_to_publish_a_tunnel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --no-tunnel is UNCONDITIONAL for any checkout that accepts it: unlike
        # CRONS there is no env key to opt a pod back into publishing, because a
        # throwaway instance has no business being reachable off the host. Pinned
        # across every combination the env file can express, so an opt-in cannot
        # be added by accident later.
        for i, env in enumerate(
            (
                {},
                {"CRONS": "1"},
                {"APPROVAL": "yolo"},
                {"CRONS": "1", "APPROVAL": "interactive"},
            )
        ):
            argv = self._booted_argv(tmp_path / f"nt{i}", monkeypatch, env)
            assert "--no-tunnel" in argv, env

    def test_boot_omits_the_flag_for_a_checkout_that_cannot_parse_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        # The control plane builds this argv but the TARGET WORKTREE's gateway
        # executes it, and Dev Fleet worktrees sit at different commits. Handing
        # --no-tunnel to a gateway that predates it makes argparse exit 2, and the
        # unit's Restart=on-failure/RestartSec=5 (no RestartPreventExitStatus)
        # turns that into a restart loop every 5s.
        #
        # So the flag is dropped and the pod STILL BOOTS. Such a checkout simply
        # does not receive the new guarantee -- exactly its behaviour before the
        # flag existed.
        root = tmp_path / "old"
        root.mkdir()
        c = PodConfig.load()
        rt.pin_checkout(c, "x", _ready_worktree(root, "x", flags=False))
        # Seed the HOME with tunnel.enabled=TRUE. boot() must leave it ALONE: a
        # boot-time config re-pin was tried and withdrawn, because
        # KiroCrewConfig.load() deep-merges config.local.json OVER config.json with
        # the overlay winning (and `config set` writes the overlay by default), so
        # pinning this file does not pin the setting -- while writing it does mutate
        # an operator's file. Starting from true is what makes the assertion below
        # able to observe a re-pin if one is ever reintroduced.
        home = c.home_dir("x")
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        (home / "config.json").write_text(json.dumps({"tunnel": {"enabled": True}}))
        seen: list[list[str]] = []
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(os, "execve", lambda path, argv, e: seen.append(argv))
        # flake-ok: boot runs the real child probe only where PATH has kiro-cli -- moved verbatim
        rt.boot(c, "x")

        assert seen, "boot did not exec"
        argv = seen[0][1:]
        assert "--no-tunnel" not in argv
        # It still boots -- an old pod keeps working rather than restart-looping.
        assert argv[0] == "gateway"
        assert "keeps the tunnel behaviour it had" in capsys.readouterr().out
        # And the pod's config is untouched, not rewritten behind the operator.
        assert json.loads((home / "config.json").read_text())["tunnel"]["enabled"] is True

    def test_the_probe_accepts_this_very_repo(self) -> None:
        """The ratchet: THIS checkout must satisfy its own probe.

        Without this, a refactor that moves the gateway flag declarations out of
        ``cli.py`` (or changes how they are quoted) would silently DROP the flag for
        EVERY current-checkout pod with nothing going red -- each one falling back to
        its pre-flag tunnel behaviour while still looking guarded. Failing here is
        the signal to teach ``target_supports_flag`` the new location, not to relax
        it.
        """
        repo_root = Path(rt.__file__).resolve().parents[3]
        assert (repo_root / "src" / "kiro_crew" / "cli.py").is_file(), repo_root
        assert rt.target_supports_flag(repo_root, "--no-tunnel") is True
        assert rt.target_supports_flag(repo_root, "--no-crons") is True
        # A flag this repo does not declare must still read as unsupported, so the
        # probe is discriminating rather than trivially true.
        assert rt.target_supports_flag(repo_root, "--no-such-flag-exists") is False

    def test_the_probe_ignores_a_comment_only_mention(self, tmp_path: Path) -> None:
        # A checkout that merely TALKS about the flag does not parse it. Treating a
        # comment as a declaration would pass the flag and produce exactly the
        # argparse-exit restart loop the probe exists to prevent.
        co = tmp_path / "co"
        (co / "src" / "kiro_crew").mkdir(parents=True)
        cli = co / "src" / "kiro_crew" / "cli.py"

        cli.write_text('# TODO: someday accept "--no-tunnel" here\ngw.add_argument("--x")\n')
        assert rt.target_supports_flag(co, "--no-tunnel") is False

        # Same file, real declaration present -> supported.
        cli.write_text(
            '# TODO: someday accept "--no-tunnel" here\n'
            "gw_parser.add_argument(\n"
            '    "--no-tunnel",\n'
            '    action="store_true",\n'
            ")\n"
        )
        assert rt.target_supports_flag(co, "--no-tunnel") is True

    def test_the_flag_probe_reads_the_target_checkouts_own_source(self, tmp_path: Path) -> None:
        # The probe reads the checkout that will EXECUTE, not this process's own
        # source -- that distinction is the entire point, so it is asserted
        # directly rather than only through boot().
        co = tmp_path / "co"
        (co / "src" / "kiro_crew").mkdir(parents=True)
        cli = co / "src" / "kiro_crew" / "cli.py"

        cli.write_text('gw.add_argument("--no-crons")\n')
        assert rt.target_supports_flag(co, "--no-tunnel") is False

        cli.write_text('gw.add_argument("--no-tunnel")\n')
        assert rt.target_supports_flag(co, "--no-tunnel") is True

        # Unreadable source answers False, so an unparseable checkout degrades to
        # "omit the flag" rather than raising on the boot path.
        assert rt.target_supports_flag(tmp_path / "nope", "--no-tunnel") is False

    def test_boot_forces_interactive_on_an_unknown_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        # The env file is hand-editable, so boot re-validates. It must NOT merely
        # drop the value: omitting --approval leaves approval_mode unset, and the
        # gateway then falls through to cfg.agent.approval_mode, which
        # config/loader.py defaults to "auto" -- auto-approve every tool.
        # Dropping would be the LEAST restrictive outcome, so boot pins
        # interactive explicitly.
        argv = self._booted_argv(tmp_path, monkeypatch, {"APPROVAL": "--not-a-mode"})
        assert argv == ["gateway", "--no-crons", "--no-tunnel", "--approval", "interactive"]
        assert "ignoring unknown APPROVAL" in capsys.readouterr().out

    def test_every_declared_mode_survives_boot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Behavioural pin only: each mode in the enforcement tuple round-trips
        # through the env file into argv. This does NOT guard parser drift --
        # a mode present in cli_parser_admin.py but absent from APPROVAL_MODES is never
        # iterated here. test_cli_approval_choices_match_the_enforcement_tuple
        # covers that.
        for mode in rt.APPROVAL_MODES:
            argv = self._booted_argv(tmp_path / mode, monkeypatch, {"APPROVAL": mode})
            assert argv[-2:] == ["--approval", mode]

    @staticmethod
    def _top_level_cli_ast() -> ast.Module:
        # rt.__file__ is pod/runtime.py, so parent.parent is the package root.
        # encoding is explicit: cli.py contains non-ASCII (emoji in guard
        # messages), and read_text() defaults to the platform locale codec,
        # which is cp1252 on Windows CI and raises UnicodeDecodeError there.
        # The top-level parser is cli.py plus the cli_parser_* registrars it calls.
        package = Path(rt.__file__).parent.parent
        files = [package / "cli.py", *sorted(package.glob("cli_parser_*.py"))]
        src = "\n".join(path.read_text(encoding="utf-8") for path in files)
        return ast.parse(src)

    def test_cli_approval_choices_match_the_enforcement_tuple(self) -> None:
        # cli_parser_admin.py repeats the choices literal instead of importing pod.runtime
        # (the top-level parser must not import pod modules at startup), so the
        # duplication is real. Read the literal argparse actually registers, so a
        # mode added on one side only fails HERE rather than being accepted by
        # argparse and then dropped at boot. Iterating APPROVAL_MODES alone
        # cannot catch that, because the missing mode is not in it.
        choices: list[list[str]] = []
        for node in ast.walk(self._top_level_cli_ast()):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "add_argument"):
                continue
            if not (isinstance(fn.value, ast.Name) and fn.value.id == "pod_up"):
                continue
            if not any(isinstance(a, ast.Constant) and a.value == "--approval" for a in node.args):
                continue
            for kw in node.keywords:
                if kw.arg == "choices" and isinstance(kw.value, ast.List):
                    choices.append([e.value for e in kw.value.elts if isinstance(e, ast.Constant)])
        assert choices == [list(rt.APPROVAL_MODES)], (
            "pod up --approval choices must match runtime.APPROVAL_MODES exactly; parser "
            f"declares {choices}, enforcement tuple is {list(rt.APPROVAL_MODES)}"
        )

    def test_every_pod_subparser_name_is_assigned(self) -> None:
        # A `--crons` edit deleted `pod_down = pod_sub.add_parser(...)`, leaving
        # pod_down undefined: a NameError at parser build. That is invisible to
        # py_compile AND to every test in this file, which imports pod.cli and
        # never the top-level parser. Pin the structure so it cannot recur.
        tree = self._top_level_cli_ast()
        assigned = {
            t.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Name) and t.id.startswith("pod_")
        }
        used = {
            n.value.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id.startswith("pod_")
        }
        assert used <= assigned, f"pod_* names used but never assigned: {sorted(used - assigned)}"

    def _prep_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, active: bool
    ) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_WORKTREES_ROOT", str(tmp_path / "wts"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {})
        monkeypatch.setattr(rt, "require_backend", lambda: None)
        _ready_worktree(tmp_path / "wts", "demo")
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: active)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        return PodConfig.load()

    def test_up_records_the_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo", json=False, seed="", ttl="2h", provision=False, approval="yolo"
            ),
        )
        assert rt.read_env_file(c, "demo").get("APPROVAL") == "yolo"

    def test_up_on_a_running_pod_notes_the_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep_up(tmp_path, monkeypatch, active=True)
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo", json=False, seed="", ttl="2h", provision=False, approval="reads"
            ),
        )
        err = capsys.readouterr().err
        assert "already running" in err and "pod down demo" in err
        # Recorded either way, so the next boot picks it up.
        assert rt.read_env_file(c, "demo").get("APPROVAL") == "reads"

    def test_up_tolerates_a_namespace_without_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``provision`` is read the same defensive way, and hand-built Namespaces
        # (here and in TestUpVerb) must not have to carry every optional key.
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        pod_cli._up(
            c, argparse.Namespace(name="demo", json=False, seed="", ttl="2h", provision=False)
        )
        assert "APPROVAL" not in rt.read_env_file(c, "demo")
        assert "EMBEDDINGS" not in rt.read_env_file(c, "demo")

    def test_up_audits_the_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # `yolo` auto-approves every tool, so the SEL trail must name the mode
        # rather than recording only that a pod came up.
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        seen: list[tuple] = []
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: seen.append(a))
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo", json=False, seed="", ttl="2h", provision=False, approval="yolo"
            ),
        )
        allowed = [a for a in seen if a[:2] == ("pod.up", "allowed")]
        assert allowed, "pod.up allowed was never audited"
        assert "approval=yolo" in allowed[0][2]

    # --- crons -------------------------------------------------------------

    def test_boot_enables_the_scheduler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --no-crons is dropped, which is how the gateway turns the scheduler on.
        argv = self._booted_argv(tmp_path, monkeypatch, {"CRONS": "1"})
        assert argv == ["gateway", "--no-tunnel"]

    def test_boot_accepts_alternative_truthy_spellings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The env file is hand-editable, so the obvious spellings are honoured.
        for i, raw in enumerate(("true", "YES", " on ")):
            argv = self._booted_argv(tmp_path / f"t{i}", monkeypatch, {"CRONS": raw})
            assert argv == ["gateway", "--no-tunnel"], raw

    def test_boot_ignores_an_unrecognised_crons_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        # Falls back to the safer setting (scheduler off, the pre-existing
        # behavior) rather than guessing, and the pod still boots.
        argv = self._booted_argv(tmp_path, monkeypatch, {"CRONS": "maybe"})
        assert argv == ["gateway", "--no-crons", "--no-tunnel"]
        assert "ignoring unrecognised CRONS" in capsys.readouterr().out

    def test_boot_combines_crons_and_approval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv = self._booted_argv(tmp_path, monkeypatch, {"CRONS": "1", "APPROVAL": "reads"})
        assert argv == ["gateway", "--no-tunnel", "--approval", "reads"]

    def test_up_records_crons(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo", json=False, seed="", ttl="2h", provision=False, crons=True
            ),
        )
        assert rt.read_env_file(c, "demo").get("CRONS") == "1"

    def test_up_audits_crons(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A pod with the scheduler on runs work unattended; the trail must say so.
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        seen: list[tuple] = []
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: seen.append(a))
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo", json=False, seed="", ttl="2h", provision=False, crons=True
            ),
        )
        allowed = [a for a in seen if a[:2] == ("pod.up", "allowed")]
        assert allowed, "pod.up allowed was never audited"
        assert "crons=on" in allowed[0][2]

    # --- embeddings --------------------------------------------------------

    def test_the_skip_download_env_name_matches_the_embedder(self) -> None:
        # runtime.py restates the name instead of importing embeddings (which drags
        # the vendored llama.cpp resolution onto the gateway boot path), so this is
        # what keeps the two spellings from drifting. A typo here exports a variable
        # nothing reads and the pod downloads the model anyway -- silently, since a
        # pod with the model IS healthy.
        from kiro_crew import embeddings

        assert rt.SKIP_MODEL_DOWNLOAD_ENV == embeddings._SKIP_DOWNLOAD_ENV

    def test_boot_without_embeddings_skips_the_model_download(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = self._booted_env(tmp_path, monkeypatch, {"EMBEDDINGS": "0"})
        assert env[rt.SKIP_MODEL_DOWNLOAD_ENV] == "1"

    def test_boot_leaves_the_model_download_alone_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A pod whose env file carries no EMBEDDINGS key boots with the variable
        # absent rather than set to any value. Embeddings are ON by default, so the
        # opposite polarity to CRONS: saying nothing must not disable them.
        env = self._booted_env(tmp_path, monkeypatch, {})
        assert rt.SKIP_MODEL_DOWNLOAD_ENV not in env

    def test_boot_accepts_alternative_falsy_spellings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The env file is hand-editable, so the obvious spellings are honoured.
        for i, raw in enumerate(("false", "NO", " off ")):
            env = self._booted_env(tmp_path / f"e{i}", monkeypatch, {"EMBEDDINGS": raw})
            assert env[rt.SKIP_MODEL_DOWNLOAD_ENV] == "1", raw

    def test_boot_ignores_an_unrecognised_embeddings_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Falls back to the default (embeddings ON) rather than
        # guessing, and the pod still boots. Note this is the SAFER answer here for
        # the opposite reason it is with CRONS: a pod that unexpectedly kept its
        # embeddings is a normal pod, whereas one that unexpectedly lost them
        # answers search from a different index while looking perfectly healthy.
        env = self._booted_env(tmp_path, monkeypatch, {"EMBEDDINGS": "maybe"})
        assert rt.SKIP_MODEL_DOWNLOAD_ENV not in env

    def test_boot_announces_the_embedding_light_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        # The journal is the only place this mode is observable after the fact, so a
        # measurement taken against the pod stays attributable.
        self._booted_env(tmp_path, monkeypatch, {"EMBEDDINGS": "0"})
        out = capsys.readouterr().out
        assert "embeddings off" in out and "keyword fallback" in out

    def test_boot_stays_silent_about_embeddings_on_a_normal_pod(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        self._booted_env(tmp_path, monkeypatch, {})
        assert "embeddings off" not in capsys.readouterr().out

    def test_the_embedding_option_changes_no_boot_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # It is an ENV passthrough, not a gateway flag: the gateway has no
        # --no-embeddings to receive. Pinned so a later change cannot quietly start
        # passing one to a target checkout that would reject the whole argv.
        argv = self._booted_argv(tmp_path, monkeypatch, {"EMBEDDINGS": "0"})
        assert argv == ["gateway", "--no-crons", "--no-tunnel"]

    def test_boot_combines_no_embeddings_with_the_other_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = self._booted_env(
            tmp_path, monkeypatch, {"EMBEDDINGS": "0", "CRONS": "1", "APPROVAL": "reads"}
        )
        assert env[rt.SKIP_MODEL_DOWNLOAD_ENV] == "1"

    def test_build_pod_env_writes_the_skip_into_the_returned_env_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The whole promise of the option: a load test runs embedding-free without
        # the operator's own environment or home losing its model. Setting it in
        # os.environ would leak the mode into every later host command in this
        # process, the live gateway included.
        monkeypatch.delenv(rt.SKIP_MODEL_DOWNLOAD_ENV, raising=False)
        cfg = PodConfig.load()
        env = rt.build_pod_env(
            cfg, tmp_path / "home", 7999, tmp_path / "co", skip_model_download=True
        )
        assert env[rt.SKIP_MODEL_DOWNLOAD_ENV] == "1"
        assert rt.SKIP_MODEL_DOWNLOAD_ENV not in os.environ

    def test_the_embed_model_override_env_names_match_the_embedder(self) -> None:
        # Same drift guard as the skip switch: a misspelt override name here is a
        # variable nothing reads, and the pod loads the custom model anyway.
        from kiro_crew import embeddings

        assert set(rt.EMBED_MODEL_OVERRIDE_ENVS) == {
            embeddings._MODEL_PATH_ENV,
            embeddings._MODEL_URL_ENV,
        }

    def test_build_pod_env_drops_an_inherited_custom_model_path_with_the_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The skip switch gates only the DOWNLOAD; resolve_custom_model reads
        # KIROCREW_EMBED_MODEL_PATH first. Inherited, it would make the pod load the
        # operator's GGUF and embed while the journal says it does not -- so the
        # override must leave the mapping together with the switch arriving.
        monkeypatch.delenv(rt.SKIP_MODEL_DOWNLOAD_ENV, raising=False)
        monkeypatch.setenv("KIROCREW_EMBED_MODEL_PATH", str(tmp_path / "custom.gguf"))
        monkeypatch.setenv("KIROCREW_EMBED_MODEL_URL", "https://mirror.example/m.gguf")
        cfg = PodConfig.load()
        env = rt.build_pod_env(
            cfg, tmp_path / "home", 7999, tmp_path / "co", skip_model_download=True
        )
        assert env[rt.SKIP_MODEL_DOWNLOAD_ENV] == "1"
        assert "KIROCREW_EMBED_MODEL_PATH" not in env
        assert "KIROCREW_EMBED_MODEL_URL" not in env
        # Dropped from the pod's mapping only; the operator's shell keeps its model.
        assert os.environ["KIROCREW_EMBED_MODEL_PATH"] == str(tmp_path / "custom.gguf")

    def test_build_pod_env_keeps_a_custom_model_path_on_a_normal_pod(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without the option a pod is a normal gateway and inherits the override
        # like any other setting; the scrub is tied to the switch, not to the env.
        monkeypatch.setenv("KIROCREW_EMBED_MODEL_PATH", str(tmp_path / "custom.gguf"))
        cfg = PodConfig.load()
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        assert env["KIROCREW_EMBED_MODEL_PATH"] == str(tmp_path / "custom.gguf")

    def test_boot_announces_an_inherited_skip_switch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        # The announce keys on the env the pod RUNS with, not on the env file: a
        # switch the boot environment already carries produces the same
        # embedding-light pod with no EMBEDDINGS key, and the journal must say so.
        env = self._booted_env(tmp_path, monkeypatch, {}, inherit_skip=True)
        assert env[rt.SKIP_MODEL_DOWNLOAD_ENV] == "1"
        out = capsys.readouterr().out
        assert "embeddings off" in out and "inherited from the boot environment" in out

    def test_boot_names_the_env_file_as_the_source_when_it_asked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        self._booted_env(tmp_path, monkeypatch, {"EMBEDDINGS": "0"})
        assert "EMBEDDINGS=0 in the pod env file" in capsys.readouterr().out

    def test_pod_context_carries_the_embedding_setting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `pod exec` / `pod env` reach the env through this seam. Without the
        # setting here, a single `pod exec` against an embedding-light pod starts
        # the download the pod was booted to do without -- and the measurement it
        # was booted for is then taken against a pod that has the model.
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.delenv(rt.SKIP_MODEL_DOWNLOAD_ENV, raising=False)
        cfg = PodConfig.load()
        rt.pin_checkout(cfg, "x", _ready_worktree(tmp_path, "x"))
        _, plain = rt.pod_context(cfg, "x")
        assert rt.SKIP_MODEL_DOWNLOAD_ENV not in plain
        rt.write_env_file(cfg, "x", {"EMBEDDINGS": "0"})
        _, embedless = rt.pod_context(cfg, "x")
        assert embedless[rt.SKIP_MODEL_DOWNLOAD_ENV] == "1"

    def test_up_records_no_embeddings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo", json=False, seed="", ttl="2h", provision=False, no_embeddings=True
            ),
        )
        assert rt.read_env_file(c, "demo").get("EMBEDDINGS") == "0"

    def test_up_leaves_the_key_absent_without_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo", json=False, seed="", ttl="2h", provision=False, no_embeddings=False
            ),
        )
        assert "EMBEDDINGS" not in rt.read_env_file(c, "demo")

    def test_up_audits_no_embeddings(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A pod answering search from the keyword fallback is not the same instance
        # as a normal pod, so a result measured on one must be attributable.
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        seen: list[tuple] = []
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: seen.append(a))
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo", json=False, seed="", ttl="2h", provision=False, no_embeddings=True
            ),
        )
        allowed = [a for a in seen if a[:2] == ("pod.up", "allowed")]
        assert allowed, "pod.up allowed was never audited"
        assert "embeddings=off" in allowed[0][2]

    def _up_allowed_row(self, c: PodConfig, monkeypatch: pytest.MonkeyPatch) -> str:
        seen: list[tuple] = []
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: seen.append(a))
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo", json=False, seed="", ttl="2h", provision=False, no_embeddings=False
            ),
        )
        allowed = [a for a in seen if a[:2] == ("pod.up", "allowed")]
        assert allowed, "pod.up allowed was never audited"
        return allowed[0][2]

    def test_up_audits_a_sticky_no_embeddings_on_a_flagless_re_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The env file is merge-preserving, so EMBEDDINGS=0 from an earlier `up`
        # survives a re-up without the flag and that pod boots embedding-light. The
        # row keys on what the pod boots with, so it says so although this command
        # never asked.
        monkeypatch.delenv(rt.SKIP_MODEL_DOWNLOAD_ENV, raising=False)
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        rt.write_env_file(c, "demo", {"EMBEDDINGS": "0"})
        assert "embeddings=off" in self._up_allowed_row(c, monkeypatch)

    def test_up_audits_an_inherited_skip_switch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No flag and no key, but the control plane's own environment carries the
        # switch every `pod exec` and an inheriting boot will run with.
        monkeypatch.setenv(rt.SKIP_MODEL_DOWNLOAD_ENV, "1")
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        assert "embeddings=off" in self._up_allowed_row(c, monkeypatch)

    def test_up_audit_stays_silent_about_embeddings_on_a_normal_pod(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(rt.SKIP_MODEL_DOWNLOAD_ENV, raising=False)
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        assert "embeddings=off" not in self._up_allowed_row(c, monkeypatch)

    def test_up_notes_every_deferred_flag_on_a_running_pod(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        # One note covers both settings; a per-flag note would be two messages
        # and the repeated `pod down` instruction would read as two restarts.
        c = self._prep_up(tmp_path, monkeypatch, active=True)
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo",
                json=False,
                seed="",
                ttl="2h",
                provision=False,
                approval="yolo",
                crons=True,
                no_embeddings=True,
            ),
        )
        err = capsys.readouterr().err
        assert err.count("pod: note:") == 1
        assert "--approval yolo --crons --no-embeddings" in err

    def test_up_merges_all_boot_settings_into_one_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SEED / APPROVAL / CRONS / EMBEDDINGS share one write; the pinned CHECKOUT
        # survives it.
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo",
                json=False,
                seed="/tmp/fixture",
                ttl="2h",
                provision=False,
                approval="reads",
                crons=True,
                no_embeddings=True,
            ),
        )
        env = rt.read_env_file(c, "demo")
        assert env.get("SEED") == "/tmp/fixture"
        assert env.get("APPROVAL") == "reads"
        assert env.get("CRONS") == "1"
        assert env.get("EMBEDDINGS") == "0"
        # Compare path components, not a "/"-joined suffix: str(Path) uses "\"
        # on Windows, so endswith("wts/demo") fails there.
        checkout = Path(env.get("CHECKOUT", ""))
        assert (checkout.parent.name, checkout.name) == ("wts", "demo")
