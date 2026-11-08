"""The cloud sign-in on a box whose Crew runs the KAS bundle route.

Every remote script decides ON THE BOX which program owns the sign-in, by the
box's own ``kirocrew kas route``: the vault verbs when it prints ``kas-bundle``,
the kiro-cli verbs it always ran otherwise -- byte for byte, including when the
probe prints nothing at all (a box whose Kiro Crew predates the verb). The local
Python reads one set of sentinels whichever branch ran. Half of these tests read
the generated scripts; the other half run them under bash against stub
``kirocrew`` / ``kiro-cli`` programs, one isolated HOME per route, and never start
a background login (the device-login script is exercised only on its text).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from kiro_crew.cloud import login, login_vault, ssm
from kiro_crew.cloud.login_target import KiroLoginTarget

_IDC = KiroLoginTarget.from_fields(
    license="pro", start_url="https://example.awsapps.com/start", region="us-east-1"
)

_SCRIPTS = {
    "check": login._login_check_command(),
    "whoami": login._whoami_json_command(),
    "logout": login._logout_command(),
    "device_default": login._device_login_command(replace_existing=True),
    "device_idc": login._device_login_command(replace_existing=False, **_IDC.login_kwargs()),
    "callback": login._callback_login_command(),
    "resume": login._resume_login_command(),
}

_VAULT_VERBS = {
    "check": '"$KIROCREW" kas whoami < /dev/null',
    "whoami": '"$KIROCREW" kas whoami --format json < /dev/null',
    "logout": '"$KIROCREW" kas logout < /dev/null',
    "device_default": 'nohup "$KIROCREW" kas login >',
    "device_idc": (
        'nohup "$KIROCREW" kas login --provider idc --start-url '
        "https://example.awsapps.com/start --region us-east-1 >"
    ),
    "resume": 'nohup "$KIROCREW" kas login >',
}

_KIRO_CLI_VERBS = {
    "check": '"$KIRO" whoami < /dev/null',
    "whoami": '"$KIRO" whoami --format json < /dev/null',
    "logout": '"$KIRO" logout < /dev/null',
    "device_default": '"$KIRO" login --use-device-flow >',
    "device_idc": '"$KIRO" login --use-device-flow --identity-provider https://example.awsapps.com/start',
    "resume": '"$KIRO" login --use-device-flow >',
}


class TestScriptText:
    @pytest.mark.parametrize("name", sorted(_SCRIPTS))
    def test_every_script_asks_the_box_for_its_route(self, name):
        cmd = _SCRIPTS[name]
        assert login_vault.KIROCREW_BIN_RESOLVE in cmd
        assert '"$KIROCREW" kas route' in cmd
        assert login_vault.is_vault_route_test() in cmd

    @pytest.mark.parametrize("name", sorted(_VAULT_VERBS))
    def test_both_programs_are_present_and_the_vault_one_is_the_if_branch(self, name):
        cmd = _SCRIPTS[name]
        test_at = cmd.index(login_vault.is_vault_route_test())
        vault_at = cmd.index(_VAULT_VERBS[name])
        cli_at = cmd.index(_KIRO_CLI_VERBS[name])
        assert test_at < vault_at < cli_at, name

    def test_the_route_is_chosen_only_by_the_exact_spelling(self):
        """An empty or foreign answer takes the kiro-cli branch: the vault is never
        selected by an absence."""
        assert login_vault.is_vault_route_test() == '[ "$KC_ROUTE" = "kas-bundle" ]'
        assert "|| true" in login_vault.route_prelude()

    def test_the_vault_launch_echoes_the_route_sentinel_for_the_local_side(self):
        for name in ("device_default", "device_idc", "resume"):
            cmd = _SCRIPTS[name]
            sentinel_at = cmd.index(f'echo "{login_vault.VAULT_ROUTE_SENTINEL}"')
            launch_at = cmd.index(_VAULT_VERBS[name])
            assert sentinel_at < launch_at, name

    def test_the_vault_login_takes_its_identity_from_flags_and_needs_no_pty(self):
        """kiro-cli's IdC login prompts and needs the pty driver; the vault verb does
        not, so the driver staging sits in the kiro-cli branch only."""
        cmd = _SCRIPTS["device_idc"]
        else_at = cmd.index("\nelse\n", cmd.index(_VAULT_VERBS["device_idc"]))
        assert cmd.index("KC_DRIVER_DIR=") > else_at

    def test_the_vault_login_flags_are_shell_quoted(self):
        evil = "$(touch /tmp/pwned)"
        launch = login_vault.login_launch(
            log_path="$KC_LOGIN_DIR/x.log", identity_provider=evil, idp_region="us east"
        )
        assert f"--start-url {evil}" not in launch
        assert "--start-url '$(touch /tmp/pwned)'" in launch
        assert "--region 'us east'" in launch

    def test_the_default_target_adds_no_identity_flags_to_the_vault_login(self):
        launch = login_vault.login_launch(log_path="$KC_LOGIN_DIR/x.log")
        assert "--provider" not in launch
        assert "--start-url" not in launch
        assert "--region" not in launch

    def test_the_callback_flow_refuses_on_the_vault_route(self):
        cmd = _SCRIPTS["callback"]
        refusal = cmd.index(f'echo "{login_vault.VAULT_NO_CALLBACK_SENTINEL}"')
        assert refusal < cmd.index("mkfifo")
        # It exits before touching the login directory: nothing to cancel later.
        assert refusal < cmd.index("KC_LOGIN_DIR=")

    def test_cancel_and_replace_stop_a_vault_login_too(self):
        for cmd in (
            login._cancel_login_command(),
            login._device_login_command(replace_existing=True),
        ):
            assert f'pkill -u "$(id -u)" -f "{login._LOGIN_PROCESS_PATTERN}"' in cmd
            assert f'pkill -u "$(id -u)" -f "{login_vault.VAULT_LOGIN_PROCESS_PATTERN}"' in cmd
        # The stop is only confirmed once BOTH command lines are gone.
        cancel = login._cancel_login_command()
        assert f'pgrep -u "$(id -u)" -f "{login_vault.VAULT_LOGIN_PROCESS_PATTERN}"' in cancel

    def test_the_vault_login_pattern_matches_what_the_box_launches(self):
        import re

        pat = re.compile(login_vault.VAULT_LOGIN_PROCESS_PATTERN)
        for cmdline in (
            "/srv/crew/.local/bin/kirocrew kas login",
            "python3 /srv/crew/.local/bin/kirocrew kas login --provider idc --start-url https://x",
        ):
            assert pat.search(cmdline), cmdline
        for other in (
            "kirocrew kas logout",
            "kirocrew kas whoami --format json",
            "kirocrew kas route",
            "kirocrew gateway",
            "kiro-cli login",
        ):
            assert not pat.search(other), other

    def test_the_vault_logout_kills_no_engine_process(self):
        """The gateway's pre-turn identity-change check retires them; the kiro-cli
        branch's `pkill kiro-cli acp` has no vault counterpart on purpose."""
        lines = login_vault.logout_lines()
        assert "kas logout" in lines
        assert "acp" not in lines
        assert "kiro-cli" not in lines


