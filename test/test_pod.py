"""Tests for the pod runtime (isolated worktree test instances)."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import provision as prov
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import unit as unit_mod
from kiro_crew.pod.config import (
    DEFAULT_BASE_PORT,
    DEFAULT_LIVE_PORT,
    DEFAULT_UNIT_PREFIX,
    PodConfig,
)


@pytest.fixture
def cfg() -> PodConfig:
    return PodConfig.load()


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _ready_worktree(root: Path, name: str, *, venv: bool = True, dist: bool = True) -> Path:
    """Build a flat kirocrew worktree checkout (repo root == worktree dir)."""
    co = root / name
    co.mkdir(parents=True, exist_ok=True)
    if venv:
        (co / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        b = co / ".venv" / "bin" / "kirocrew"
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)
    if dist:
        (co / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True, exist_ok=True)
    return co


class TestConfig:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in list(os.environ):
            if k.startswith("KIROCREW_POD_"):
                monkeypatch.delenv(k, raising=False)
        c = PodConfig.load()
        assert c.base_port == DEFAULT_BASE_PORT
        assert c.live_port == DEFAULT_LIVE_PORT == 5476
        assert c.unit_prefix == DEFAULT_UNIT_PREFIX == "kirocrew-pod"
        # Git is the primary resolver — no fixed root/repo pinned by default.
        assert c.repo_hint is None
        assert c.worktrees_root is None

    def test_env_overrides_build_a_hermetic_plane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_UNIT_PREFIX", "kirocrew-podtest")
        monkeypatch.setenv("KIROCREW_POD_BASE_PORT", "7300")
        monkeypatch.setenv("KIROCREW_POD_ROOT", "/tmp/podtest-root")
        monkeypatch.setenv("KIROCREW_POD_LIVE_PORT", "9999")
        c = PodConfig.load()
        assert c.unit_prefix == "kirocrew-podtest"
        assert c.base_port == 7300
        assert c.pod_root == Path("/tmp/podtest-root")
        assert c.live_port == 9999
        assert rt.pod_unit(c, "foo") == "kirocrew-podtest@foo.service"


class TestPortDerivation:
    def test_matches_posix_cksum_formula(self, cfg: PodConfig) -> None:
        # POSIX cksum != zlib.crc32, so verify against a real cksum invocation.
        for name in ["command-palette", "pod-cli", "x", "a-b_c.d"]:
            cks = int(
                subprocess.run(
                    ["cksum"], input=name, capture_output=True, text=True
                ).stdout.split()[0]
            )
            assert rt.derive_port(cfg, name) == cfg.base_port + (cks % 199) + 1

    def test_in_band(self, cfg: PodConfig) -> None:
        for name in ["a", "bb", "ccc", "command-palette", "zzzzz"]:
            assert cfg.base_port + 1 <= rt.derive_port(cfg, name) <= cfg.base_port + 199

    def test_pinned_port_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        (tmp_path / "pinned.env").write_text("PORT='7999'\nSEED='/x'\n")
        assert rt.derive_port(c, "pinned") == 7999


class TestNameValidation:
    @pytest.mark.parametrize("bad", ["", "../x", "a/b", "x" * 70, "-leading", "a b"])
    def test_rejects(self, bad: str) -> None:
        with pytest.raises(rt.PodError):
            rt.validate_name(bad)

    @pytest.mark.parametrize("good", ["command-palette", "a.b_c-d", "x", "A1"])
    def test_accepts(self, good: str) -> None:
        assert rt.validate_name(good) == good


class TestEnvFileAndPin:
    def test_write_merge_preserves_keys(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        rt.write_env_file(c, "x", {"CHECKOUT": "/a", "PORT": "7999"})
        rt.write_env_file(c, "x", {"SEED": "/s"})  # merge, don't clobber
        assert rt.read_env_file(c, "x") == {"CHECKOUT": "/a", "PORT": "7999", "SEED": "/s"}

    def test_pin_checkout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        rt.pin_checkout(c, "x", Path("/abs/co"))
        assert rt.read_env_file(c, "x")["CHECKOUT"] == "/abs/co"


class TestWorktreeResolution:
    """Git-native: a friendly name → checkout via pinned CHECKOUT, else git, else
    an optional root, else a teaching error."""

    def test_git_worktrees_parses_porcelain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        out = (
            "worktree /repo/main\nHEAD aaa\nbranch refs/heads/main\n\n"
            "worktree /repo/kirocrew-wt-foo\nHEAD bbb\nbranch refs/heads/feat/foo\n\n"
        )
        monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _cp(stdout=out))
        wts = rt._git_worktrees(Path("/repo/main"))
        assert wts["main"] == Path("/repo/main")
        assert wts["kirocrew-wt-foo"] == Path("/repo/kirocrew-wt-foo")
        assert wts["feat/foo"] == Path("/repo/kirocrew-wt-foo")  # branch match

    def test_git_worktrees_empty_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _cp(returncode=128))
        assert rt._git_worktrees(Path("/nope")) == {}

    def test_resolve_prefers_pin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        co = tmp_path / "co"
        co.mkdir()
        rt.pin_checkout(c, "demo", co)
        # Even if git would resolve elsewhere, a valid pin wins (boot path).
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {"demo": Path("/other")})
        assert rt.resolve_checkout(c, "demo", cwd=tmp_path) == co

    def test_resolve_via_git_basename_and_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        monkeypatch.setattr(
            rt, "_git_worktrees", lambda ref: {"foo": Path("/x/foo"), "feat/bar": Path("/x/bar")}
        )
        assert rt.resolve_checkout(c, "foo", cwd=tmp_path) == Path("/x/foo")
        assert rt.resolve_checkout(c, "bar", cwd=tmp_path) == Path("/x/bar")  # feat/<name>

    def test_resolve_root_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_WORKTREES_ROOT", str(tmp_path / "wts"))
        c = PodConfig.load()
        (tmp_path / "wts" / "demo").mkdir(parents=True)
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {})
        assert rt.resolve_checkout(c, "demo", cwd=tmp_path) == tmp_path / "wts" / "demo"

    def test_resolve_raises_teaching(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {})
        with pytest.raises(rt.PodError, match="git worktree add"):
            rt.resolve_checkout(c, "ghost", cwd=tmp_path)

    def test_stale_pin_falls_through_to_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        rt.pin_checkout(c, "demo", tmp_path / "gone")  # pinned dir no longer exists
        (tmp_path / "real").mkdir()
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {"demo": tmp_path / "real"})
        assert rt.resolve_checkout(c, "demo", cwd=tmp_path) == tmp_path / "real"


class TestUnitRendering:
    def test_execstart_reenters_pod_run(self, cfg: PodConfig) -> None:
        assert "pod _run %i" in unit_mod.render_unit(cfg)

    def test_execstoppost_routes_through_cleanup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", "/tmp/podtest-root")
        txt = unit_mod.render_unit(PodConfig.load())
        # Teardown re-enters the Python verb (re-validates %i) — NOT a raw rm -rf.
        assert "pod _cleanup %i" in txt
        assert "-rf" not in txt and "$HOME" not in txt

    def test_env_block_pins_nondefaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", "/tmp/hermetic-pods")
        monkeypatch.setenv("KIROCREW_POD_REPO", "/tmp/some-repo")
        txt = unit_mod.render_unit(PodConfig.load())
        assert "Environment=KIROCREW_POD_ROOT=/tmp/hermetic-pods" in txt
        assert "Environment=KIROCREW_POD_REPO=/tmp/some-repo" in txt

    def test_unit_path_uses_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_UNIT_PREFIX", "kirocrew-podtest")
        assert unit_mod.unit_path(PodConfig.load()).name == "kirocrew-podtest@.service"


class TestBootGuardrails:
    def test_refuses_no_pinned_checkout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        assert rt.boot(PodConfig.load(), "nope") == 3

    def test_refuses_missing_venv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        co = tmp_path / "co"
        co.mkdir()
        rt.pin_checkout(c, "x", co)  # pinned but no .venv → exit 3
        assert rt.boot(c, "x") == 3

    def test_refuses_live_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        co = _ready_worktree(tmp_path, "x")
        rt.pin_checkout(c, "x", co)
        with patch.object(rt, "derive_port", return_value=c.live_port):
            assert rt.boot(c, "x") == 70


class TestSeedSanitization:
    """Deny-by-default: a seeded pod must NEVER boot with tunnel enabled."""

    def _write(self, d: Path, obj: object) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps(obj))
        return d

    def test_forces_tunnel_off_when_enabled(self, tmp_path: Path) -> None:
        seed = self._write(tmp_path / "s", {"tunnel": {"enabled": True, "name": "keep"}})
        out = rt.sanitized_seed_config(seed)
        assert out is not None and out["tunnel"]["enabled"] is False
        assert out["tunnel"]["name"] == "keep"  # other keys preserved

    def test_overwrites_non_dict_tunnel(self, tmp_path: Path) -> None:
        seed = self._write(tmp_path / "s", {"tunnel": True})
        out = rt.sanitized_seed_config(seed)
        assert out is not None and out["tunnel"] == {"enabled": False}

    def test_adds_tunnel_when_absent(self, tmp_path: Path) -> None:
        seed = self._write(tmp_path / "s", {"dashboard": {"port": 5476}})
        out = rt.sanitized_seed_config(seed)
        assert out is not None and out["tunnel"]["enabled"] is False

    def test_bad_json_returns_none(self, tmp_path: Path) -> None:
        seed = tmp_path / "s"
        seed.mkdir()
        (seed / "config.json").write_text("{not valid json")
        assert rt.sanitized_seed_config(seed) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert rt.sanitized_seed_config(tmp_path / "nope") is None

    def test_non_dict_root_returns_none(self, tmp_path: Path) -> None:
        assert rt.sanitized_seed_config(self._write(tmp_path / "s", [1, 2, 3])) is None

    def test_refuses_sensitive_seed_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = self._write(tmp_path / "s", {"tunnel": {"enabled": True}})
        monkeypatch.setattr("kiro_crew.security.is_sensitive_path", lambda p, base_dir=None: True)
        assert rt.sanitized_seed_config(seed) is None


class TestWaitHealthyFailsFast:
    def test_bails_on_failed_unit(self, cfg: PodConfig) -> None:
        with (
            patch.object(rt, "health", return_value=0),
            patch.object(rt, "unit_state", return_value=("failed", 0)),
        ):
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=45) == -1

    def test_bails_on_crash_loop(self, cfg: PodConfig) -> None:
        with (
            patch.object(rt, "health", return_value=0),
            patch.object(rt, "unit_state", return_value=("activating", 1)),
        ):
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=45) == -1

    def test_returns_code_when_healthy(self, cfg: PodConfig) -> None:
        with (
            patch.object(rt, "health", return_value=403),
            patch.object(rt, "unit_state", return_value=("active", 0)),
        ):
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == 403


class TestProvision:
    def test_detectors(self, tmp_path: Path) -> None:
        full = _ready_worktree(tmp_path, "full", venv=True, dist=True)
        assert prov.has_venv(full) and prov.has_dist(full)
        novenv = _ready_worktree(tmp_path, "novenv", venv=False, dist=True)
        assert not prov.has_venv(novenv) and prov.has_dist(novenv)

    def test_provision_venv_only_skips_build(self, tmp_path: Path) -> None:
        co = _ready_worktree(tmp_path, "be-only", venv=True, dist=False)
        with patch.object(prov, "build_dist", side_effect=AssertionError("must not build")):
            assert prov.provision(co, build=False) is True


class TestProvisionBuildPaths:
    def test_ensure_venv_builds_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": "/usr/bin/python3.12")

        def fake_run(cmd: list[str], cwd: Path) -> int:
            if cmd[1:3] == ["-m", "venv"]:
                (co / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
                b = co / ".venv" / "bin" / "kirocrew"
                b.write_text("#!/bin/sh\n")
                b.chmod(0o755)
            return 0

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.ensure_venv(co) is True

    def test_ensure_venv_no_python(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": None)
        assert prov.ensure_venv(co) is False

    def test_build_dist_npm_then_stages(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        co = tmp_path / "wt"
        (co / "website").mkdir(parents=True)

        def fake_run(cmd: list[str], cwd: Path) -> int:
            (co / "website" / "dist").mkdir(parents=True, exist_ok=True)
            (co / "website" / "dist" / "index.html").write_text("<html>")
            return 0

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.build_dist(co) is True
        # website/dist staged into the served static/dist.
        assert (co / "src" / "kiro_crew" / "static" / "dist" / "index.html").is_file()

    def test_build_dist_no_website_dir(self, tmp_path: Path) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        assert prov.build_dist(co) is False

    def test_build_dist_short_circuits_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = _ready_worktree(tmp_path, "wt", venv=False, dist=True)
        monkeypatch.setattr(prov, "_run", lambda cmd, cwd: 99)  # must not be called
        assert prov.build_dist(co) is True

    def test_provision_full_chain(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "ensure_venv", lambda c: True)
        monkeypatch.setattr(prov, "build_dist", lambda c: True)
        assert prov.provision(co, build=True) is True

    def test_find_python_via_which(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(prov.Path, "exists", lambda self: False)
        monkeypatch.setattr(prov.shutil, "which", lambda exe: "/opt/python3.12")
        assert prov._find_python() == "/opt/python3.12"


class TestProvisionDependencyInstall:
    """Dependency-gap fixes: npm deps before dist build (#229), dev extras in
    the venv with graceful fallback for old pip (#230)."""

    def _venv_seeding_run(self, co: Path, calls: list[list[str]], group_fails: bool = False):
        """Return a fake _run that records calls and materializes the venv bin on
        `python -m venv`, so has_venv() passes. Optionally fail `--group` cmds."""

        def fake_run(cmd: list[str], cwd: Path) -> int:
            calls.append(cmd)
            if cmd[1:3] == ["-m", "venv"]:
                b = co / ".venv" / "bin"
                b.mkdir(parents=True, exist_ok=True)
                (b / "kirocrew").write_text("#!/bin/sh\n")
                (b / "kirocrew").chmod(0o755)
            if group_fails and "--group" in cmd:
                return 1
            return 0

        return fake_run

    # ---- #229: website npm deps ----

    def test_npm_ci_when_node_modules_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        website = tmp_path / "wt" / "website"
        website.mkdir(parents=True)
        calls: list[list[str]] = []
        monkeypatch.setattr(prov, "_run", lambda cmd, cwd: calls.append(cmd) or 0)
        assert prov.ensure_node_modules(website) is True
        assert ["npm", "ci"] in calls
        # ci succeeded → no fallback (non-mutating install never runs)
        assert ["npm", "install", "--no-package-lock"] not in calls

    def test_node_modules_skipped_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        website = tmp_path / "wt" / "website"
        (website / "node_modules" / ".bin").mkdir(parents=True)
        (website / "node_modules" / ".bin" / "tsc").write_text("#!/bin/sh\n")

        def boom(cmd: list[str], cwd: Path) -> int:
            raise AssertionError(f"must not run npm when node_modules present: {cmd}")

        monkeypatch.setattr(prov, "_run", boom)
        assert prov.ensure_node_modules(website) is True

    def test_npm_install_fallback_when_ci_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        website = tmp_path / "wt" / "website"
        website.mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: Path) -> int:
            calls.append(cmd)
            return 1 if cmd == ["npm", "ci"] else 0  # ci fails, install succeeds

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.ensure_node_modules(website) is True
        # Fallback must be NON-MUTATING: --no-package-lock so the tracked
        # website/package-lock.json is never rewritten (would dirty the worktree).
        assert ["npm", "ci"] in calls
        assert ["npm", "install", "--no-package-lock"] in calls
        assert ["npm", "install"] not in calls  # plain (mutating) install never runs

    def test_build_dist_installs_node_modules_before_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = tmp_path / "wt"
        website = co / "website"
        website.mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: Path) -> int:
            calls.append(cmd)
            if cmd == ["npm", "run", "build"]:
                (website / "dist").mkdir(parents=True, exist_ok=True)
                (website / "dist" / "index.html").write_text("<html>")
            return 0

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.build_dist(co) is True
        assert calls.index(["npm", "ci"]) < calls.index(["npm", "run", "build"])

    # ---- #230: venv dev extras ----

    def test_pip_group_dev_attempted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": "/usr/bin/python3.12")
        calls: list[list[str]] = []
        monkeypatch.setattr(prov, "_run", self._venv_seeding_run(co, calls))
        assert prov.ensure_venv(co) is True
        assert any(c[-2:] == ["--group", "dev"] for c in calls)

    def test_pip_group_dev_fallback_on_old_pip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": "/usr/bin/python3.12")
        calls: list[list[str]] = []
        monkeypatch.setattr(prov, "_run", self._venv_seeding_run(co, calls, group_fails=True))
        assert prov.ensure_venv(co) is True
        assert any("--group" in c for c in calls)  # attempted --group dev
        pip = str(co / ".venv" / "bin" / "pip")
        assert [pip, "install", "--editable", str(co)] in calls  # then fell back


