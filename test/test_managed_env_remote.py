"""ssh to a managed environment, its scripts, its ssh config block and the ProxyCommand."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from email.message import Message
from typing import Any

import pytest

from kiro_crew.managed_env import install, machine, mint_and_attach, remote, ssh_config
from kiro_crew.managed_env.settings import Cell
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

HOST = "crew-sbx-env-001"


class Runner:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[list, str]] = []

    def __call__(self, argv, stdin, timeout):
        self.calls.append((argv, stdin))
        rc, out, err = self.answers.pop(0)
        return subprocess.CompletedProcess(argv, rc, out, err)


# ---- remote ------------------------------------------------------------------------------


def test_the_script_goes_on_stdin_and_the_alias_is_checked():
    run = Runner((0, "ok", "Warning: Permanently added x"))
    res = remote.run_script(
        HOST, "echo hi", timeout=FALLBACK_TIMEOUT_SECS, env={"EXAMPLE": "a b"}, runner=run
    )
    argv, stdin = run.calls[0]
    assert argv[-2:] == [HOST, "sh -s"] and "echo hi" in stdin and "export EXAMPLE='a b'" in stdin
    assert res.ok and res.stderr == ""
    with pytest.raises(ValueError):
        remote.run_script("other-host", "x", timeout=FALLBACK_TIMEOUT_SECS, runner=run)
    with pytest.raises(ValueError):
        remote.run_script(HOST, "x", timeout=FALLBACK_TIMEOUT_SECS, env={"PATH": "/x"}, runner=run)


@pytest.mark.parametrize(
    "first, retry, calls",
    [
        ((255, "", ""), True, 2),  # a lone 255 on an idempotent script: once more
        ((255, "", "mint-and-attach: Stopped."), True, 1),  # a failed mint: never again
        ((255, "", ""), False, 1),
        ((1, "", ""), True, 1),
    ],
)
def test_a_lone_255_is_tried_once_more(first, retry, calls):
    run = Runner(first, (0, "done", ""))
    remote.run_script(HOST, "x", timeout=FALLBACK_TIMEOUT_SECS, runner=run, retry=retry)
    assert len(run.calls) == calls


def test_a_transport_failure_is_a_255_result():
    def boom(argv, stdin, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    res = remote.run_script(HOST, "x", timeout=FALLBACK_TIMEOUT_SECS, runner=boom)
    assert res.returncode == 255 and res.timed_out


def test_the_alias_round_trips():
    assert remote.sandbox_id_from_host(remote.ssh_host_for("env-001")) == "env-001"
    assert remote.sandbox_id_from_host("crew-sbx-../x") == ""
    with pytest.raises(ValueError):
        remote.ssh_host_for("a b")


# ---- install, hook, machine mode ----------------------------------------------------------


def test_the_hook_clears_credentials_before_it_starts_anything():
    text = install.hook_text(["EXAMPLE_TOKEN"])
    lines = text.splitlines()
    first = next(i for i, ln in enumerate(lines) if ln and not ln.startswith("#"))
    assert lines[first].startswith("unset ") and "AWS_SECRET_ACCESS_KEY" in lines[first]
    assert "EXAMPLE_TOKEN" in lines[first]
    assert text.index(machine.ROUTE_CHECK) < text.index("setsid nohup")
    with pytest.raises(install.InstallError):
        install.hook_text(["BAD NAME; rm -rf /"])


@pytest.mark.skipif(sys.platform == "win32", reason="runs the hook under sh")
def test_the_hook_starts_nothing_off_machine_mode(tmp_path):
    # A relative root, so a temp dir with a space never lands in the hook's unquoted paths.
    text = install.hook_text().replace(remote.ROOT, ".")
    (tmp_path / "hook").write_text(text, encoding="utf-8")
    res = subprocess.run(
        ["sh", "hook"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert res.returncode == install.LAUNCHER_FAILED_EXIT
    assert install.hook_state(res.returncode, res.stdout) == "failed"


def test_hook_states():
    for word in ("started", "alive", "not_machine", "needs_signin"):
        assert install.hook_state(0, f"{install.HOOK_NAME}: {word}\n") == word
    assert install.hook_state(1, "") == "failed"


def test_machine_mode_turns_on_last_and_quotes_every_value():
    cell = Cell("https://a.example", "https://b.example", "ex-test-1", "/t", ("A", "B"))
    keys = [k for k, _ in machine.config_args(cell)]
    assert keys[0] == "dashboard.feature_previews.crew_client" and keys[-1] == "agent.kas_auth"
    assert machine.config_script(cell).rstrip().endswith(f"echo '{machine.CONFIG_DONE}'")
    assert machine.account_type('noise\n{"accountType": "machine"}\n') == "machine"
    assert machine.account_type("not json") == ""


def test_the_install_script_uses_the_public_installer_on_projects():
    script = install.install_script()
    assert install.PUBLIC_URL in script and f"KIROCREW_HOME={install.CREW_HOME}" in script


# ---- ssh config ---------------------------------------------------------------------------


def test_the_ssh_block_is_idempotent_and_keeps_the_rest(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("Host other\n  User me\n", encoding="utf-8")
    assert ssh_config.ensure_block(cfg, sys.executable) is True
    assert ssh_config.ensure_block(cfg, sys.executable) is False
    text = cfg.read_text(encoding="utf-8")
    assert text.endswith("Host other\n  User me\n") and text.count(ssh_config.BEGIN) == 1


def test_the_ssh_block_escapes_interpreter_percent_tokens():
    block = ssh_config.render_block("/opt/crew%/bin/python")
    proxy = next(line for line in block.splitlines() if line.startswith("  ProxyCommand "))
    assert "/opt/crew%%/bin/python" in proxy
    assert proxy.endswith(" %n")


def test_a_percent_interpreter_block_is_current_and_idempotent(tmp_path):
    cfg = tmp_path / "config"
    python = "/opt/crew%/bin/python"
    assert ssh_config.ensure_block(cfg, python)
    assert "/opt/crew%%/bin/python" in cfg.read_text(encoding="utf-8")
    assert ssh_config.block_current(cfg, python)
    assert not ssh_config.ensure_block(cfg, python)


def test_a_damaged_ssh_block_is_refused(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text(ssh_config.BEGIN + "\n", encoding="utf-8")
    with pytest.raises(ssh_config.SshConfigError):
        ssh_config.ensure_block(cfg, sys.executable)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_a_new_ssh_config_is_0600(tmp_path):
    cfg = tmp_path / "ssh" / "config"
    ssh_config.ensure_block(cfg, sys.executable)
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o600


# ---- the ProxyCommand ----------------------------------------------------------------------


def test_the_proxy_command_asks_only_a_loopback_dashboard():
    with pytest.raises(mint_and_attach.MintError):
        mint_and_attach.mint_tuple("env-001", {"KIROCREW_SBX_DASHBOARD": "http://example.com:5476"})
    with pytest.raises(mint_and_attach.MintError):
        mint_and_attach.mint_tuple("env-001", {})


def test_the_plugin_gets_the_session_in_its_env_and_no_dashboard_url(monkeypatch):
    monkeypatch.setenv("KIROCREW_SBX_DASHBOARD", "http://127.0.0.1:5476")
    tup = {"ssmSessionId": "s", "streamUrl": "wss://x", "tokenValue": "tv"}
    argv, env = mint_and_attach.plugin_env_and_argv(tup, "plugin", "ex-test-1")
    assert argv == ["plugin", mint_and_attach.PLUGIN_ENV_VAR, "ex-test-1", "StartSession"]
    assert "tv" not in " ".join(argv) and "KIROCREW_SBX_DASHBOARD" not in env
    assert json.loads(env[mint_and_attach.PLUGIN_ENV_VAR])["TokenValue"] == "tv"


def test_ssh_config_refuses_a_relative_python_and_markers_out_of_order(tmp_path):
    with pytest.raises(ssh_config.SshConfigError):
        ssh_config.render_block("python3")
    cfg = tmp_path / "config"
    cfg.write_text(f"{ssh_config.END}\n{ssh_config.BEGIN}\n", encoding="utf-8")
    with pytest.raises(ssh_config.SshConfigError):
        ssh_config.ensure_block(cfg, sys.executable)


@pytest.mark.parametrize(
    "begin, end",
    [
        (f"{ssh_config.BEGIN}\n", f"{ssh_config.END} (installed by Crew)\n"),
        (f"{ssh_config.BEGIN} note\n", f"{ssh_config.END}\n"),
        (f"x {ssh_config.BEGIN}\n", f"{ssh_config.END}\n"),
        (f"{ssh_config.BEGIN}\n", f"  {ssh_config.END}\n"),
    ],
)
def test_a_marker_with_text_beside_it_is_refused_and_the_file_left_alone(tmp_path, begin, end):
    cfg = tmp_path / "config"
    text = "Host work\n  User me\n\n" + begin + "Host crew-sbx-*\n" + end + "Host *\n  Port 22\n"
    cfg.write_text(text, encoding="utf-8")
    with pytest.raises(ssh_config.SshConfigError):
        ssh_config.ensure_block(cfg, sys.executable)
    assert cfg.read_text(encoding="utf-8") == text
    assert not ssh_config.block_current(cfg, sys.executable)


def test_moving_an_old_block_up_keeps_the_lines_after_it_global(tmp_path):
    cfg = tmp_path / "config"
    old = ssh_config.render_block("/x/python")
    cfg.write_text("Host work\n  User me\n\n" + old + "ServerAliveInterval 47\n", encoding="utf-8")
    ssh_config.ensure_block(cfg, sys.executable)
    text = cfg.read_text(encoding="utf-8")
    assert text.startswith(ssh_config.BEGIN)
    # the user's global line still follows a Host *, not Host work
    tail = text.split(ssh_config.END, 1)[1]
    assert "Host work\n  User me\n\nHost *\nServerAliveInterval 47\n" in tail
    # a block that was first in the file needs no extra boundary
    cfg.write_text(old + "ServerAliveInterval 47\n", encoding="utf-8")
    ssh_config.ensure_block(cfg, sys.executable)
    assert cfg.read_text(encoding="utf-8").count("Host *") == 1


def test_a_crlf_config_keeps_its_block_replaceable(tmp_path):
    cfg = tmp_path / "config"
    old = ssh_config.render_block("/x/python").replace("\n", "\r\n")
    cfg.write_bytes(("Host work\r\n  User me\r\n\r\n" + old + "Host *\r\n  Port 22\r\n").encode())
    assert ssh_config.ensure_block(cfg, sys.executable)
    text = cfg.read_bytes().decode()
    assert text.count(ssh_config.BEGIN) == 1 and "/x/python" not in text
    assert text.endswith("Host *\r\n  Port 22\r\n") and "Host work\r\n  User me\r\n" in text


# ---- the ProxyCommand, against a fake dashboard --------------------------------------------


class _Resp:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, answer):
        self.answer = answer
        self.urls: list[str] = []

    def open(self, req, timeout):
        self.urls.append(req.full_url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return _Resp(json.dumps(self.answer).encode())


ENV = {"KIROCREW_BOUND_PORT": "5476"}
TUPLE = {"ssmSessionId": "s", "streamUrl": "wss://x", "tokenValue": "tv", "region": "ex-test-1"}


def _http_error(code: int, body: dict):
    import io
    import urllib.error

    return urllib.error.HTTPError("u", code, "x", Message(), io.BytesIO(json.dumps(body).encode()))


def test_the_proxy_command_mints_through_the_loopback_route():
    opener = _Opener(TUPLE)
    got = mint_and_attach.mint_tuple("env-001", ENV, opener=opener, token_source=lambda b, e: "own")
    assert got == TUPLE
    assert opener.urls[0].startswith(
        "http://127.0.0.1:5476/api/managed-environments/env-001/ssh-session"
    )


@pytest.mark.parametrize(
    "answer, text",
    [
        (_http_error(409, {"code": "not_ready"}), mint_and_attach.TEXT_STOPPED),
        (_http_error(409, {"code": "not_signed_in"}), mint_and_attach.TEXT_SIGN_IN),
        (_http_error(409, {"code": "not_configured", "error": "set x"}), "set x"),
        (_http_error(403, {}), "HTTP 403"),
        (_http_error(502, {"error": "down"}), "HTTP 502: down"),
        (OSError("refused"), mint_and_attach.TEXT_NO_DASHBOARD),
        ({"ssmSessionId": "s"}, "no ssh tuple"),
    ],
)
def test_each_failure_is_one_plain_line(answer, text):
    with pytest.raises(mint_and_attach.MintError) as ei:
        mint_and_attach.mint_tuple(
            "env-001", ENV, opener=_Opener(answer), token_source=lambda b, e: "own"
        )
    assert text in str(ei.value) and "tv" not in str(ei.value)


def test_the_session_post_goes_only_to_a_proven_gateway(monkeypatch):
    """The owner token rides only a connection whose peer is proven to be this gateway."""
    from kiro_crew import cli_server

    seen: dict[str, object] = {}

    def proven(req, port, timeout, *, operation):
        seen.update(port=port, operation=operation, url=req.full_url)
        return _Resp(json.dumps(TUPLE).encode())

    monkeypatch.setattr(cli_server, "_owner_verified_secret_urlopen", proven)
    assert mint_and_attach.mint_tuple("env-001", ENV, token_source=lambda b, e: "own") == TUPLE
    assert seen["port"] == 5476 and seen["operation"] == "managed_env_ssh_session"

    def refused(req, port, timeout, *, operation):
        raise cli_server._GatewayPeerRefused("foreign listener")

    monkeypatch.setattr(cli_server, "_owner_verified_secret_urlopen", refused)
    with pytest.raises(mint_and_attach.MintError) as ei:
        mint_and_attach.mint_tuple("env-001", ENV, token_source=lambda b, e: "own")
    assert str(ei.value) == mint_and_attach.TEXT_NOT_THIS_GATEWAY


def test_the_owner_token_comes_from_kirocrew_token():
    def run(argv, **kw):
        assert argv[1:5] == ["-P", "-m", "kiro_crew", "token"]
        assert argv[argv.index("--ttl") + 1] == "1m"  # a short-lived link, never 20h
        return subprocess.CompletedProcess(argv, 0, "http://127.0.0.1:5476/?token=abc\n", "")

    assert mint_and_attach.mint_owner_token("http://127.0.0.1:5476", {}, run=run) == "abc"

    def fails(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "")

    with pytest.raises(mint_and_attach.MintError):
        mint_and_attach.mint_owner_token("http://127.0.0.1:5476", {}, run=fails)
    with pytest.raises(mint_and_attach.MintError):
        mint_and_attach.mint_owner_token("http://127.0.0.1", {}, run=run)


def test_main_execs_the_plugin_with_the_session(monkeypatch):
    monkeypatch.setattr(mint_and_attach, "mint_tuple", lambda sid: TUPLE)
    # the core's resolver, so a minimal launchd PATH still finds the plugin
    monkeypatch.setattr(
        "kiro_crew.deploy.engine.resolve_aws_tool_bin", lambda name: f"/opt/aws/{name}"
    )
    seen: dict[str, Any] = {}

    def execvpe(file, args, env):
        seen.update(file=file, args=args, env=env)

    mint_and_attach.main(["x", "crew-sbx-env-001"], execvpe=execvpe)
    assert seen["file"] == "/opt/aws/session-manager-plugin"
    assert seen["args"][2] == "ex-test-1"
    with pytest.raises(SystemExit) as ei:
        mint_and_attach.main(["x", "not-an-alias"], execvpe=execvpe)
    assert ei.value.code == 255


def test_the_ssh_block_goes_first_so_a_wildcard_default_cannot_override_it(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("Host *\n  ProxyJump bastion\n", encoding="utf-8")
    ssh_config.ensure_block(cfg, sys.executable)
    text = cfg.read_text(encoding="utf-8")
    assert text.startswith(ssh_config.BEGIN) and text.endswith("Host *\n  ProxyJump bastion\n")
    assert ssh_config.block_current(cfg, sys.executable)
    # an older block below the wildcard moves to the top too
    cfg.write_text(
        "Host *\n  ProxyJump bastion\n\n" + ssh_config.render_block("/x/python"), encoding="utf-8"
    )
    ssh_config.ensure_block(cfg, sys.executable)
    assert cfg.read_text(encoding="utf-8").startswith(ssh_config.BEGIN)
    assert not ssh_config.block_current(tmp_path / "none", sys.executable)


def test_a_block_for_another_interpreter_is_not_current(tmp_path):
    """An update can prune the tree a block names; the block then reads as missing."""
    cfg = tmp_path / "config"
    ssh_config.ensure_block(cfg, "/old/tree/bin/python")
    assert ssh_config.block_current(cfg, "/old/tree/bin/python")
    assert not ssh_config.block_current(cfg, "/new/tree/bin/python")


def test_a_managed_venv_names_its_stable_link(tmp_path, monkeypatch):
    from kiro_crew.platform import wheel_engine

    legacy = tmp_path / "crew-venv"
    layout = wheel_engine.ManagedVenvLayout(
        legacy=legacy, stable_link=tmp_path / "crew-venv-current"
    )
    (tmp_path / "crew-venv-current" / "bin").mkdir(parents=True)
    (tmp_path / "crew-venv-current" / "bin" / "python").write_text("", encoding="utf-8")
    monkeypatch.setattr(wheel_engine, "managed_venv_layout", lambda: layout)
    monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda layout=None: True)
    assert ssh_config.gateway_python() == str(tmp_path / "crew-venv-current" / "bin" / "python")
    monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda layout=None: False)
    assert ssh_config.gateway_python() == sys.executable  # pipx, a dev venv, a system Python


def test_the_installer_may_take_either_of_its_branches():
    script = install.install_script()
    assert (
        f"PIPX_HOME={install.PIPX_HOME}" in script and f"PIPX_BIN_DIR={install.PIPX_BIN}" in script
    )
    launcher = install.launcher_text()
    assert install.CREW_VENV in launcher and install.PIPX_HOME in launcher
    assert f"export PIPX_HOME={install.PIPX_HOME}\n" in launcher
    assert f"export PIPX_BIN_DIR={install.PIPX_BIN}\n" in launcher
    # an in-app update repoints the -current link; the original venv stays behind
    order = launcher.split("for bin in ", 1)[1].split(";", 1)[0].split()
    assert order[0] == f"{install.CREW_VENV}-current/bin/kirocrew"
    assert order[1] == f"{install.CREW_VENV}/bin/kirocrew"


@pytest.mark.skipif(sys.platform == "win32", reason="runs the launcher under sh")
def test_the_launcher_exports_the_installers_pipx_paths(tmp_path):
    binary = tmp_path / ".crew" / "pipx" / "venvs" / "kirocrew" / "bin" / "kirocrew"
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "$PIPX_HOME" "$PIPX_BIN_DIR"\n', encoding="utf-8")
    binary.chmod(0o755)
    launcher = install.launcher_text().replace(remote.ROOT, ".")
    res = subprocess.run(
        ["/bin/sh", "-c", launcher],
        cwd=tmp_path,
        env={"PIPX_HOME": "wrong-home", "PIPX_BIN_DIR": "wrong-bin"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines() == [
        install.PIPX_HOME.replace(remote.ROOT, "."),
        install.PIPX_BIN.replace(remote.ROOT, "."),
    ]


def test_global_lines_that_began_the_file_keep_applying_to_every_host(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("User example-deploy\nServerAliveInterval 47\n", encoding="utf-8")
    ssh_config.ensure_block(cfg, sys.executable)
    lines = cfg.read_text(encoding="utf-8").splitlines()
    end = lines.index(ssh_config.END)
    assert lines[end - 1] == "Host *" and lines[end + 2] == "User example-deploy"


@pytest.mark.skipif(sys.platform == "win32", reason="runs the hook under sh")
@pytest.mark.parametrize(
    "identity, returncode",
    [
        ('{"accountType":"personal"}', 0),
        ('{"accountType" : "user"}', 0),
        ("{}", 0),
        ("not json", 0),
        ('{"accountType":"machine"}', 1),
    ],
)
def test_the_hook_starts_nothing_without_a_successful_machine_identity(
    tmp_path, identity, returncode
):
    import shlex

    wrapper = tmp_path / ".crew" / "bin" / "kirocrew"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  --version) exit 0 ;;\n"
        "  'kas route') echo kas-bundle ;;\n"
        "  'kas whoami'|'kas whoami --format json')\n"
        f"    printf '%s\\n' {shlex.quote(identity)}; exit {returncode} ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    (tmp_path / "hook").write_text(install.hook_text().replace(remote.ROOT, "."), encoding="utf-8")
    res = subprocess.run(
        ["/bin/sh", "hook"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    assert res.returncode == install.NEEDS_SIGNIN_EXIT
    assert install.hook_state(res.returncode, res.stdout) == "needs_signin"
    assert not (tmp_path / ".sandbox" / "run" / f"{install.HOOK_NAME}.pid").exists()
    assert not (tmp_path / ".sandbox" / "gateway.log").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="runs the machine gate under sh")
@pytest.mark.parametrize("identity", ['{"accountType":"machine"}', '{"accountType" : "machine"}'])
def test_the_machine_identity_gate_accepts_json_spacing(tmp_path, identity):
    import shlex

    wrapper = tmp_path / "kirocrew"
    wrapper.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(identity)}\n", encoding="utf-8")
    wrapper.chmod(0o755)
    res = subprocess.run(
        ["/bin/sh", "-c", machine.WHOAMI_CHECK.replace(remote.WRAPPER, "./kirocrew")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    assert res.returncode == 0 and res.stdout == ""


def test_a_launch_probe_loop_keeps_the_owners_pending_link(monkeypatch):
    """Every ssh run mints one link; at ``MINT_TTL`` a whole health probe leaves few live
    nonces, so an unopened owner link minted before the launch is never pushed out."""
    from kiro_crew.dashboard import token_auth
    from kiro_crew.dashboard import token_auth_tokens as tat
    from kiro_crew.managed_env import remote

    state = tat.TokenStateManager(max_concurrent_nonces=tat.MAX_CONCURRENT_NONCES)
    monkeypatch.setattr(token_auth, "_state", state)  # the facade patches every holder
    from kiro_crew.testing.clock import ManualClock

    clock = ManualClock()
    clock.install(monkeypatch, tat)
    tat.generate_token("owner", tat.MAX_SESSION_TTL_SECS)  # e.g. a pending Slack link
    pending = next(iter(state._nonces))
    ttl = tat.parse_duration(mint_and_attach.MINT_TTL)
    peak = 0
    for _ in range(remote.PROBE_UP_TO_SECS // remote.PROBE_EVERY_SECS):
        tat.generate_token("owner", ttl)
        peak = max(peak, len(state._nonces))
        clock.advance(remote.PROBE_EVERY_SECS)
    assert pending in state._nonces
    assert peak <= ttl // remote.PROBE_EVERY_SECS + 2 < tat.MAX_CONCURRENT_NONCES
