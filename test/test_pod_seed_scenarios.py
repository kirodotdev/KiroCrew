"""Named pod seed scenarios, fixture validation, and boot integration."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from conftest import requires_symlinks
from kiro_crew import seed as seed_mod
from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig
from kiro_crew.testing.fixtures import seeded_home

# Credential shapes that must never ship inside a fixture. The fixtures land in
# the wheel and the sdist, so a placeholder that merely LOOKS like a secret is
# also a defect: it trains readers to ignore the real thing and trips every
# downstream secret scanner on an install that has done nothing wrong.
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("pem private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("slack bot token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}")),
)

# Every text extension a fixture uses. Read as text with errors replaced so a
# stray binary cannot fail the scan by decoding badly -- it would still be
# scanned, and an unexpected binary is caught by the size assertion instead.
_ALL_FIXTURES = seed_mod.available_fixtures()

# A fixture ships as package data, so its cost is paid by every install. The cap
# is generous next to the largest shipped fixture (`rich`, ~21 KB) and exists to
# catch a category error -- a database, a screenshot, a vendored tree -- rather
# than to police a few hundred bytes.
_MAX_FIXTURE_BYTES = 64 * 1024


class TestFixtureRegistry:
    def test_registry_is_non_empty_and_sorted(self) -> None:
        assert _ALL_FIXTURES, "no fixtures discovered -- packaging or path regression"
        assert _ALL_FIXTURES == sorted(_ALL_FIXTURES)
        for expected in ("empty", "minimal", "rich"):
            assert expected in _ALL_FIXTURES


@pytest.mark.parametrize("fixture_name", _ALL_FIXTURES)
class TestEveryShippedFixture:
    """One instance per fixture, so a failure names the offending fixture."""

    def test_manifest_parses_with_a_description(self, fixture_name: str) -> None:
        root = Path(str(seed_mod._fixtures_root()))
        manifest = root / fixture_name / seed_mod.FIXTURE_MANIFEST
        assert manifest.is_file(), f"{fixture_name} has no {seed_mod.FIXTURE_MANIFEST}"
        data = yaml.safe_load(manifest.read_text())
        assert isinstance(data, dict), f"{fixture_name} manifest is not a mapping"
        assert data.get("schema-version"), f"{fixture_name} declares no schema-version"
        assert str(data.get("description") or "").strip(), f"{fixture_name} has no description"

    def test_seeds_into_a_fresh_home(self, fixture_name: str) -> None:
        # The real contract: a fixture is only useful if `seed` can lay it down.
        with seeded_home(fixture_name) as home:
            assert (home / seed_mod.FIXTURE_MANIFEST).is_file()
            # Any JSON a fixture ships must parse -- a fixture with a broken
            # config.json boots a pod that silently falls back to defaults.
            for path in home.rglob("*.json"):
                json.loads(path.read_text())
            for path in home.rglob("*.jsonl"):
                for line in path.read_text().splitlines():
                    if line.strip():
                        json.loads(line)

    def test_ships_no_credential_shaped_text(self, fixture_name: str) -> None:
        root = Path(str(seed_mod._fixtures_root())) / fixture_name
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            text = path.read_text(encoding="utf-8", errors="replace")
            for label, pattern in _CREDENTIAL_PATTERNS:
                assert not pattern.search(text), f"{path} looks like it carries a {label}"

    def test_stays_small_enough_to_ship(self, fixture_name: str) -> None:
        root = Path(str(seed_mod._fixtures_root())) / fixture_name
        total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        assert total <= _MAX_FIXTURE_BYTES, f"{fixture_name} is {total} bytes of package data"


class TestScenarioClassification:
    @pytest.mark.parametrize(
        "value",
        ["rich", "minimal", "a", "x.y_z-1", "Rich", "NOT_A_FIXTURE"],
    )
    def test_bare_tokens_use_named_seed_resolution(self, value: str) -> None:
        assert rt.is_scenario_ref(value)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "~/.kiro/crew",
            "/abs/path",
            "./rel",
            "../up",
            "dir/sub",
            "dir\\sub",
        ],
    )
    def test_path_shaped_values_are_directories(self, value: str) -> None:
        assert not rt.is_scenario_ref(value)

    def test_a_bare_token_no_fixture_answers_to_refuses_by_name(self) -> None:
        """The refusal must come from the resolver, not from a silent blank boot.

        Classifying an unknown bare token as a directory sends it down the
        seed-a-directory path, where a non-existent relative name copies nothing
        and the pod comes up empty and healthy — which reads as the feature under
        test being broken.
        """
        with pytest.raises(rt.PodError) as excinfo:
            rt.resolve_seed_scenario("Rich")
        assert "unknown seed scenario 'Rich'" in str(excinfo.value)

    def test_resolve_accepts_a_shipped_scenario(self) -> None:
        assert rt.resolve_seed_scenario("rich") == "rich"

    def test_resolve_lists_available_names_on_a_typo(self) -> None:
        with pytest.raises(rt.PodError) as excinfo:
            rt.resolve_seed_scenario("richh")
        msg = str(excinfo.value)
        assert "unknown seed scenario 'richh'" in msg
        assert "rich" in msg and "minimal" in msg
        # Must also name the escape hatch, since a bare relative directory name
        # is exactly what lands here.
        assert "--seed ./richh" in msg


@pytest.mark.skipif(not rt.IS_POSIX, reason="pods require POSIX descriptor traversal")
class TestSeedHomeFromScenario:
    def test_populates_an_absent_home(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        assert rt.seed_home_from_scenario(cfg, "wt", "minimal") is True
        assert (cfg.home_dir("wt") / "crons.json").is_file()

    def test_leaves_a_completed_matching_home_alone(self, tmp_path: Path, monkeypatch) -> None:
        """A restart of the same scenario must preserve state created after seed."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        home = cfg.home_dir("wt")
        assert rt.seed_home_from_scenario(cfg, "wt", "minimal") is True
        (home / "sessions" / "live.jsonl").write_text("{}\n")

        assert rt.seed_home_from_scenario(cfg, "wt", "minimal") is False
        assert (home / "sessions" / "live.jsonl").is_file()
        assert (home / seed_mod.FIXTURE_MANIFEST).is_file()

    def test_a_non_directory_home_refuses_instead_of_crashing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A stale FILE at the home path answers exists() but not iterdir().

        Left unchecked it raises NotADirectoryError straight out of boot(), which
        the supervisor reads as a crash and restart-loops the pod every few
        seconds — with the real cause buried in a traceback.
        """
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        home = cfg.home_dir("wt")
        home.parent.mkdir(parents=True, exist_ok=True)
        home.write_text("stale file where a directory belongs")
        with pytest.raises(rt.PodError, match="not a plain directory"):
            rt.seed_home_from_scenario(cfg, "wt", "minimal")

    def test_pod_root_creation_errors_refuse_in_the_pod_vocabulary(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()

        def _refuse(*args, **kwargs):
            raise PermissionError("root denied")

        monkeypatch.setattr(rt.pinned_fs, "create_and_open_dir_pinned", _refuse)
        with pytest.raises(rt.PodError, match="could not inspect or prepare pod home"):
            rt.seed_home_from_scenario(cfg, "wt", "minimal")

    @requires_symlinks
    def test_raced_home_symlink_cannot_redirect_fixture_writes(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        home = cfg.home_dir("wt")
        held = tmp_path / "held-home"
        outside = tmp_path / "outside"
        outside.mkdir()

        def _swap_then_write(scenario: str, dst_fd: int) -> None:
            home.rename(held)
            home.symlink_to(outside, target_is_directory=True)
            fd = os.open("proof.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dst_fd)
            try:
                os.write(fd, b"pinned")
            finally:
                os.close(fd)

        monkeypatch.setattr(seed_mod, "copy_fixture_into_dir_fd", _swap_then_write)
        with pytest.raises(rt.PodError, match="changed while it was being seeded"):
            rt.seed_home_from_scenario(cfg, "wt", "minimal")
        assert (held / "proof.txt").read_text() == "pinned"
        assert not (outside / "proof.txt").exists()

    def test_restores_the_callers_home_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "callers-home"))
        rt.seed_home_from_scenario(PodConfig.load(), "wt", "empty")
        assert os.environ["KIROCREW_HOME"] == str(tmp_path / "callers-home")

    def test_unknown_scenario_raises_in_the_pod_vocabulary(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        with pytest.raises(rt.PodError):
            rt.seed_home_from_scenario(PodConfig.load(), "wt", "no-such-fixture")

    def test_unknown_scenario_still_refuses_over_a_populated_home(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Validation comes FIRST, or the emptiness check swallows the typo.

        A populated home returns False without copying anything, so a name no
        fixture answers to was reported as "not re-applied" and the pod booted —
        the same silent success the loud refusal exists to prevent, only harder to
        see, because the operator is told the home was already fine.
        """
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        home = cfg.home_dir("wt")
        home.mkdir(parents=True)
        (home / "config.json").write_text("{}")
        with pytest.raises(rt.PodError, match="unknown seed scenario"):
            rt.seed_home_from_scenario(cfg, "wt", "no-such-fixture")