class TestPodEnv:
    def test_scrubs_slack_and_nonaws_tokens_keeps_aws(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-live-bot")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-live")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "sts-temp")
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        assert "SLACK_BOT_TOKEN" not in env and "SLACK_APP_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env  # non-AWS *_TOKEN scrubbed
        # AWS_* kept (agent turns need it); AWS_SESSION_TOKEN must survive.
        assert env.get("AWS_REGION") == "us-west-2"
        assert env.get("AWS_SESSION_TOKEN") == "sts-temp"
        assert env["KIROCREW_PORT"] == "7999"
        assert env["KIROCREW_HOME"].endswith("home")
        assert env["KIROCREW_PROJECT_DIR"].endswith("co")


class TestPodConfigWrite:
    def test_blank_pod_writes_tunnel_off_config(self, tmp_path: Path) -> None:
        home = tmp_path / "pod-home"
        rt.write_pod_config(home, seed="")
        data = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert data["tunnel"]["enabled"] is False

    def test_config_and_home_are_owner_only(self, tmp_path: Path) -> None:
        home = tmp_path / "pod-home"
        rt.write_pod_config(home, seed="")
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        assert stat.S_IMODE((home / "config.json").stat().st_mode) == 0o600

    def test_seed_config_sanitized_and_locked_down(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed"
        seed.mkdir()
        (seed / "config.json").write_text(
            json.dumps({"tunnel": {"enabled": True}, "provider_token": "secret"})
        )
        home = tmp_path / "pod-home"
        rt.write_pod_config(home, seed=str(seed))
        data = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert data["tunnel"]["enabled"] is False  # sanitized
        assert stat.S_IMODE((home / "config.json").stat().st_mode) == 0o600


class TestCleanupHome:
    def test_removes_pod_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        victim = c.home_dir("demo")
        victim.mkdir(parents=True)
        (victim / "marker").write_text("x")
        assert rt.cleanup_home(c, "demo") == 0
        assert not victim.exists()
        assert c.pod_root.exists()  # parent untouched

    def test_refuses_dotdot_escape(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pods = tmp_path / "pods"
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pods))
        c = PodConfig.load()
        pods.mkdir(parents=True)
        sentinel = tmp_path / "DO_NOT_DELETE"  # lives in pod_root's PARENT
        sentinel.write_text("precious")
        assert rt.cleanup_home(c, "..") != 0
        assert sentinel.exists() and pods.exists()


