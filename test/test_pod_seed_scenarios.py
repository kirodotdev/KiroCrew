"""Seed scenarios and the agent-facing pod verbs (``api`` / ``scenarios``).

Two halves:

* A parametrized validation pass over EVERY fixture shipped in
  ``kiro_crew/tests_fixtures`` -- it loads, it describes itself, and it carries
  no credential-shaped text. Parametrizing over the live registry rather than a
  hand-written list is the point: a fixture added to the package is validated
  without this file being touched, which is what stops a broken or
  secret-bearing fixture from reaching the wheel.
* Unit coverage for ``pod up --seed <scenario>``, ``pod api`` and
  ``pod scenarios``, including the refusal paths an agent has to be able to read
  (unknown scenario, non-2xx, pod not up).
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

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
        # The three the rest of the suite and the seeded_home default depend on.
        for expected in ("empty", "minimal", "rich"):
            assert expected in _ALL_FIXTURES

    def test_every_fixture_has_a_one_line_summary(self) -> None:
        missing = [name for name in _ALL_FIXTURES if not seed_mod.fixture_summary(name)]
        assert not missing, f"fixtures with no description in fixture.yaml: {missing}"

    def test_summary_of_unknown_fixture_is_empty_not_an_error(self) -> None:
        # `pod scenarios` must survive a half-written fixture rather than crash.
        assert seed_mod.fixture_summary("no-such-fixture") == ""


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
        ["rich", "minimal", "crons-active", "a", "x.y_z-1", "Rich", "NOT_A_FIXTURE"],
    )
    def test_bare_tokens_are_scenarios(self, value: str) -> None:
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


class TestScenariosVerb:
    def test_human_listing_names_every_scenario(self, capsys: pytest.CaptureFixture) -> None:
        pod_cli._scenarios(PodConfig.load(), argparse.Namespace(json=False))
        out = capsys.readouterr().out
        for name in _ALL_FIXTURES:
            assert name in out
        assert "kirocrew pod up <worktree> --seed" in out

    def test_json_listing_carries_descriptions(self, capsys: pytest.CaptureFixture) -> None:
        pod_cli._scenarios(PodConfig.load(), argparse.Namespace(json=True))
        rows = json.loads(capsys.readouterr().out)
        assert [r["name"] for r in rows] == _ALL_FIXTURES
        assert all(r["description"] for r in rows)


class TestSeedHomeFromScenario:
    def test_populates_an_absent_home(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        assert rt.seed_home_from_scenario(cfg, "wt", "minimal") is True
        assert (cfg.home_dir("wt") / "crons.json").is_file()

    def test_leaves_a_populated_home_alone(self, tmp_path: Path, monkeypatch) -> None:
        """A restart must never wipe the state the operator is podding to inspect."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        home = cfg.home_dir("wt")
        home.mkdir(parents=True)
        (home / "sessions").mkdir()
        (home / "sessions" / "live.jsonl").write_text("{}\n")
        assert rt.seed_home_from_scenario(cfg, "wt", "minimal") is False
        assert (home / "sessions" / "live.jsonl").is_file()
        assert not (home / "crons.json").exists()

    def test_restores_the_callers_home_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "callers-home"))
        rt.seed_home_from_scenario(PodConfig.load(), "wt", "empty")
        import os

        assert os.environ["KIROCREW_HOME"] == str(tmp_path / "callers-home")

    def test_unknown_scenario_raises_in_the_pod_vocabulary(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        with pytest.raises(rt.PodError):
            rt.seed_home_from_scenario(PodConfig.load(), "wt", "no-such-fixture")


class TestSanitizeHomeConfig:
    def test_forces_every_self_activating_channel_off(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"tunnel": {"enabled": True}, "discord": {"enabled": True, "keep": 1}})
        )
        rt.sanitize_home_config(tmp_path)
        data = json.loads((tmp_path / "config.json").read_text())
        for section in rt.SEED_DISABLED_SECTIONS:
            assert data[section]["enabled"] is False
        # Non-enable keys in a touched section survive.
        assert data["discord"]["keep"] == 1

    def test_absent_or_malformed_config_is_a_no_op(self, tmp_path: Path) -> None:
        rt.sanitize_home_config(tmp_path)  # no config.json at all
        (tmp_path / "config.json").write_text("{not json")
        rt.sanitize_home_config(tmp_path)
        assert (tmp_path / "config.json").read_text() == "{not json"

    def test_a_seeded_scenario_config_ends_up_sanitized(self, tmp_path: Path, monkeypatch) -> None:
        """End-to-end for the guarantee: a fixture's own config cannot leave a
        channel enabled, which is what `--seed <dir>` has always refused."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        rt.seed_home_from_scenario(cfg, "wt", "connections-two")
        rt.sanitize_home_config(cfg.home_dir("wt"))
        data = json.loads((cfg.home_dir("wt") / "config.json").read_text())
        assert data["tunnel"]["enabled"] is False


class TestApiPath:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("sessions", "/api/sessions"),
            ("/sessions", "/api/sessions"),
            ("api/sessions", "/api/sessions"),
            ("/api/sessions", "/api/sessions"),
            ("http://127.0.0.1:7811/api/health", "/api/health"),
            ("/api/sessions?limit=2", "/api/sessions?limit=2"),
            ("  /api/health  ", "/api/health"),
        ],
    )
    def test_normalizes_every_shape_a_caller_has_in_hand(self, given: str, expected: str) -> None:
        assert rt.api_path(given) == expected

    def test_a_malformed_url_refuses_instead_of_raising_valueerror(self) -> None:
        """An unparseable authority must read as one `pod: …` line, not a traceback."""
        with pytest.raises(rt.PodError) as excinfo:
            rt.api_path("http://[bad/api/health")
        assert "invalid request path" in str(excinfo.value)


class TestBodySizeCeiling:
    """A response is buffered in the CLI's memory, so it needs a ceiling."""

    def test_a_body_one_byte_over_the_cap_refuses(self) -> None:
        class _Big:
            def read(self, n: int) -> bytes:
                return b"x" * n

        with pytest.raises(rt.PodError) as excinfo:
            rt._read_capped(_Big(), "GET", "/api/huge", "wt")
        assert str(rt.API_BODY_MAX_BYTES) in str(excinfo.value)

    def test_a_body_at_the_cap_is_returned_whole(self) -> None:
        payload = b"y" * 128

        class _Small:
            def read(self, n: int) -> bytes:
                return payload

        assert rt._read_capped(_Small(), "GET", "/api/small", "wt") == payload.decode()