@pytest.mark.skipif(not rt.IS_POSIX, reason="pods require POSIX descriptor traversal")
class TestSeededScenarioInHome:
    """The sentinel ``pod up`` judges a seed against. Every fixture ships a
    ``fixture.yaml`` at its root and ``copytree`` lands it in the pod's home, so
    the manifest is the only on-disk evidence of WHICH scenario a home holds —
    the env file records what was asked for, which is the thing under test."""

    def test_names_the_scenario_a_seeded_home_holds(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        rt.seed_home_from_scenario(cfg, "wt", "minimal")
        assert rt.seeded_scenario_in_home(cfg, "wt") == "minimal"

    def test_marker_read_does_not_require_pyyaml(self, tmp_path: Path, monkeypatch) -> None:
        """PyYAML is a dev/test dependency, not a runtime dependency.

        The marker format is owned by these fixtures and needs only one scalar;
        a lazy import merely hid the undeclared dependency until an installed
        build tried to verify a seed and reported an empty marker.
        """
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        rt.seed_home_from_scenario(cfg, "wt", "minimal")
        monkeypatch.setitem(sys.modules, "yaml", None)
        assert rt.seeded_scenario_in_home(cfg, "wt") == "minimal"

    def test_a_blank_home_names_nothing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        cfg.home_dir("wt").mkdir(parents=True)
        assert rt.seeded_scenario_in_home(cfg, "wt") is None

    def test_an_unreadable_manifest_still_reports_seeded(self, tmp_path: Path, monkeypatch) -> None:
        # A manifest that is present but unparseable still proves the home was
        # seeded from SOMETHING, which is the distinction the caller needs;
        # returning None would have it report a seeded home as blank.
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        home = cfg.home_dir("wt")
        home.mkdir(parents=True)
        (home / seed_mod.FIXTURE_MANIFEST).write_text("[not, a, mapping]")
        assert rt.seeded_scenario_in_home(cfg, "wt") == ""

    def test_a_manifest_directory_reports_no_marker(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        home = cfg.home_dir("wt")
        (home / seed_mod.FIXTURE_MANIFEST).mkdir(parents=True)
        assert rt.seeded_scenario_in_home(cfg, "wt") is None

    def test_a_manifest_fifo_is_opened_nonblocking_and_reports_no_marker(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        os.mkfifo(home / seed_mod.FIXTURE_MANIFEST)
        home_fd = os.open(home, rt.pinned_fs.dir_flags())
        real_open = rt.os.open

        def _open(name, flags, mode=0o777, *, dir_fd=None):
            if name == seed_mod.FIXTURE_MANIFEST:
                assert flags & os.O_NONBLOCK
            return real_open(name, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(rt.os, "open", _open)
        try:
            assert rt._seeded_scenario_from_fd(home_fd) is None
        finally:
            os.close(home_fd)

    @pytest.mark.parametrize("shape", ["invalid-utf8", "directory", "fifo"])
    def test_malformed_seeded_config_refuses_in_pod_vocabulary(
        self, tmp_path: Path, monkeypatch, shape: str
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        config = home / "config.json"
        if shape == "invalid-utf8":
            config.write_bytes(b'{"agent": "\xff"}')
        elif shape == "directory":
            config.mkdir()
        else:
            os.mkfifo(config)
        home_fd = os.open(home, rt.pinned_fs.dir_flags())
        if shape == "fifo":
            real_open = rt.os.open

            def _open(name, flags, mode=0o777, *, dir_fd=None):
                if name == "config.json":
                    assert flags & os.O_NONBLOCK
                return real_open(name, flags, mode, dir_fd=dir_fd)

            monkeypatch.setattr(rt.os, "open", _open)
        try:
            with pytest.raises(rt.PodError, match="seeded config.json"):
                rt._prepare_seeded_home_fd(home_fd)
        finally:
            os.close(home_fd)


@pytest.mark.skipif(not rt.IS_POSIX, reason="pods require POSIX descriptor traversal")
class TestBootAppliesTheScenario:
    """``boot`` is where the seed has to land: after the HOME exists, before the
    gateway is exec'd. These drive the real function with the exec stubbed, so the
    ordering is observed rather than assumed."""

    @pytest.fixture
    def booted(self, tmp_path: Path, monkeypatch):
        """Run ``boot`` for a pod with SEED=<value>; return (rc, home, argv)."""
        monkeypatch.setattr(rt, "IS_MACOS", False)
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "envs"))
        checkout = tmp_path / "co"
        binary = rt.prov.venv_bin(checkout)
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        (checkout / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True)
        cli_src = checkout / "src" / "kiro_crew" / "cli.py"
        cli_src.write_text('gw.add_argument("--no-crons")\ngw.add_argument("--no-tunnel")\n')
        captured: dict[str, object] = {}

        def _fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
            captured["argv"] = argv
            captured["env"] = env

        monkeypatch.setattr(rt.os, "execve", _fake_execve)

        def _run(seed_value: str):
            cfg = PodConfig.load()
            rt.write_env_file(cfg, "wt", {"CHECKOUT": str(checkout), "SEED": seed_value})
            rc = rt.boot(cfg, "wt")
            return rc, cfg.home_dir("wt"), captured

        return _run

    def test_a_scenario_populates_the_home_before_the_exec(self, booted, capsys) -> None:
        rc, home, captured = booted("minimal")
        assert rc == 0
        # The fixture's own content is on disk...
        crons = json.loads((home / "crons.json").read_text())
        assert len(crons["jobs"]) == 2
        # ...and the gateway was exec'd afterwards, pointed at that home. The name
        # is compared against the resolver rather than a literal: a Windows venv's
        # entry point is `kirocrew.exe`, so a bare `endswith("kirocrew")` asserts
        # the POSIX layout on every platform.
        assert captured["argv"][0].endswith(rt.prov.venv_bin(Path(".")).name)
        assert captured["env"]["KIROCREW_HOME"] == str(home)
        assert "seeded home from scenario 'minimal'" in capsys.readouterr().out

    def test_the_seeded_config_is_sanitized(self, booted) -> None:
        _, home, _ = booted("minimal")
        data = json.loads((home / "config.json").read_text())
        for section in rt.SEED_DISABLED_SECTIONS:
            assert data[section]["enabled"] is False
        assert data["agent"]["sandbox"] == "auto"
        assert data["agent"]["sandbox_allow_unsandboxed_exec"] is False
        assert data["agent"]["sandbox_allow_no_isolation"] is False

    def test_setup_failure_never_publishes_completion_marker(self, booted, monkeypatch) -> None:
        def _refuse(*args, **kwargs) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(rt, "atomic_write_at", _refuse)
        rc, home, captured = booted("minimal")
        assert rc == 3
        assert "argv" not in captured
        assert not (home / seed_mod.FIXTURE_MANIFEST).exists()

    def test_a_restart_does_not_re_seed(self, booted, capsys) -> None:
        _, home, _ = booted("minimal")
        (home / "sessions" / "dashboard_starter.jsonl").unlink()
        capsys.readouterr()
        booted("minimal")  # the Restart=on-failure re-exec
        assert not (home / "sessions" / "dashboard_starter.jsonl").exists()
        assert "not re-applied" in capsys.readouterr().out

    def test_an_interrupted_copy_never_becomes_a_completed_home(self, booted, monkeypatch) -> None:
        """The completion manifest lands last through the pinned home descriptor.

        A failed copy may leave diagnostics in the isolated home, but neither the
        current boot nor systemd's automatic retry may treat that partial tree as
        a completed scenario and start the gateway.
        """

        def _copy_then_die(scenario: str, dst_fd: int) -> None:
            fd = os.open("partial.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dst_fd)
            try:
                os.write(fd, b"partial")
            finally:
                os.close(fd)
            raise rt.SeedError("copy interrupted")

        monkeypatch.setattr(seed_mod, "copy_fixture_into_dir_fd", _copy_then_die)
        rc, home, captured = booted("minimal")
        assert rc == 3
        assert "argv" not in captured
        assert (home / "partial.txt").read_text() == "partial"
        assert not (home / seed_mod.FIXTURE_MANIFEST).exists()

        # Restart=on-failure re-enters boot directly, without the outer `_up`
        # verification. The runtime itself must reject the partial home.
        rc, _, captured = booted("minimal")
        assert rc == 3
        assert "argv" not in captured

    def test_an_unknown_scenario_refuses_instead_of_booting_blank(self, booted, capsys) -> None:
        rc, home, captured = booted("no-such-scenario")
        assert rc == 3
        assert "argv" not in captured, "the gateway must not boot on a failed seed"
        assert "FATAL" in capsys.readouterr().out

    def test_a_directory_seed_keeps_its_config_only_behaviour(self, booted, tmp_path: Path) -> None:
        seed_dir = tmp_path / "seed-dir"
        seed_dir.mkdir()
        (seed_dir / "config.json").write_text(
            json.dumps({"tunnel": {"enabled": True}, "timezone": "UTC"})
        )
        (seed_dir / "crons.json").write_text(json.dumps({"version": 2, "jobs": []}))
        rc, home, _ = booted(str(seed_dir))
        assert rc == 0
        data = json.loads((home / "config.json").read_text())
        assert data["timezone"] == "UTC"
        assert data["tunnel"]["enabled"] is False
        # Only config.json is ever taken from a directory seed.
        assert not (home / "crons.json").exists()


class TestUpRefusesAnUnknownScenario:
    @pytest.mark.parametrize("scenario", ["no-such-scenario", "Rich"])
    def test_refuses_before_touching_the_host(self, scenario: str, monkeypatch, capsys) -> None:
        """The refusal must land before provisioning, port allocation or a start:
        an agent that mistyped a scenario should read the list, not a journal."""
        monkeypatch.setattr(pod_cli, "_resolve_or_die", lambda cfg, name: Path("/nope"))

        def _boom(*_a: object, **_k: object) -> None:  # pragma: no cover - must not run
            raise AssertionError("host was touched despite an unknown scenario")

        monkeypatch.setattr(rt, "derive_port", _boom)
        monkeypatch.setattr(rt, "start_pod", _boom)
        args = argparse.Namespace(name="wt", seed=scenario, json=False, ttl="2h", provision=False)
        with pytest.raises(SystemExit):
            pod_cli._up(PodConfig.load(), args)
        assert "unknown seed scenario" in capsys.readouterr().err

    def test_a_directory_seed_is_not_scenario_checked(self, monkeypatch) -> None:
        """`--seed ./whatever` must keep reaching the directory path untouched."""
        monkeypatch.setattr(pod_cli, "_resolve_or_die", lambda cfg, name: Path("/nope"))
        monkeypatch.setattr(
            rt, "resolve_seed_scenario", lambda v: pytest.fail("directory seed was name-checked")
        )
        # Stops right after the seed check, at the provisioning gate.
        monkeypatch.setattr(pod_cli.prov, "has_venv", lambda co: False)
        monkeypatch.setattr(pod_cli.prov, "ensure_venv", lambda co: False)
        args = argparse.Namespace(
            name="wt", seed="./some-dir", json=False, ttl="2h", provision=False
        )
        with pytest.raises(SystemExit):
            pod_cli._up(PodConfig.load(), args)