class TestRuntimeHelpers:
    def test_is_active(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        assert rt.is_active(cfg, "x") is True
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=3))
        assert rt.is_active(cfg, "x") is False

    def test_active_names_parses_units(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        out = (
            "kirocrew-pod@alpha.service    loaded active running x\n"
            "kirocrew-pod@beta-two.service loaded active running y\n"
            "unrelated.service             loaded active running z\n"
        )
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(stdout=out))
        assert rt.active_names(cfg) == {"alpha", "beta-two"}

    def test_unit_state(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            rt, "systemctl", lambda *a, **k: _cp(stdout="ActiveState=failed\nNRestarts=2\n")
        )
        assert rt.unit_state(cfg, "x") == ("failed", 2)

    def test_unit_state_defaults(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(stdout="garbage\n"))
        assert rt.unit_state(cfg, "x") == ("unknown", 0)

    def test_health_codes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error

        class _Resp:
            status = 200

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

        monkeypatch.setattr(rt.urllib.request, "urlopen", lambda *a, **k: _Resp())
        assert rt.health(7999) == 200

        def _raise_http(*a: object, **k: object) -> None:
            raise urllib.error.HTTPError("u", 403, "f", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr(rt.urllib.request, "urlopen", _raise_http)
        assert rt.health(7999) == 403

        def _raise_url(*a: object, **k: object) -> None:
            raise urllib.error.URLError("down")

        monkeypatch.setattr(rt.urllib.request, "urlopen", _raise_url)
        assert rt.health(7999) == 0

    def test_mint_token_reads_secret_and_posts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / ".local_secret").write_text("s3cret")

        class _Resp:
            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"token":"tok-xyz"}'

        monkeypatch.setattr(rt.urllib.request, "urlopen", lambda *a, **k: _Resp())
        assert rt.mint_token(c, "demo", "1h") == "tok-xyz"

    def test_mint_token_no_secret_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        with pytest.raises(rt.PodError):
            rt.mint_token(PodConfig.load(), "ghost", "1h")