class _Handler(BaseHTTPRequestHandler):
    """Stand-in gateway: echoes the request so the test can assert what was sent."""

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        if self.path.endswith("/boom"):
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "unavailable"}).encode())
            return
        if self.path.endswith("/plain"):
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not json at all")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "method": self.command,
                    "path": self.path,
                    "auth": self.headers.get("Authorization", ""),
                    "sent": body,
                }
            ).encode()
        )

    do_GET = _respond
    do_POST = _respond
    do_DELETE = _respond

    def log_message(self, *_args: object) -> None:  # silence the test server
        return


@pytest.fixture
def stub_gateway(monkeypatch) -> HTTPServer:
    """A live loopback HTTP server wired in as pod ``wt``'s gateway.

    The pod half is stubbed at exactly two seams -- liveness and the credential
    mint -- because those are the only two things `pod_api` asks the host. The
    HTTP call itself is real, so the header, method, body and status handling are
    all genuinely exercised rather than asserted against a mock.
    """
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(rt, "is_active", lambda cfg, name: True)
    monkeypatch.setattr(rt, "derive_port", lambda cfg, name: server.server_address[1])
    monkeypatch.setattr(rt, "mint_token", lambda cfg, name, ttl="2h": "fixture-token")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestPodApi:
    def test_sends_the_bearer_token_and_normalized_path(self, stub_gateway: HTTPServer) -> None:
        status, raw = rt.pod_api(PodConfig.load(), "wt", "GET", "sessions")
        assert status == 200
        echoed = json.loads(raw)
        assert echoed["method"] == "GET"
        assert echoed["path"] == "/api/sessions"
        assert echoed["auth"] == "Bearer fixture-token"

    def test_sends_a_json_body_on_post(self, stub_gateway: HTTPServer) -> None:
        status, raw = rt.pod_api(
            PodConfig.load(), "wt", "POST", "config", data='{"key":"agent.model"}'
        )
        assert status == 200
        assert json.loads(raw)["sent"] == '{"key":"agent.model"}'

    def test_returns_a_non_2xx_body_rather_than_raising(self, stub_gateway: HTTPServer) -> None:
        status, raw = rt.pod_api(PodConfig.load(), "wt", "GET", "boom")
        assert status == 503
        assert json.loads(raw)["error"] == "unavailable"

    def test_unsupported_method_is_refused_by_name(self) -> None:
        with pytest.raises(rt.PodError, match="unsupported method"):
            rt.pod_api(PodConfig.load(), "wt", "TRACE", "sessions")

    def test_pod_not_up_names_the_command_that_starts_it(self, monkeypatch) -> None:
        monkeypatch.setattr(rt, "is_active", lambda cfg, name: False)
        with pytest.raises(rt.PodError) as excinfo:
            rt.pod_api(PodConfig.load(), "wt", "GET", "sessions")
        assert "kirocrew pod up wt" in str(excinfo.value)

    def test_unreachable_port_points_at_status_and_logs(self, monkeypatch) -> None:
        # A closed port: `is_active` says the unit is up but nothing answers,
        # which is the crash-looping case an agent needs routed to the journal.
        monkeypatch.setattr(rt, "is_active", lambda cfg, name: True)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, name, ttl="2h": "t")
        with _closed_port() as port:
            monkeypatch.setattr(rt, "derive_port", lambda cfg, name: port)
            with pytest.raises(rt.PodError) as excinfo:
                rt.pod_api(PodConfig.load(), "wt", "GET", "sessions")
        assert "kirocrew pod logs wt" in str(excinfo.value)


def _closed_port():
    """Context manager yielding a port number nothing is listening on."""
    import contextlib
    import socket

    @contextlib.contextmanager
    def _inner():
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        yield port

    return _inner()


