"""End-to-end tailnet publish/withdraw — a REAL process boundary, no mocks.

The unit suites (`test_tailnet_serve.py`, `test_tailnet_cli.py`) patch
`subprocess.run`, so they prove the logic and prove nothing about the boundary.
This file removes the mock: a **real executable** named `tailscale` is written to
disk, discovered by the production `_cli_path`, spawned by the production
`subprocess.run` with the production `scrub_env()`, and answers over real stdout
with real exit codes. The command under test is the real `kirocrew tailnet`
entry point, and the config it writes is a real file on disk that the assertions
read back.

That catches a class the mocks structurally cannot:

* the argv a real daemon would actually receive (the fake records it verbatim),
* whether `scrub_env()` leaves the child a **usable** environment — a scrubber
  that stripped too much would pass every mocked test and fail every real one,
* exit codes and stderr propagating through the CLI's own exit path,
* the publish -> status -> withdraw **state machine**, since the fake persists
  what `serve` set and `serve status` reports it back.

Two things it deliberately does not claim.

**It is not Tailscale.** The fake reproduces the daemon's interface, not its
behaviour, so the schema of a real `tailscale status --json` remains unverified
here — this repo's host cannot obtain a real daemon (the package host and the Go
module proxy are both outside the sandbox's egress allowlist, and Tailscale ships
no binaries on GitHub releases). `TestAgainstARealDaemon` at the bottom closes
exactly that gap and **skips unless a real tailscale is installed**, so on a
machine that has one, the same assertions run for real.

**The candidate-path patch is the one unavoidable seam.** `_CLI_CANDIDATE_PATHS`
deliberately contains only root-owned locations, which is a security property with
its own test (`test_path_is_never_consulted`), so an unprivileged E2E cannot place
a binary where production would look. It is patched here rather than adding an
environment override to the product — an env-selectable CLI path would re-open the
exact binary-injection hole the allowlist exists to close.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.dashboard import tailnet, tailnet_serve

_DASH_PORT = 5476
_SUFFIX = "tail1a2b3c.ts.net"
_NAME = f"desk.{_SUFFIX}"

# A stand-in for the daemon: persists serve config to a JSON file so `serve
# status` reflects what `serve` did, and appends every invocation's argv so the
# test can assert on what a real daemon would have received.
_FAKE_CLI = '''import json, os, sys

state_path = "__STATE_PATH__"  # baked in, so scrub_env() needs no exception
with open(state_path) as fh:
    state = json.load(fh)

state["calls"].append(sys.argv[1:])
state["env_seen"] = {k: v for k, v in os.environ.items() if k.startswith(("AWS_", "TS_FAKE"))}
state["env_size"] = len(os.environ)


args = sys.argv[1:]


def save():
    with open(state_path, "w") as fh:
        json.dump(state, fh)


args = sys.argv[1:]

# Fails only WRITES, never the status read: a daemon that answers `serve status`
# can still refuse `serve --bg`, and failing both would let publish's occupancy
# guard swallow the refusal instead of the write reporting it.
if state.get("fail_with") and args[:2] != ["status", "--json"] and args[:3] != ["serve", "status", "--json"]:
    save()
    sys.stderr.write(state["fail_with"])
    sys.exit(1)

if args[:2] == ["status", "--json"]:
    save()
    print(json.dumps({
        "BackendState": "Running",
        "Self": {"DNSName": state["dns_name"]},
        "CurrentTailnet": {"MagicDNSSuffix": state["suffix"]},
    }))
    sys.exit(0)

if args[:3] == ["serve", "status", "--json"]:
    save()
    print(json.dumps(state["serve"]))
    sys.exit(0)

if args[0] == "serve" and "--bg" in args:
    target = args[-1]
    state["serve"] = {"Web": {f"{state['dns_name']}:443": {"Handlers": {"/": {"Proxy": target}}}}}
    save()
    sys.exit(0)

if args[:2] == ["serve", "--https"] and args[-1] == "off":
    state["serve"] = {}
    save()
    sys.exit(0)

save()
sys.stderr.write("fake tailscale: unhandled argv %r\\n" % (args,))
sys.exit(2)
'''


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A real fake-CLI on disk, a real data home, and the state file to inspect."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"calls": [], "serve": {}, "dns_name": _NAME, "suffix": _SUFFIX}))

    # The state path is baked into the script rather than passed through the
    # environment, so `scrub_env()` runs completely unpatched. Handing the child a
    # test-only variable would have meant weakening the very scrubber one of these
    # tests exists to verify.
    body = tmp_path / "fake_tailscale.py"
    body.write_text(_FAKE_CLI.replace("__STATE_PATH__", str(state).replace("\\", "\\\\")))

    # The launcher must be executable BY THE OS, not just marked +x. A shebang
    # script is not executable on Windows -- `subprocess.run` raises OSError, which
    # the product then reported as "tailscale was not found" (a real diagnostic bug
    # this exposed, now fixed) and which made this whole suite fail on the Windows
    # shard. So the launcher is platform-native: a `.bat` that calls the interpreter
    # on Windows, a shebang script elsewhere.
    if sys.platform == "win32":
        cli = tmp_path / "tailscale.bat"
        cli.write_text(f'@echo off\r\n"{sys.executable}" "{body}" %*\r\n')
    else:
        cli = tmp_path / "tailscale"
        cli.write_text(f"#!{sys.executable}\n" + body.read_text())
        cli.chmod(cli.stat().st_mode | stat.S_IXUSR)

    home = tmp_path / "home"
    home.mkdir()
    # The trust flag is a PRECONDITION of `up` (it checks, and never writes config),
    # so the end-to-end success path starts from a host where it is already enabled.
    (home / "config.json").write_text(
        json.dumps(
            {
                "timezone": "UTC",
                "dashboard": {
                    "url": f"http://localhost:{_DASH_PORT}",
                    "tailscale": {"enabled": True},
                },
            }
        )
    )

    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(tailnet, "_CLI_CANDIDATE_PATHS", (str(cli),))
    # SECOND deliberate seam, and it disables a security guard, so it is stated
    # loudly rather than quietly worked around. `_cli_path` refuses any candidate the
    # gateway user can write (planted-binary defence): the pinned list needs root,
    # and an agent that can write a `tailscale` there would get it executed on the
    # auth path. An unprivileged E2E cannot satisfy that — its fake lives in a
    # user-owned tmp dir by construction.
    #
    # `test_the_planted_binary_defence_refuses_this_fake` below asserts the guard
    # really does reject this fake when NOT patched, so this bypass is proven
    # deliberate and cannot rot into an accidentally-disabled control.
    monkeypatch.setattr(tailnet, "_posix_candidate_trusted", lambda _c: True)
    monkeypatch.setenv("KIROCREW_PORT", str(_DASH_PORT))
    return {"cli": cli, "state": state, "home": home}


def _run_cli(*argv: str) -> int:
    """Invoke the real `kirocrew` entry point in-process; return its exit code."""
    from kiro_crew.cli import main

    with patch.object(sys, "argv", ["kirocrew", *argv]):
        try:
            main()
        except SystemExit as exc:
            return int(exc.code or 0)
    return 0


def _state(env) -> dict:
    return json.loads(Path(env["state"]).read_text())


def _config(env) -> dict:
    return json.loads((env["home"] / "config.json").read_text())


class TestTheSeamIsDeliberate:
    """The fixture disables the planted-binary defence; prove that on purpose.

    A test that switches off a security control has to demonstrate the control was
    live, or the bypass silently becomes "this guard does nothing" the next time
    someone refactors it.
    """

    def test_the_planted_binary_defence_refuses_this_fake(self, env, monkeypatch) -> None:
        # Undo only the trust bypass; the candidate list still points at the fake.
        monkeypatch.undo()
        monkeypatch.setattr(tailnet, "_CLI_CANDIDATE_PATHS", (str(env["cli"]),))
        if not tailnet.IS_POSIX:
            pytest.skip("the writability guard is POSIX-only")
        assert tailnet._cli_path() is None, (
            "a user-writable fake must be refused — if this passes, the guard the "
            "fixture bypasses is no longer doing anything"
        )


class TestPublishWithdrawRoundTrip:
    def test_up_publishes_and_records(self, env, capsys) -> None:
        assert _run_cli("tailnet", "up") == 0
        out = capsys.readouterr().out

        # The daemon really was invoked, with the argv a real one would receive.
        calls = _state(env)["calls"]
        assert ["serve", "--bg", "--https=443", f"http://127.0.0.1:{_DASH_PORT}"] in calls

        # The config really was written to disk, and the URL really was printed.
        assert _config(env)["dashboard"]["tailscale"]["enabled"] is True
        assert f"https://{_NAME}" in out
        assert "Restart the gateway" in out

    def test_status_after_up_reports_published(self, env, capsys) -> None:
        _run_cli("tailnet", "up")
        capsys.readouterr()
        assert _run_cli("tailnet", "status") == 0
        out = capsys.readouterr().out
        assert "Published:  yes" in out
        assert f"https://{_NAME}" in out

    def test_down_withdraws_what_up_published(self, env, capsys) -> None:
        _run_cli("tailnet", "up")
        capsys.readouterr()
        assert _run_cli("tailnet", "down") == 0
        # The serve config is really gone from the daemon's state.
        assert _state(env)["serve"] == {}
        # And the trust setting is deliberately left alone.
        assert _config(env)["dashboard"]["tailscale"]["enabled"] is True

    def test_status_before_anything_is_not_published(self, env, capsys) -> None:
        assert _run_cli("tailnet", "status") == 0
        out = capsys.readouterr().out
        assert "Published:  no" in out


class TestWithdrawalProtectsForeignMappings:
    def test_a_foreign_443_mapping_survives_down(self, env, capsys) -> None:
        """The defect two review rounds were spent on, exercised for real.

        Someone else's service is on 443. `down` must refuse rather than run
        `serve --https 443 off`, and the foreign mapping must still be there
        afterwards.
        """
        state = _state(env)
        state["serve"] = {"Web": {f"{_NAME}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}
        Path(env["state"]).write_text(json.dumps(state))

        assert _run_cli("tailnet", "down") == 1
        after = _state(env)
        assert after["serve"] != {}, "a mapping that is not ours must not be removed"
        assert not any(c[:2] == ["serve", "--https"] and c[-1] == "off" for c in after["calls"])


class TestRealFailurePropagation:
    def test_a_refusing_daemon_stops_up_and_touches_no_config(self, env, capsys) -> None:
        """A real non-zero exit + real stderr, through the real CLI exit path."""
        state = _state(env)
        state["fail_with"] = "access denied: serve config denied\n"
        Path(env["state"]).write_text(json.dumps(state))

        assert _run_cli("tailnet", "up") == 1
        err = capsys.readouterr().err
        # The daemon's own words survive the whole stack.
        assert "access denied" in err
        # And the remedy this repo's classifier adds is there too.
        assert "--operator" in err
        # The command writes no config at all, so the operator's own setting is
        # exactly as they left it — a failed publish cannot corrupt or flip it.
        assert _config(env)["dashboard"]["tailscale"]["enabled"] is True


class TestTheChildEnvironmentIsUsable:
    def test_credentials_are_absent_but_the_child_still_works(self, env, monkeypatch) -> None:
        """`scrub_env` has to remove secrets AND leave a working environment.

        A scrubber that stripped too much would pass every mocked test and fail on
        a real host — the child would not start, or the real CLI would not find its
        daemon socket. Only a real spawn can tell the difference.
        """
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-be-inherited")
        assert _run_cli("tailnet", "up") == 0
        seen = _state(env)
        assert "AWS_SECRET_ACCESS_KEY" not in seen["env_seen"]
        assert seen["env_size"] > 1, "an emptied environment breaks the real CLI on macOS/Windows"
        assert seen["calls"], "the child really ran — an unusable env would show up as no calls"


_REAL_CLI = next(
    (p for p in tailnet._CLI_CANDIDATE_PATHS if os.path.isfile(p) and os.access(p, os.X_OK)),
    None,
)


@pytest.mark.skipif(_REAL_CLI is None, reason="no real tailscale installed on this host")
class TestAgainstARealDaemon:
    """The gap the fake cannot close: Tailscale's own output.

    Skipped wherever Tailscale is absent (including this repo's dev hosts and CI),
    and it is the whole verification on a machine that has it. Asserts only what is
    true regardless of login state, so it passes on a logged-out node too — the
    point is that the production parsers survive REAL output rather than output
    this repo invented.
    """

    def test_status_parses_without_raising(self) -> None:
        name = tailnet.self_dns_name()
        assert name is None or (isinstance(name, str) and " " not in name)

    def test_a_resolved_name_agrees_with_the_daemons_own_suffix(self) -> None:
        raw = subprocess.run(  # noqa: S603 - vetted path from the product's own allowlist
            [str(_REAL_CLI), "status", "--json"], capture_output=True, text=True, timeout=10
        )
        if raw.returncode != 0:
            pytest.skip("daemon not answering; nothing to compare against")
        doc = json.loads(raw.stdout)
        suffix = (doc.get("CurrentTailnet") or {}).get("MagicDNSSuffix")
        name = tailnet.self_dns_name()
        if name is None:
            return
        assert suffix and name.endswith(f".{suffix}")

    def test_serve_state_reports_a_definite_answer_or_says_unknown(self) -> None:
        state = tailnet_serve.serve_state(_DASH_PORT)
        assert state.published in (True, False, None)
        assert state.detail