class TestAuditEvents:
    def test_down_emits_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[tuple] = []
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: recorded.append((a, k)))
        monkeypatch.setattr(rt, "is_active", lambda cfg, name: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        pod_cli._down(PodConfig.load(), argparse.Namespace(name="demo"))
        assert any("pod.down" in str(r) for r in recorded)

    def test_down_dies_when_stop_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[tuple] = []
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: recorded.append((a, k)))
        monkeypatch.setattr(rt, "is_active", lambda cfg, name: True)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=1, stderr="bus error"))
        with pytest.raises(SystemExit):
            pod_cli._down(PodConfig.load(), argparse.Namespace(name="demo"))
        assert any("failure" in str(r) for r in recorded)

    def test_token_emits_audit_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[tuple] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, *a, **k: recorded.append((op, outcome))
        )
        monkeypatch.setattr(rt, "mint_token", lambda cfg, name, ttl: "tok-abc")
        pod_cli._token(PodConfig.load(), argparse.Namespace(name="demo", ttl="2h"))
        assert ("pod.token", "allowed") in recorded

    def test_audit_failure_is_logged_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        def _boom(*a: object, **k: object) -> None:
            raise RuntimeError("sel down")

        monkeypatch.setattr(pod_cli, "sel", lambda: type("S", (), {"log_api_access": _boom})())
        with caplog.at_level(logging.WARNING):
            pod_cli._audit("pod.up", "allowed", "name=x")  # must NOT raise
        assert any("SEL audit failed for pod.up" in r.message for r in caplog.records)