class TestApiVerb:
    def _args(self, path: str, method: str = "GET", data: str = "") -> argparse.Namespace:
        return argparse.Namespace(name="wt", method=method, path=path, data=data)

    def test_prints_one_fixed_shape_document(
        self, stub_gateway: HTTPServer, capsys: pytest.CaptureFixture
    ) -> None:
        pod_cli._api(PodConfig.load(), self._args("sessions"))
        doc = json.loads(capsys.readouterr().out)
        assert doc["name"] == "wt"
        assert doc["method"] == "GET"
        assert doc["path"] == "/api/sessions"
        assert doc["status"] == 200
        assert doc["ok"] is True
        assert doc["body"]["path"] == "/api/sessions"

    def test_lowercase_method_is_accepted(
        self, stub_gateway: HTTPServer, capsys: pytest.CaptureFixture
    ) -> None:
        pod_cli._api(PodConfig.load(), self._args("sessions", method="get"))
        assert json.loads(capsys.readouterr().out)["method"] == "GET"

    def test_non_2xx_prints_the_body_and_exits_nonzero(
        self, stub_gateway: HTTPServer, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            pod_cli._api(PodConfig.load(), self._args("boom"))
        assert excinfo.value.code == 1
        doc = json.loads(capsys.readouterr().out)
        assert doc["ok"] is False
        assert doc["status"] == 503
        assert doc["body"]["error"] == "unavailable"

    def test_non_json_body_is_carried_as_text(
        self, stub_gateway: HTTPServer, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit):
            pod_cli._api(PodConfig.load(), self._args("plain"))
        assert json.loads(capsys.readouterr().out)["body"] == "not json at all"

    def test_pod_not_up_exits_1_with_the_fix_on_stderr(
        self, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(rt, "is_active", lambda cfg, name: False)
        with pytest.raises(SystemExit) as excinfo:
            pod_cli._api(PodConfig.load(), self._args("sessions"))
        assert excinfo.value.code == 1
        assert "kirocrew pod up wt" in capsys.readouterr().err


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
        rc, home, captured = booted("crons-active")
        assert rc == 0
        # The fixture's own content is on disk...
        crons = json.loads((home / "crons.json").read_text())
        assert len(crons["jobs"]) == 3
        # ...and the gateway was exec'd afterwards, pointed at that home.
        assert captured["argv"][0].endswith("kirocrew")
        assert captured["env"]["KIROCREW_HOME"] == str(home)
        assert "seeded home from scenario 'crons-active'" in capsys.readouterr().out

    def test_the_seeded_config_is_sanitized(self, booted) -> None:
        _, home, _ = booted("connections-two")
        data = json.loads((home / "config.json").read_text())
        for section in rt.SEED_DISABLED_SECTIONS:
            assert data[section]["enabled"] is False

    def test_a_restart_does_not_re_seed(self, booted, capsys) -> None:
        _, home, _ = booted("sessions-a-few")
        (home / "sessions" / "dashboard_open-slot.jsonl").unlink()
        capsys.readouterr()
        booted("sessions-a-few")  # the Restart=on-failure re-exec
        assert not (home / "sessions" / "dashboard_open-slot.jsonl").exists()
        assert "not re-applied" in capsys.readouterr().out

    def test_an_interrupted_copy_leaves_no_partial_home(
        self, booted, tmp_path: Path, monkeypatch
    ) -> None:
        """A failed copy must not leave a tree the emptiness check calls seeded.

        The next boot decides "already seeded" purely from the HOME being
        non-empty, so a half-copied fixture would be accepted and the gateway
        would open on part of a scenario. Staging plus rename is what makes the
        failure leave nothing behind.
        """
        real_seed = rt.seed

        def _seed_then_die(scenario: str, **kwargs: object) -> None:
            real_seed(scenario, **kwargs)  # writes the fixture into the staging dir
            raise rt.SeedError("copy interrupted")

        monkeypatch.setattr(rt, "seed", _seed_then_die)
        rc, home, _ = booted("crons-active")
        assert rc != 0, "an interrupted seed must fail the boot, not proceed"
        assert not (
            home.exists() and any(home.iterdir())
        ), f"partial home survived: {sorted(p.name for p in home.iterdir())}"
        assert not list(home.parent.glob(".*seeding")), "staging directory was left behind"

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
    def test_refuses_before_touching_the_host(self, monkeypatch, capsys) -> None:
        """The refusal must land before provisioning, port allocation or a start:
        an agent that mistyped a scenario should read the list, not a journal."""
        monkeypatch.setattr(pod_cli, "_resolve_or_die", lambda cfg, name: Path("/nope"))

        def _boom(*_a: object, **_k: object) -> None:  # pragma: no cover - must not run
            raise AssertionError("host was touched despite an unknown scenario")

        monkeypatch.setattr(rt, "derive_port", _boom)
        monkeypatch.setattr(rt, "start_pod", _boom)
        args = argparse.Namespace(
            name="wt", seed="no-such-scenario", json=False, ttl="2h", provision=False
        )
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