# ---------------------------------------------------------------------------
# The local Python's reading of the vault branch's outputs.
# ---------------------------------------------------------------------------


def _result(stdout: str, status: str = "Success", exit_code: int = 0) -> ssm.CommandResult:
    return ssm.CommandResult(status, stdout, "", exit_code)


class TestLocalSide:
    def test_a_vault_prompt_is_read_exactly_like_a_kiro_cli_one(self, monkeypatch):
        monkeypatch.setattr(login, "is_logged_in", lambda *a, **k: False)
        monkeypatch.setattr(
            ssm,
            "run_command",
            lambda *a, **k: _result(
                f"{login_vault.VAULT_ROUTE_SENTINEL}\n"
                "Open https://device.sso.us-east-1.amazonaws.com/?user_code=WXYZ-1234 in a browser\n"
                "code: WXYZ-1234\n"
                "Waiting for approval; this prompt expires at 2099-01-01T00:00:00+00:00 ...\n"
            ),
        )
        prompt = login.start_device_login("i-0abc", "dev", "us-east-1", open_browser=False)
        assert prompt.url == "https://device.sso.us-east-1.amazonaws.com/?user_code=WXYZ-1234"
        assert prompt.code == "WXYZ-1234"
        assert prompt.actionable and not prompt.error

    def test_no_prompt_on_the_vault_route_never_falls_back_to_the_callback_flow(self, monkeypatch):
        monkeypatch.setattr(login, "is_logged_in", lambda *a, **k: False)
        monkeypatch.setattr(
            ssm,
            "run_command",
            lambda *a, **k: _result(
                f"{login_vault.VAULT_ROUTE_SENTINEL}\n❌ could not reach the sign-in service: boom\n"
            ),
        )

        def _never(*a, **k):
            raise AssertionError("the kiro-cli callback flow must not run on a vault-route box")

        monkeypatch.setattr(login, "_start_callback_login", _never)
        prompt = login.start_device_login("i-0abc", "dev", "us-east-1", open_browser=False)
        assert not prompt.actionable
        assert "Kiro Crew's own vault" in prompt.error
        assert "kirocrew kas login" in prompt.error

    def test_the_callback_refusal_sentinel_is_an_error_not_a_silent_prompt(self, monkeypatch):
        monkeypatch.setattr(
            ssm,
            "run_command",
            lambda *a, **k: _result(login_vault.VAULT_NO_CALLBACK_SENTINEL, "Success", 1),
        )
        prompt = login._start_callback_login("i-0abc", "dev", "us-east-1", open_browser=False)
        assert prompt.error and not prompt.actionable and not prompt.ports

    def test_a_vault_whoami_document_is_the_remote_identity(self, monkeypatch):
        doc = json.dumps(
            {
                "accountType": "IamIdentityCenter",
                "identity": "identity_center",
                "startUrl": "https://example.awsapps.com/start",
                "expiresAt": "2099-01-01T00:00:00+00:00",
            }
        )
        monkeypatch.setattr(ssm, "run_command", lambda *a, **k: _result(doc + "\n"))
        assert login.remote_identity("i-0abc", "dev", "us-east-1") == {
            "account_type": "IamIdentityCenter",
            "start_url": "https://example.awsapps.com/start",
        }
        assert login.remote_identity_state("i-0abc", "dev", "us-east-1", target=_IDC) == "match"
        assert (
            login.remote_identity_state("i-0abc", "dev", "us-east-1", target=KiroLoginTarget())
            == "mismatch"
        )