class TestCliVerbs:
    def test_ls_empty(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setattr(rt, "active_names", lambda c: set())
        pod_cli._ls(cfg, argparse.Namespace(json=False))
        assert "no pods running" in capsys.readouterr().out

    def test_ls_table_and_json(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setattr(rt, "active_names", lambda c: {"alpha"})
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7811)
        monkeypatch.setattr(rt, "health", lambda p: 403)
        pod_cli._ls(cfg, argparse.Namespace(json=False))
        assert "alpha" in capsys.readouterr().out
        pod_cli._ls(cfg, argparse.Namespace(json=True))
        out = capsys.readouterr().out
        assert '"port": 7811' in out and '"health": 403' in out

    def test_status(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda c, n: True)
        monkeypatch.setattr(rt, "health", lambda p: 403)
        pod_cli._status(cfg, argparse.Namespace(name="alpha", json=True))
        out = capsys.readouterr().out
        assert '"status": "up"' in out and '"health": 403' in out

    def test_url(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7811)
        pod_cli._url(cfg, argparse.Namespace(name="alpha"))
        assert "http://127.0.0.1:7811" in capsys.readouterr().out

    def test_install_writes_unit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        recorded: list[tuple] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, *a, **k: recorded.append((op, outcome))
        )
        c = PodConfig.load()
        pod_cli._install(c, argparse.Namespace())
        dst = tmp_path / ".config" / "systemd" / "user" / f"{c.unit_prefix}@.service"
        assert dst.exists() and "pod _run %i" in dst.read_text(encoding="utf-8")
        assert "daemon-reload OK" in capsys.readouterr().out
        assert ("pod.install", "allowed") in recorded

    def test_install_dies_on_reload_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=1))
        recorded: list[tuple] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, *a, **k: recorded.append((op, outcome))
        )
        with pytest.raises(SystemExit):
            pod_cli._install(PodConfig.load(), argparse.Namespace())
        assert ("pod.install", "failure") in recorded

    def test_dispatch_unknown_verb_exits(self) -> None:
        with pytest.raises(SystemExit):
            pod_cli.dispatch(argparse.Namespace(pod_action=None))


class TestUpVerb:
    """Drive the big _up body end-to-end with the host boundary mocked."""

    def _prep(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, ready: bool = True,
              dist: bool = True) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_WORKTREES_ROOT", str(tmp_path / "wts"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        # Force git resolution to miss so the root fallback resolves deterministically.
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {})
        if ready:
            _ready_worktree(tmp_path / "wts", "demo", venv=True, dist=dist)
        return PodConfig.load()

    def test_up_happy_path_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )
        out = capsys.readouterr().out
        assert '"port": 7811' in out and '"token": "tok-9"' in out
        # _up must pin the resolved checkout for the systemd boot.
        assert rt.read_env_file(c, "demo").get("CHECKOUT", "").endswith("wts/demo")

    def test_up_refuses_live_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: c.live_port)
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        with pytest.raises(SystemExit):
            pod_cli._up(
                c, argparse.Namespace(name="demo", json=False, seed="", ttl="2h", provision=False)
            )

    def test_up_missing_worktree_teaches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep(tmp_path, monkeypatch, ready=False)
        with pytest.raises(SystemExit):
            pod_cli._up(
                c, argparse.Namespace(name="ghost", json=False, seed="", ttl="2h", provision=False)
            )
        assert "git worktree add" in capsys.readouterr().err  # git-native resolution

    def test_up_no_dist_points_at_provision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep(tmp_path, monkeypatch, dist=False)  # venv but no dist
        with pytest.raises(SystemExit):
            pod_cli._up(
                c, argparse.Namespace(name="demo", json=False, seed="", ttl="2h", provision=False)
            )
        assert "--provision" in capsys.readouterr().err

    def test_up_broken_gateway_fails_fast(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: -1)
        monkeypatch.setattr(rt, "recent_journal", lambda cfg, n, ln=30: "ImportError: boom")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        with pytest.raises(SystemExit):
            pod_cli._up(
                c, argparse.Namespace(name="demo", json=False, seed="", ttl="2h", provision=False)
            )
        err = capsys.readouterr().err
        assert "worktree build, not pod" in err and "ImportError: boom" in err