class TestLocalIdentityOnTheBundleRoute:
    def test_the_local_vault_answers_when_this_machine_runs_the_bundle_route(self, monkeypatch):
        from kiro_crew.cloud import login_target

        monkeypatch.setattr(login_target, "_local_vault_owns_sign_in", lambda: True)
        monkeypatch.setattr(
            login_target, "local_vault_identity", lambda: {"account_type": "BuilderId"}
        )

        def _never():
            raise AssertionError("kiro-cli must not be resolved on the bundle route")

        monkeypatch.setattr(login_target, "resolve_kiro_cli", _never)
        assert login_target.discover_local_identity() == {"account_type": "BuilderId"}

    def test_an_explicit_binary_still_asks_kiro_cli(self, monkeypatch):
        from kiro_crew.cloud import login_target

        monkeypatch.setattr(login_target, "_local_vault_owns_sign_in", lambda: True)
        seen = {}

        class _Proc:
            returncode = 0
            stdout = '{"accountType": "BuilderId"}'
            stderr = ""

        def _run(argv, **kw):
            seen["argv"] = argv
            return _Proc()

        monkeypatch.setattr(login_target.subprocess, "run", _run)
        assert login_target.discover_local_identity("kiro-cli-explicit") == {
            "account_type": "BuilderId"
        }
        assert seen["argv"][0] == "kiro-cli-explicit"

    def test_local_vault_identity_has_the_three_answers(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone

        from kiro_crew.auth import bridge
        from kiro_crew.auth.store import KasToken, TokenStore
        from kiro_crew.cloud import login_target

        store = TokenStore(tmp_path)
        monkeypatch.setattr(bridge, "default_token_store", lambda: store)
        # Nothing stored: nothing to inherit.
        assert login_target.local_vault_identity() == {}
        store.save(
            KasToken(
                access_token="atk",
                expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
                provider="BuilderId",
                identity="builder_id",
            )
        )
        assert login_target.local_vault_identity() == {"account_type": "BuilderId"}

        def _broken():
            raise OSError("vault unreadable")

        monkeypatch.setattr(bridge, "default_token_store", _broken)
        assert login_target.local_vault_identity() is None

    def test_an_entry_that_will_not_read_is_an_unknown_identity_not_builder_id(
        self, tmp_path, monkeypatch
    ):
        """`resolve` hides a read or decryption failure as "empty"; inheriting
        Builder ID from an Identity Center entry that merely failed to read would
        launch the wrong sign-in. Only a vault proven empty answers `{}`."""
        from kiro_crew.auth import bridge
        from kiro_crew.auth.store import TokenStore
        from kiro_crew.cloud import login_target

        store = TokenStore(tmp_path)
        monkeypatch.setattr(bridge, "default_token_store", lambda: store)
        monkeypatch.setattr(store, "resolve", lambda: None)
        monkeypatch.setattr(store, "holds_no_identity", lambda: False)
        assert login_target.local_vault_identity() is None

        def _unreadable():
            raise OSError("secrets.enc unreadable")

        monkeypatch.setattr(store, "holds_no_identity", _unreadable)
        assert login_target.local_vault_identity() is None


# ---------------------------------------------------------------------------
# The scripts under bash, against stub programs, one HOME per route.
# ---------------------------------------------------------------------------

#: The stub's `kas route` line: a warning on stderr (as the real config loader
#: emits for a loose config.local.json) and the answer on stdout. Variants below
#: swap this one line, and assert that they did.
_STUB_ROUTE_LINE = (
    '  route) echo "config.local.json is group-writable; tightening is advised" >&2; '
    'echo "{route}"; exit 0;;'
)

_STUB_KIROCREW = (
    """#!/bin/bash
echo "kirocrew $*" >> "$HOME/calls.log"
case "$2" in
"""
    + _STUB_ROUTE_LINE
    + """
  whoami) if [ -f "$HOME/vault" ]; then
            if [ "$3" = "--format" ]; then echo '{{"accountType": "BuilderId", "identity": "builder_id"}}'; else echo "Signed in to Kiro Crew: BuilderId"; fi
            exit 0
          fi
          echo "Not signed in to Kiro Crew." >&2; exit 1;;
  logout) rm -f "$HOME/vault"; echo "Signed out of Kiro Crew."; exit 0;;
esac
exit 2
"""
)

_STUB_KIRO_CLI = """#!/bin/bash
echo "kiro-cli $*" >> "$HOME/calls.log"
case "$1" in
  whoami) if [ -f "$HOME/clistore" ]; then echo '{{"email":"x@y","accountType":"BuilderId"}}'; exit 0; fi
          echo "Not logged in"; exit 1;;
  logout) rm -f "$HOME/clistore"; exit 0;;
esac
exit 2
"""


_STUB_PKILL = """#!/bin/bash
echo "pkill $*" >> "$HOME/calls.log"
exit 1
"""

_STUB_PGREP = """#!/bin/bash
echo "pgrep $*" >> "$HOME/calls.log"
exit 1
"""


# The generated scripts run on a Linux instance: the login-directory guard needs
# POSIX mode bits (``mkdir -m 0700``, ``stat -c %a``) and the stubs need an
# executable bit, none of which Git Bash on Windows provides -- so the bash-backed
# tests below run only where a POSIX bash exists, like ``test_cloud_login_paths``.
_BASH = shutil.which("bash") if os.name == "posix" else None
_needs_posix_bash = pytest.mark.skipif(
    _BASH is None, reason="requires a POSIX bash to execute the remote scripts"
)


@pytest.fixture
def box(tmp_path):
    """A fake instance: ``$HOME`` with stub programs on a PATH of its own.

    The PATH puts the stub directory first, so the generated scripts resolve
    ``kirocrew``, ``kiro-cli``, ``pkill`` and ``pgrep`` to the stubs and nothing
    here touches a real process or a real store.
    """
    if _BASH is None:
        pytest.skip("requires a POSIX bash to execute the remote scripts")

    def make(
        route: str, *, vault: bool = False, clistore: bool = False, kiro_cli: bool = True
    ) -> dict[str, str]:
        home = tmp_path / f"home-{route or 'none'}"
        (home / "bin").mkdir(parents=True)
        (home / "bin" / "kirocrew").write_text(_STUB_KIROCREW.format(route=route))
        if kiro_cli:
            (home / "bin" / "kiro-cli").write_text(_STUB_KIRO_CLI)
        # The logout and cancel scripts run uid-scoped `pkill -f` / `pgrep -f` on the
        # box. Here the box is this machine, so both are shadowed by stubs that only
        # log the pattern they were given: the test must never reach a real sign-in
        # or agent process running under the developer's or the runner's uid.
        (home / "bin" / "pkill").write_text(_STUB_PKILL)
        (home / "bin" / "pgrep").write_text(_STUB_PGREP)
        for stub in ("kirocrew", "kiro-cli", "pkill", "pgrep"):
            if (home / "bin" / stub).exists():
                os.chmod(home / "bin" / stub, stat.S_IRWXU)  # owner-only, executable
        if vault:
            (home / "vault").write_text("x")
        if clistore:
            (home / "clistore").write_text("x")
        return {"HOME": str(home), "PATH": f"{home / 'bin'}:/usr/bin:/bin"}

    return make


def _bash(script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    # cwd is the fake box's HOME: the scripts and their stub children write
    # relative to nowhere in particular, and nothing of theirs may land in the
    # checkout.
    return subprocess.run(
        ["bash", "-c", script],
        cwd=env["HOME"],
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


def _calls(env: dict[str, str]) -> list[str]:
    path = Path(env["HOME"]) / "calls.log"
    return path.read_text().splitlines() if path.exists() else []


@_needs_posix_bash
@pytest.mark.parametrize("route", ["kas-bundle", "kiro-cli", ""])
def test_the_check_runs_the_route_owner_and_answers_with_the_shared_sentinels(box, route):
    env = box(route)
    out = _bash(login._login_check_command(), env)
    assert login._NOAUTH_SENTINEL in out.stdout and out.returncode == 1
    program = "kirocrew kas whoami" if route == "kas-bundle" else "kiro-cli whoami"
    assert program in _calls(env)
    other = "kiro-cli whoami" if route == "kas-bundle" else "kirocrew kas whoami"
    assert other not in _calls(env)


@_needs_posix_bash
def test_a_signed_in_vault_box_reads_as_signed_in(box):
    env = box("kas-bundle", vault=True)
    out = _bash(login._login_check_command(), env)
    assert login._TOKEN_PRESENT_SENTINEL in out.stdout and out.returncode == 0
    who = _bash(login._whoami_json_command(), env)
    assert who.returncode == 0
    from kiro_crew.cloud.login_target import parse_whoami_output

    assert parse_whoami_output(who.stdout) == {"account_type": "BuilderId"}


@_needs_posix_bash
def test_logout_on_the_vault_route_runs_the_vault_verb_and_the_box_reads_signed_out(box):
    env = box("kas-bundle", vault=True)
    out = _bash(login._logout_command(), env)
    assert out.returncode == 0
    assert "kirocrew kas logout" in _calls(env)
    assert "kiro-cli logout" not in _calls(env)
    # The kill reached the stub, scoped to this uid and the vault login's command line.
    kills = [c for c in _calls(env) if c.startswith("pkill ")]
    assert kills and all(login_vault.VAULT_LOGIN_PROCESS_PATTERN in k for k in kills)
    assert all("-u " in k for k in kills)
    assert login._NOAUTH_SENTINEL in _bash(login._login_check_command(), env).stdout


@_needs_posix_bash
def test_logout_on_the_kiro_cli_route_is_unchanged(box):
    env = box("kiro-cli", clistore=True)
    out = _bash(login._logout_command(), env)
    assert out.returncode == 0
    assert "kiro-cli logout" in _calls(env)
    assert "kirocrew kas logout" not in _calls(env)


@_needs_posix_bash
def test_the_callback_flow_refuses_on_a_vault_route_box(box):
    env = box("kas-bundle")
    out = _bash(login._callback_login_command(), env)
    assert login_vault.VAULT_NO_CALLBACK_SENTINEL in out.stdout
    assert out.returncode == 1
    assert not (Path(env["HOME"]) / ".kirocrew").exists()


_STUB_KIROCREW_LOGOUT_FAILS = _STUB_KIROCREW.replace(
    'logout) rm -f "$HOME/vault"; echo "Signed out of Kiro Crew."; exit 0;;',
    'logout) rm -f "$HOME/vault"; echo "sign-out did not complete" >&2; exit 1;;',
)


@_needs_posix_bash
def test_a_failed_vault_logout_is_reported_not_folded_into_exit_zero(box, monkeypatch):
    """`kiro-cli logout` exits non-zero when there was nothing to drop, so the kiro-cli
    branch ends `exit 0` and re-probes. The vault verb's non-zero means its fence was
    not written; that must reach the local side as a failed sign-out, even though the
    empty-vault probe would read clean."""
    env = box("kas-bundle", vault=True)
    stub = Path(env["HOME"]) / "bin" / "kirocrew"
    stub.write_text(_STUB_KIROCREW_LOGOUT_FAILS.format(route="kas-bundle"))
    out = _bash(login._logout_command(), env)
    assert out.returncode == 1
    assert login_vault.VAULT_LOGOUT_FAILED_SENTINEL in out.stdout
    # And the local reading: a Success status with the sentinel is a failed logout,
    # with no probe sent.
    sent: list[str] = []

    def _run(instance_id, command, *a, **k):
        sent.append(command)
        return ssm.CommandResult("Success", out.stdout, "", 0)

    monkeypatch.setattr(ssm, "run_command", _run)
    assert login.logout("i-0abc", "dev", "us-east-1") is False
    assert len(sent) == 1


@_needs_posix_bash
def test_a_kiro_cli_logout_still_ends_zero_and_reprobes(box, monkeypatch):
    env = box("kiro-cli")  # nothing stored: kiro-cli logout "fails", as it does
    out = _bash(login._logout_command(), env)
    assert out.returncode == 0
    assert login_vault.VAULT_LOGOUT_FAILED_SENTINEL not in out.stdout


# ---------------------------------------------------------------------------
# A box with NO sign-in program: kiro-cli absent and the route probe not kas-bundle.
# ---------------------------------------------------------------------------

#: A ``kirocrew`` whose ``kas route`` fails the way the real verb does on an
#: unreadable config: a plain message on stderr and exit 1.
_STUB_KIROCREW_ROUTE_FAILS = _STUB_KIROCREW.replace(
    _STUB_ROUTE_LINE,
    '  route) echo "could not read the Kiro Crew config under /srv/crew/.kiro/crew: '
    "ValueError: Expecting value. Which program owns the Kiro sign-in here is unknown; "
    'fix the config, then run this again." >&2; exit 1;;',
)
assert _STUB_KIROCREW_ROUTE_FAILS != _STUB_KIROCREW

#: A Kiro Crew too old to have the verb: argparse's exit 2, not the unknown exit.
_STUB_KIROCREW_NO_VERB = _STUB_KIROCREW.replace(
    _STUB_ROUTE_LINE,
    "  route) echo \"kirocrew kas: error: argument kas_action: invalid choice: 'route'\" >&2; exit 2;;",
)
assert _STUB_KIROCREW_NO_VERB != _STUB_KIROCREW


class TestNoSignInProgram:
    def test_every_script_refuses_before_running_either_program(self):
        for name, cmd in _SCRIPTS.items():
            if name == "cancel":
                continue
            refusal = cmd.index(login_vault.NO_SIGNIN_PROGRAM_SENTINEL)
            # The resolve lines and the route probe sit before the refusal by design;
            # what must come AFTER it is every invocation of either sign-in program.
            assert cmd.index(login_vault.KIROCREW_BIN_RESOLVE) < refusal, name
            assert cmd.index('"$KIROCREW" kas route') < refusal, name
            first_program = min(
                i
                for i in (cmd.find('"$KIRO" ', refusal), cmd.find('"$KIROCREW" kas ', refusal))
                if i != -1
            )
            assert refusal < first_program, name
            # And nothing but the probe invoked kirocrew before the refusal (the
            # refusal's own `command -v "$KIRO"` test is a lookup, not a verb).
            before = cmd[:refusal]
            assert before.count('"$KIROCREW" kas ') == 1, name
            assert '"$KIRO" whoami' not in before and '"$KIRO" login' not in before, name
            assert '"$KIRO" logout' not in before, name

    def test_the_refusal_names_the_cause_and_the_remedy(self):
        snippet = login_vault.require_sign_in_program()
        assert "no kiro-cli" in snippet
        assert "kirocrew kas route" in snippet
        assert "fix its Kiro Crew config.json" in snippet
        assert "install kiro-cli" in snippet
        # The probe's own output is shown, so the operator reads the real error.
        assert '"$KC_ROUTE_OUT"' in snippet

    @_needs_posix_bash
    def test_a_bundle_route_box_whose_route_probe_fails_reads_as_unknown(self, box, monkeypatch):
        """The box is on the bundle route but its config is unreadable: `kas route`
        exits 1, so the vault branch is not chosen -- and with no kiro-cli either,
        the script says so instead of `kiro-cli: command not found`."""
        env = box("kas-bundle", kiro_cli=False)
        stub = Path(env["HOME"]) / "bin" / "kirocrew"
        stub.write_text(_STUB_KIROCREW_ROUTE_FAILS.format(route="kas-bundle"))
        out = _bash(login._login_check_command(), env)
        assert out.returncode == 1
        assert login_vault.ROUTE_UNKNOWN_SENTINEL in out.stdout
        assert "could not read the Kiro Crew config" in out.stdout
        assert "nothing was changed on it" in out.stdout
        assert "command not found" not in out.stdout + out.stderr
        # Only the probe ran on the box: neither sign-in program was asked anything.
        assert _calls(env) == ["kirocrew kas route"]
        monkeypatch.setattr(ssm, "run_command", lambda *a, **k: _result(out.stdout, "Success", 1))
        # Unknown, not signed out: no sign-in is started, no verdict is given.
        assert login._auth_probe("i-0abc", "dev", "us-east-1") is None
        assert login.remote_identity("i-0abc", "dev", "us-east-1") is None

    @_needs_posix_bash
    def test_a_box_with_no_kirocrew_and_no_kiro_cli_reads_the_same_way(self, box):
        env = box("", kiro_cli=False)
        (Path(env["HOME"]) / "bin" / "kirocrew").unlink()
        out = _bash(login._whoami_json_command(), env)
        assert out.returncode == 1
        assert login_vault.NO_SIGNIN_PROGRAM_SENTINEL in out.stdout
        assert "not found" in out.stdout  # the shell's own word for the missing kirocrew

    @_needs_posix_bash
    def test_a_box_with_kiro_cli_is_untouched_by_the_check(self, box):
        env = box("")
        out = _bash(login._login_check_command(), env)
        assert login_vault.NO_SIGNIN_PROGRAM_SENTINEL not in out.stdout
        assert "kiro-cli whoami" in _calls(env)

    def test_the_local_side_shows_the_boxs_words_as_the_error(self, monkeypatch):
        stdout = (
            f"{login_vault.NO_SIGNIN_PROGRAM_SENTINEL}\n"
            "This instance has no kiro-cli, and its 'kirocrew kas route' did not answer kas-bundle (exit 1):\n"
            "  could not read the Kiro Crew config under /srv/crew/.kiro/crew: boom\n"
            "If this instance runs the KAS bundle route, fix its Kiro Crew config.json; otherwise install kiro-cli on it.\n"
        )
        monkeypatch.setattr(login, "is_logged_in", lambda *a, **k: False)
        monkeypatch.setattr(ssm, "run_command", lambda *a, **k: _result(stdout, "Success", 1))

        def _never(*a, **k):
            raise AssertionError("no fallback flow on a box with no sign-in program")

        monkeypatch.setattr(login, "_start_callback_login", _never)
        prompt = login.start_device_login("i-0abc", "dev", "us-east-1", open_browser=False)
        assert not prompt.actionable
        assert "no kiro-cli" in prompt.error
        assert "could not read the Kiro Crew config" in prompt.error
        assert "fix its Kiro Crew config.json" in prompt.error
        assert login_vault.NO_SIGNIN_PROGRAM_SENTINEL not in prompt.error

    @_needs_posix_bash
    def test_an_unknown_route_refuses_even_with_kiro_cli_installed(self, box):
        """GPT's case: vault identity + kiro-cli installed + config just broke. The
        probe exits 1; the script must NOT fall into the kiro-cli branch and sign
        the wrong store out while the vault credential stays usable."""
        env = box("kas-bundle", vault=True, clistore=True)
        stub = Path(env["HOME"]) / "bin" / "kirocrew"
        stub.write_text(_STUB_KIROCREW_ROUTE_FAILS.format(route="kas-bundle"))
        out = _bash(login._logout_command(), env)
        assert out.returncode == 1
        assert login_vault.ROUTE_UNKNOWN_SENTINEL in out.stdout
        assert "could not read the Kiro Crew config" in out.stdout
        # Neither store was touched.
        assert _calls(env) == ["kirocrew kas route"]
        assert (Path(env["HOME"]) / "vault").exists()
        assert (Path(env["HOME"]) / "clistore").exists()
        # The local side: logout reports failure, nothing reads as signed out.
        import kiro_crew.cloud.login as login_mod

        def _run(instance_id, command, *a, **k):
            return ssm.CommandResult("Success", out.stdout, "", 1)

        orig = ssm.run_command
        ssm.run_command = _run
        try:
            assert login_mod.logout("i-0abc", "dev", "us-east-1") is False
            assert login_mod._auth_probe("i-0abc", "dev", "us-east-1") is None
        finally:
            ssm.run_command = orig

    @_needs_posix_bash
    def test_a_kirocrew_without_the_verb_still_takes_the_kiro_cli_branch(self, box):
        """An older Kiro Crew answers argparse's exit 2 to `kas route`: that is "no
        route answer", and the box keeps its kiro-cli commands exactly as before."""
        env = box("", clistore=True)
        stub = Path(env["HOME"]) / "bin" / "kirocrew"
        stub.write_text(_STUB_KIROCREW_NO_VERB.format(route=""))
        out = _bash(login._login_check_command(), env)
        assert out.returncode == 0
        assert login._TOKEN_PRESENT_SENTINEL in out.stdout
        assert login_vault.ROUTE_UNKNOWN_SENTINEL not in out.stdout
        assert "kiro-cli whoami" in _calls(env)

    def test_the_callback_flow_reads_the_refusal_as_an_error_too(self, monkeypatch):
        stdout = f"{login_vault.NO_SIGNIN_PROGRAM_SENTINEL}\nThis instance has no kiro-cli.\n"
        monkeypatch.setattr(ssm, "run_command", lambda *a, **k: _result(stdout, "Success", 1))
        callback = login._start_callback_login("i-0abc", "dev", "us-east-1", open_browser=False)
        assert callback.error == "This instance has no kiro-cli."
        assert not callback.actionable and not callback.ports


@_needs_posix_bash
def test_a_warning_on_stderr_does_not_change_the_route(box):
    """The config loader warns on stderr about a loose config.local.json while the
    verb still answers on stdout; only stdout is compared."""
    env = box("kas-bundle", vault=True)
    out = _bash(login._login_check_command(), env)
    assert out.returncode == 0
    assert login._TOKEN_PRESENT_SENTINEL in out.stdout
    assert "kirocrew kas whoami" in _calls(env)
    assert login_vault.NO_SIGNIN_PROGRAM_SENTINEL not in out.stdout


def test_the_prelude_keeps_stderr_apart_from_the_compared_answer():
    prelude = login_vault.route_prelude()
    assert '2>"$KC_ROUTE_ERR_FILE"' in prelude
    assert "2>&1" not in prelude
    assert 'KC_ROUTE="$KC_ROUTE_OUT"' in prelude