class TestReviewRound1Fixes:
    """Round-1 review fixes: deny-by-default channel disable in the sanitized seed,
    WeCom env-cred scrub, ttl query quoting, matched-quote env parsing."""

    def test_seed_forces_all_channels_off(self, tmp_path: Path) -> None:
        seed = tmp_path / "s"
        seed.mkdir()
        (seed / "config.json").write_text(
            json.dumps(
                {
                    "tunnel": {"enabled": True},
                    "telegram": {"enabled": True, "bot_token": "tg-live"},
                    "wechat": {"enabled": True},
                }
            )
        )
        out = rt.sanitized_seed_config(seed)
        assert out is not None
        assert out["tunnel"]["enabled"] is False
        assert out["telegram"]["enabled"] is False
        assert out["wechat"]["enabled"] is False
        assert out["telegram"]["bot_token"] == "tg-live"  # preserved, just disabled

    def test_build_pod_env_scrubs_wecom_and_telegram(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WECOM_BOT_ID", "bot-live")
        monkeypatch.setenv("WECOM_SECRET", "sec-live")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-live")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "sts-temp")
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        assert "WECOM_BOT_ID" not in env
        assert "WECOM_SECRET" not in env
        assert "TELEGRAM_BOT_TOKEN" not in env  # non-AWS *_TOKEN
        assert env.get("AWS_SESSION_TOKEN") == "sts-temp"  # AWS kept

    def test_read_env_file_matched_quote_pair_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        # An inner apostrophe survives; only the one surrounding pair is stripped.
        c.env_file("x").write_text("CHECKOUT='/a/o'brien'\n")
        assert rt.read_env_file(c, "x")["CHECKOUT"] == "/a/o'brien"

    def test_mint_token_quotes_ttl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / ".local_secret").write_text("s")
        captured: dict[str, str] = {}

        class _Resp:
            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"token":"t"}'

        def _urlopen(req: object, timeout: int = 5) -> "_Resp":
            captured["url"] = req.full_url  # type: ignore[attr-defined]
            return _Resp()

        monkeypatch.setattr(rt.urllib.request, "urlopen", _urlopen)
        rt.mint_token(c, "demo", "1 h")
        assert "ttl=1%20h" in captured["url"]


class TestReviewRound2Fix:
    """Round-2 review fix: per-pod env values must be single-line (fail-closed)."""

    def test_write_env_file_rejects_newline_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        with pytest.raises(rt.PodError):
            rt.write_env_file(c, "x", {"SEED": "/a/\nevil"})
        # a normal single-line value still round-trips
        rt.write_env_file(c, "y", {"CHECKOUT": "/a/b"})
        assert rt.read_env_file(c, "y")["CHECKOUT"] == "/a/b"


class TestUnitExecSelfHeal:
    """The unit bakes an absolute kirocrew path; a pruned worktree leaves it
    dangling and every start fails EXEC. unit_exec_ok detects that."""

    def _cfg_with_unit(self, tmp_path, monkeypatch, exec_line):
        from kiro_crew.pod import unit as unit_mod
        from kiro_crew.pod.config import PodConfig

        monkeypatch.setattr(
            unit_mod, "unit_path", lambda cfg: tmp_path / "pod@.service"
        )
        (tmp_path / "pod@.service").write_text(
            f"[Service]\n{exec_line}\nExecStopPost=/bin/true pod _cleanup %i\n"
        )
        return PodConfig.load()

    def test_dangling_binary_detected(self, tmp_path, monkeypatch):
        from kiro_crew.pod import unit as unit_mod

        cfg = self._cfg_with_unit(
            tmp_path, monkeypatch,
            f"ExecStart={tmp_path}/gone/.venv/bin/kirocrew pod _run %i",
        )
        assert unit_mod.unit_exec_ok(cfg) is False

    def test_valid_binary_passes(self, tmp_path, monkeypatch):
        from kiro_crew.pod import unit as unit_mod

        exe = tmp_path / "kirocrew"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        cfg = self._cfg_with_unit(
            tmp_path, monkeypatch, f"ExecStart={exe} pod _run %i"
        )
        assert unit_mod.unit_exec_ok(cfg) is True

    def test_missing_unit_file_detected(self, tmp_path, monkeypatch):
        from kiro_crew.pod import unit as unit_mod
        from kiro_crew.pod.config import PodConfig

        monkeypatch.setattr(
            unit_mod, "unit_path", lambda cfg: tmp_path / "absent@.service"
        )
        assert unit_mod.unit_exec_ok(PodConfig.load()) is False

    def test_module_invocation_form_passes(self, tmp_path, monkeypatch):
        from kiro_crew.pod import unit as unit_mod

        cfg = self._cfg_with_unit(
            tmp_path, monkeypatch,
            "ExecStart=python3 -m kiro_crew pod _run %i",
        )
        assert unit_mod.unit_exec_ok(cfg) is True
