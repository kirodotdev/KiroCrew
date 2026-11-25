"""``route_is_not_self``: a host with no DNS record is not this machine only when
ssh's effective config proves it, and ``ssh -G`` never runs a config's commands.

``ssh -G`` itself is stubbed: each case hands back the effective config ssh would
print, so no real ssh, network or user config is touched.
"""

from __future__ import annotations

import glob as globlib
import os
import shutil
import socket
import subprocess
import tempfile

import pytest

from kiro_crew import platform_compat, sandbox, sandbox_spawn, sandbox_tiers
from kiro_crew.security import argv_floor_hosts, ssh_config_route

HOST = "proxy-only.example"


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Empty user and system config dirs, so the scan reads only what a test writes."""
    user = tmp_path / "user"
    system = tmp_path / "system"
    user.mkdir()
    system.mkdir()
    monkeypatch.setattr(
        ssh_config_route,
        "_config_roots",
        lambda: [(str(user / "config"), str(user)), (str(system / "ssh_config"), str(system))],
    )
    monkeypatch.setattr(argv_floor_hosts, "_own_host_names", lambda: frozenset({"203.0.113.55"}))
    # The own-address table counts as read and the kernel says no test IP is
    # ours, so an IP literal hop is judged the same whatever ran earlier.
    monkeypatch.setattr(argv_floor_hosts, "_NETLINK_ADDRS_PUBLISHED", True)
    monkeypatch.setattr(argv_floor_hosts, "_kernel_says_own_address", lambda *_a, **_k: False)
    # A hop's own lookup: these names resolve, every other name does not exist.
    dns = {"jump.example": "198.51.100.7", "loopalias.example": "127.0.0.1"}

    def _gai(name, *_args, **_kwargs):
        if name not in dns:
            raise socket.gaierror(socket.EAI_NONAME, "no such name")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (dns[name], 0))]

    monkeypatch.setattr(ssh_config_route.socket, "getaddrinfo", _gai)
    return user


@pytest.fixture
def ssh_g(monkeypatch):
    """Stub sandbox reads, globs and ssh -G; reject any bare gateway subprocess.

    Hidden paths answer missing/unreadable. Calls, snapshots and modes record
    exactly what the sandbox wrapper handed the runner. ``on_run`` can change
    config after the scan to test that ssh reads only the snapshot.
    """

    class _Stub:
        stdout = ""
        returncode = 0
        backend = "namespace"
        mode = "auto"
        on_run = None
        hidden: "dict[str, str]" = {}
        calls: "list[dict]" = []
        wraps: "list[dict]" = []
        snapshots: "list[str]" = []
        bare: "list[object]" = []
        reads: "list[str]" = []
        globs: "list[str]" = []
        read_modes: "list[str]" = []

    stub = _Stub()
    stub.hidden, stub.calls, stub.wraps, stub.snapshots, stub.bare = {}, [], [], [], []
    stub.reads, stub.globs, stub.read_modes = [], [], []

    def _hidden(path):
        for root, how in stub.hidden.items():
            if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
                return how
        return None

    def _wrap(argv, mode, *, env=None, **_kwargs):
        if os.path.basename(argv[0]) == "ssh":
            stub.wraps.append({"argv": list(argv), "mode": mode, "env": dict(env or {})})
        else:
            stub.read_modes.append(mode)
        return ["sandbox-wrap", *argv], dict(env or {}), None

    def _sandboxed_sh(inner):
        script, args = inner[2], inner[4:]
        if script == ssh_config_route._GLOB_SCRIPT:
            stub.globs.append(args[0])
            found = [p for p in globlib.glob(args[0]) if _hidden(p) != "missing"]
            return 0, b"".join(p.encode() + b"\0" for p in found)
        assert script == ssh_config_route._READ_SCRIPT, script
        path = args[0]
        stub.reads.append(path)
        how = _hidden(path)
        if how == "unreadable":
            return 1, b""
        if how == "missing" or not os.path.exists(path):
            return ssh_config_route._READ_MISSING, b""
        if os.path.isdir(path):
            return 1, b""
        with open(path, "rb") as fh:
            return 0, fh.read(int(args[1]))

    def _run(argv, **kwargs):
        assert argv[0] == "sandbox-wrap", argv
        inner = argv[1:]
        if os.path.basename(inner[0]) == "sh":
            assert kwargs["stdin"] is subprocess.DEVNULL
            assert kwargs.get("input") is None
            code, out = _sandboxed_sh(inner)
            return subprocess.CompletedProcess(inner, code, out, b"")
        if stub.on_run is not None:
            stub.on_run()
        stub.snapshots.append(kwargs["input"].decode("utf-8"))
        stub.calls.append({"argv": inner, **kwargs})
        return subprocess.CompletedProcess(inner, stub.returncode, stub.stdout.encode(), b"")

    def _bare(*args, **_kwargs):
        stub.bare.append(args)
        raise AssertionError("ssh -G spawned outside the sandbox")

    def _gateway_open(*_args, **_kwargs):
        raise AssertionError("the gateway read a config file outside the sandbox")

    monkeypatch.setattr(ssh_config_route, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sandbox_spawn, "sandboxed_spawn_argv", _wrap)
    # Names more than one sandbox module binds are patched through the facade.
    monkeypatch.setattr(sandbox, "run_limited", _run)
    monkeypatch.setattr(sandbox_tiers, "configured_sandbox_mode", lambda: stub.mode)
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda mode: mode)
    monkeypatch.setattr(sandbox, "detect_backend", lambda _mode="auto": stub.backend)
    monkeypatch.setattr(ssh_config_route.subprocess, "run", _bare)
    monkeypatch.setattr(ssh_config_route.subprocess, "Popen", _bare)
    monkeypatch.setattr(ssh_config_route, "open", _gateway_open, raising=False)
    return stub


def _effective(hostname="198.51.100.9", proxycommand=None, proxyjump=None) -> str:
    lines = ["user someone", f"hostname {hostname}", "port 22"]
    if proxycommand is not None:
        lines.append(f"proxycommand {proxycommand}")
    if proxyjump is not None:
        lines.append(f"proxyjump {proxyjump}")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("program", ["ssh", "sh", "head"])
def test_route_proof_uses_trusted_executables(config_dir, ssh_g, monkeypatch, program):
    (config_dir / "config").write_text("Include *.conf\n", encoding="utf-8")
    (config_dir / "extra.conf").write_text("User someone\n", encoding="utf-8")
    ssh_g.stdout = _effective()
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(shutil, "which", lambda name: f"/home/x/.local/bin/{name}")
    wrapped = []
    wrap = sandbox_spawn.sandboxed_spawn_argv

    def _record(argv, *args, **kwargs):
        wrapped.append(list(argv))
        return wrap(argv, *args, **kwargs)

    monkeypatch.setattr(sandbox_spawn, "sandboxed_spawn_argv", _record)
    assert ssh_config_route.route_is_not_self(HOST) is True
    if program == "ssh":
        assert ssh_g.calls[0]["argv"][0] == "/usr/bin/ssh"
    else:
        shell_calls = [argv for argv in wrapped if argv[1] == "-c"]
        assert {argv[2] for argv in shell_calls} == {
            ssh_config_route._READ_SCRIPT,
            ssh_config_route._GLOB_SCRIPT,
        }
        if program == "sh":
            assert all(argv[0] == "/usr/bin/sh" for argv in shell_calls)
        else:
            reads = [argv for argv in shell_calls if argv[2] == ssh_config_route._READ_SCRIPT]
            assert all(argv[6:] == ["/usr/bin/head"] for argv in reads)
            assert 'exec "$3" -c "$2" -- "$1"' in ssh_config_route._READ_SCRIPT


@pytest.mark.parametrize("missing", ["ssh", "sh", "head"])
def test_missing_trusted_executable_refuses_without_spawn(config_dir, ssh_g, monkeypatch, missing):
    ssh_g.stdout = _effective()
    monkeypatch.setattr(
        platform_compat,
        "trusted_system_bin",
        lambda name: None if name == missing else f"/usr/bin/{name}",
    )
    assert ssh_config_route.route_is_not_self(HOST) is False
    assert ssh_g.calls == []
    assert ssh_g.read_modes == []
    assert ssh_g.bare == []


def test_a_proxy_only_route_elsewhere_is_cleared(config_dir, ssh_g):
    ssh_g.stdout = _effective(proxycommand="ssh -W %h:%p jump.example", proxyjump="none")
    assert ssh_config_route.route_is_not_self(HOST) is True
    (wrap,) = ssh_g.wraps
    (call,) = ssh_g.calls
    # The probe runs only as the argv the sandbox wrapper returned, at the agent's tier.
    assert call["argv"] == wrap["argv"]
    assert wrap["mode"] == "auto"
    assert wrap["argv"] == ["/usr/bin/ssh", "-G", "-F", "/dev/stdin", "--", HOST]
    assert call["stdin"] is None
    assert call["input"] == b"\n"
    assert call["timeout"] == 5.0
    assert "SSH_AUTH_SOCK" not in wrap["env"]
    # Never a bare subprocess.run / Popen of ssh beside the sandboxed run.
    assert ssh_g.bare == []


@pytest.mark.parametrize(
    "effective",
    [
        pytest.param(_effective(proxyjump="me@jump.example:2222"), id="proxyjump-resolves-remote"),
        pytest.param(_effective(proxyjump="198.51.100.9"), id="proxyjump-remote-ip"),
        pytest.param(_effective(hostname="jump.example"), id="hostname-resolves-remote"),
        pytest.param(_effective(), id="hostname-remote-ip"),
        pytest.param(_effective(proxycommand="ssh -W %h:%p jump.example"), id="ssh-w-name"),
        pytest.param(_effective(proxycommand="ssh 198.51.100.9 -W %h:%p"), id="ssh-w-ip"),
        pytest.param(
            _effective(proxycommand="ssh -q -W %h:%p u@jump.example"), id="ssh-w-quiet-user"
        ),
    ],
)
def test_a_proxy_hop_proven_remote_is_cleared(config_dir, ssh_g, effective):
    (config_dir / "config").write_text("Host other.example\n  HostName localhost\n")
    ssh_g.stdout = effective
    assert ssh_config_route.route_is_not_self(HOST) is True


@pytest.mark.parametrize("form", ["proxyjump", "proxycommand"])
@pytest.mark.parametrize("hostname", ["203.0.113.55", "own.example"])
@pytest.mark.parametrize("guard", ["own-set", "kernel"])
def test_proxy_final_hostname_cannot_resolve_to_self(
    config_dir, ssh_g, monkeypatch, form, hostname, guard
):
    own = "203.0.113.55"
    monkeypatch.setattr(
        ssh_config_route.socket,
        "getaddrinfo",
        lambda *_a, **_k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in ("198.51.100.9", own)
        ],
    )
    if guard == "kernel":
        monkeypatch.setattr(argv_floor_hosts, "_own_host_names", frozenset)
        monkeypatch.setattr(
            argv_floor_hosts, "_kernel_says_own_address", lambda ip, *_a: str(ip) == own
        )
    value = "198.51.100.7" if form == "proxyjump" else "ssh -W %h:%p 198.51.100.7"
    ssh_g.stdout = _effective(hostname=hostname, **{form: value})
    assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize("form", ["hostname", "proxyjump", "proxycommand"])
@pytest.mark.parametrize("name", ["crew-host", "Crew-Host"])
@pytest.mark.parametrize("local_dns", [None, "198.51.100.8"], ids=["unresolved", "resolves-remote"])
def test_a_known_own_name_that_does_not_resolve_here_refuses(
    config_dir, ssh_g, monkeypatch, form, name, local_dns
):
    # Split DNS: this machine's own name has no local answer, or a remote one.
    monkeypatch.setattr(
        argv_floor_hosts, "_own_host_names", lambda: frozenset({"203.0.113.55", "crew-host"})
    )
    if local_dns:
        monkeypatch.setattr(
            ssh_config_route.socket,
            "getaddrinfo",
            lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (local_dns, 0))],
        )
    value = "198.51.100.7" if form == "proxyjump" else "ssh -W %h:%p 198.51.100.7"
    proxy = {} if form == "hostname" else {form: value}
    ssh_g.stdout = _effective(hostname=name, **proxy)
    assert ssh_config_route.route_is_not_self(HOST) is False
    if form != "hostname":
        # The same own name as the hop is refused too.
        hop = name if form == "proxyjump" else f"ssh -W %h:%p {name}"
        ssh_g.stdout = _effective(hostname="198.51.100.9", **{form: hop})
        assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize("form", ["hostname", "proxyjump", "proxycommand"])
@pytest.mark.parametrize(
    "hostname", ["$(id)", "`id`", "a;b", "a b", "-oProxyCommand=x", "%h", "a|b"]
)
def test_final_hostname_requires_a_literal(config_dir, ssh_g, form, hostname):
    value = "198.51.100.9" if form == "proxyjump" else "ssh -W %h:%p 198.51.100.9"
    proxy = {} if form == "hostname" else {form: value}
    ssh_g.stdout = _effective(hostname=hostname, **proxy)
    assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize("form", ["proxyjump", "proxycommand"])
@pytest.mark.parametrize(
    "hostname", [HOST, "proxy-only-final.example", "jump.example", "198.51.100.9"]
)
def test_proxy_final_hostname_accepts_remote_or_proxy_only_names(config_dir, ssh_g, form, hostname):
    value = "198.51.100.7" if form == "proxyjump" else "ssh -W %h:%p 198.51.100.7"
    (config_dir / "config").write_text(f"Host {hostname}\n  {form} {value}\n", encoding="utf-8")
    ssh_g.stdout = _effective(hostname=hostname, **{form: value})
    assert ssh_config_route.route_is_not_self(HOST) is True


@pytest.mark.parametrize("form", ["proxyjump", "proxycommand"])
@pytest.mark.parametrize("error", [socket.EAI_AGAIN, socket.EAI_FAIL, None])
def test_proxy_final_hostname_refuses_on_a_resolver_failure(
    config_dir, ssh_g, monkeypatch, form, error
):
    def _gai(name, *_args, **_kwargs):
        if name == "self-alias.example":
            if error is None:
                raise OSError("resolver socket error")
            raise socket.gaierror(error, "temporary failure")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.7", 0))]

    monkeypatch.setattr(ssh_config_route.socket, "getaddrinfo", _gai)
    value = "198.51.100.7" if form == "proxyjump" else "ssh -W %h:%p 198.51.100.7"
    ssh_g.stdout = _effective(hostname="self-alias.example", **{form: value})
    assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize(
    "line",
    [
        'Match exec "test -f /tmp/x"',
        "match host foo.example exec true",
        "Match !exec true",
        # ssh strips quotes and backslashes inside a word; these all read as exec.
        'Match ex"ec" true',
        'Match !"exec" true',
        "Match ex\\ec true",
        '"Match" exec true',
        "Match=exec true",
    ],
)
def test_a_match_exec_line_skips_ssh_g(config_dir, ssh_g, line):
    (config_dir / "config").write_text(f"Host *\n  User someone\n{line}\n  HostName far.example\n")
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is False
    assert ssh_g.wraps == []
    assert ssh_g.calls == []


@pytest.mark.parametrize("snapshot", ["", "# café\nHost other.example\n  User someone\n"])
def test_ssh_g_uses_stdin_without_a_named_snapshot(ssh_g, monkeypatch, snapshot):
    def _no_file(*_args, **_kwargs):
        raise AssertionError("ssh -G must not write a snapshot file")

    ssh_g.stdout = _effective()
    with monkeypatch.context() as patched:
        patched.setattr(tempfile, "mkstemp", _no_file)
        patched.setattr(tempfile, "NamedTemporaryFile", _no_file)
        patched.setattr(ssh_config_route.os, "fdopen", _no_file)
        assert ssh_config_route._ssh_g(HOST, snapshot, "auto") == {"hostname": "198.51.100.9"}
    (call,) = ssh_g.calls
    assert call["argv"] == ["/usr/bin/ssh", "-G", "-F", "/dev/stdin", "--", HOST]
    assert call["input"] == snapshot.encode("utf-8")
    assert call["stdin"] is None
    assert ssh_g.snapshots == [snapshot]


def test_ssh_g_refuses_windows_without_spawning(ssh_g, monkeypatch):
    monkeypatch.setattr(ssh_config_route, "IS_WINDOWS", True)
    ssh_g.stdout = _effective()
    assert ssh_config_route._ssh_g(HOST, "Host other.example\n", "auto") is None
    assert ssh_g.wraps == []
    assert ssh_g.calls == []


def test_ssh_g_reads_exactly_the_scanned_bytes(config_dir, ssh_g):
    (config_dir / "config").write_text("Include conf.d/*.conf\nHost a.example\n  User u\n")
    (config_dir / "conf.d").mkdir()
    (config_dir / "conf.d" / "x.conf").write_text("Host c.example\n  Port 2\n")
    (config_dir.parent / "system" / "ssh_config").write_text("Host other.example\n  Port 22\n")
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is True
    assert ssh_g.snapshots == [
        "Host c.example\n  Port 2\nMatch all\nHost a.example\n  User u\nMatch all\n"
        "Host other.example\n  Port 22\n"
    ]


def test_a_config_changed_after_the_scan_does_not_reach_ssh_g(config_dir, ssh_g):
    scanned = "Host other.example\n  User someone\n"
    (config_dir / "config").write_text(scanned)
    ssh_g.on_run = lambda: (config_dir / "config").write_text("Match exec true\n")
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is True
    assert ssh_g.snapshots == [scanned + "Match all\n"]


@pytest.mark.parametrize("block", ["Host *.example", "Match host *", "Match final all"])
def test_an_include_inside_a_block_is_inlined_into_that_block(config_dir, ssh_g, block):
    (config_dir / "config").write_text(f"{block}\n  Include extra\n  User other\n")
    (config_dir / "extra").write_text("User someone\nCiphers aes256-ctr\n")
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is True
    # The file's lines stay inside the block: no Match-all reset follows them.
    assert ssh_g.snapshots[0].startswith(
        f"{block}\nUser someone\nCiphers aes256-ctr\n  User other\n"
    )


def test_the_stock_red_hat_system_config_does_not_refuse(config_dir, ssh_g):
    system = config_dir.parent / "system"
    (system / "ssh_config.d").mkdir()
    (system / "ssh_config").write_text(
        "Include /etc/ssh/ssh_config.d/*.conf\n".replace("/etc/ssh", str(system))
    )
    (system / "crypto.config").write_text("Ciphers aes256-gcm@openssh.com\nMACs hmac-sha2-256\n")
    (system / "ssh_config.d" / "50-redhat.conf").write_text(
        f"Match final all\n\tInclude {system / 'crypto.config'}\n"
        "\tGSSAPIAuthentication yes\n\tForwardX11Trusted yes\n"
    )
    ssh_g.stdout = _effective(proxyjump="jump.example")
    assert ssh_config_route.route_is_not_self(HOST) is True


@pytest.mark.parametrize(
    "included",
    [
        pytest.param("Host other.example\n  User x\n", id="opens-a-host-block"),
        pytest.param("Match host *\n  User x\n", id="opens-a-match-block"),
        pytest.param("  Include deeper\n", id="nested-include-opens-a-block"),
        pytest.param("  Include missing\n", id="nested-include-missing"),
    ],
)
def test_an_include_inside_a_block_that_cannot_stay_in_it_skips_ssh_g(config_dir, ssh_g, included):
    (config_dir / "config").write_text("Host *.example\n  Include extra\n")
    (config_dir / "extra").write_text(included)
    (config_dir / "deeper").write_text("Host x\n  User y\n")
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is False
    assert ssh_g.wraps == []


@pytest.mark.parametrize("key", ["HostName elsewhere.example", "ProxyJump elsewhere.example"])
def test_a_destination_key_an_in_block_include_sets_counts_for_that_block(config_dir, ssh_g, key):
    # The block applies to the hop, so the key it inherits refuses the hop.
    (config_dir / "config").write_text("Host jump.example\n  Include extra\n")
    (config_dir / "extra").write_text(f"{key}\n")
    ssh_g.stdout = _effective(proxyjump="jump.example")
    assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize(("mode", "backend"), [("off", "namespace"), ("auto", "none")])
def test_no_sandbox_to_confine_ssh_g_is_not_cleared(config_dir, ssh_g, mode, backend):
    ssh_g.mode = mode
    ssh_g.backend = backend
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is False
    assert ssh_g.wraps == []
    assert ssh_g.calls == []
    # Nothing is read either: no sandbox would confine the read.
    assert ssh_g.reads == [] and ssh_g.read_modes == []


def test_a_match_exec_in_an_included_file_skips_ssh_g(config_dir, ssh_g):
    (config_dir / "config").write_text("Include conf.d/*.conf\n")
    (config_dir / "conf.d").mkdir()
    (config_dir / "conf.d" / "a.conf").write_text("Match exec true\n")
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is False
    assert ssh_g.calls == []


@pytest.mark.parametrize(
    "setup",
    ["unreadable", "token-path"],
)
def test_an_include_that_cannot_be_read_skips_ssh_g(config_dir, ssh_g, setup):
    if setup == "token-path":
        (config_dir / "config").write_text("Include %d/extra\n")
    else:
        (config_dir / "config").write_text("Include extra\n")
        (config_dir / "extra").write_text("Host *\n")
        ssh_g.hidden = {str(config_dir / "extra"): "unreadable"}
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is False
    assert ssh_g.calls == []


def test_ssh_g_failing_or_missing_is_not_cleared(config_dir, ssh_g, monkeypatch):
    ssh_g.stdout = _effective()
    ssh_g.returncode = 255
    assert ssh_config_route.route_is_not_self(HOST) is False
    monkeypatch.setattr(
        platform_compat, "trusted_system_bin", lambda name: None if name == "ssh" else f"/x/{name}"
    )
    ssh_g.returncode = 0
    assert ssh_config_route.route_is_not_self(HOST) is False
    # Only the first call ran ssh; without a trusted ssh nothing runs.
    assert len(ssh_g.calls) == 1


SECRET = "TOPSECRET-token-value"


def test_every_config_read_runs_inside_the_sandbox(config_dir, ssh_g):
    (config_dir / "config").write_text("Include conf.d/*.conf\n")
    (config_dir / "conf.d").mkdir()
    (config_dir / "conf.d" / "x.conf").write_text("Host c.example\n  Port 2\n")
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is True
    system = str(config_dir.parent / "system" / "ssh_config")
    assert ssh_g.reads == [
        str(config_dir / "config"),
        str(config_dir / "conf.d" / "x.conf"),
        system,
    ]
    # The Include word keeps its own "/" (os.path.join only adds the base's separator).
    assert ssh_g.globs == [os.path.join(str(config_dir), "conf.d/*.conf")]
    # Each read ran at the agent's own tier; the stub refuses any gateway open().
    assert ssh_g.read_modes == ["auto"] * 4


@pytest.mark.parametrize("how", ["unreadable", "missing"])
def test_an_include_the_sandbox_hides_never_reaches_a_snapshot(config_dir, ssh_g, how):
    secret = config_dir.parent / "crew" / ".env"
    secret.parent.mkdir()
    secret.write_text(f"Host *\n  User {SECRET}\n")
    (config_dir / "config").write_text(f"Include {secret}\n")
    ssh_g.hidden = {str(secret): how}
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is False
    assert ssh_g.calls == []
    assert not any(SECRET in snap for snap in ssh_g.snapshots)


def test_an_include_glob_lists_only_what_the_sandbox_shows(config_dir, ssh_g):
    masked = config_dir.parent / "masked"
    masked.mkdir()
    (masked / "a.conf").write_text(f"Host *\n  User {SECRET}\n")
    (config_dir / "config").write_text(f"Include {masked}/*\nHost a.example\n  User u\n")
    # The whole tree is hidden: inside the sandbox the glob matches nothing.
    ssh_g.hidden = {str(masked): "missing"}
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is True
    assert str(masked / "a.conf") not in ssh_g.reads
    assert ssh_g.snapshots == ["Host a.example\n  User u\nMatch all\n"]


def test_a_default_config_the_sandbox_hides_is_not_cleared(config_dir, ssh_g):
    (config_dir / "config").write_text(f"Host *\n  User {SECRET}\n")
    ssh_g.hidden = {str(config_dir): "missing"}
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is False
    assert ssh_g.calls == []


@pytest.mark.parametrize("form", ["hostname", "proxyjump", "proxycommand"])
@pytest.mark.parametrize(
    "target",
    ["127.0.0.1", "0.0.0.0", "203.0.113.55", "myalias", "%h", "$HOST", "*.example"],
)
def test_each_form_requires_a_literal_remote_address(config_dir, ssh_g, form, target):
    value = f"ssh -W %h:%p {target}" if form == "proxycommand" else target
    ssh_g.stdout = _effective(**{form: value})
    assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize("form", ["hostname", "proxyjump", "proxycommand"])
@pytest.mark.parametrize(
    ("target", "reason"),
    [
        (target, reason)
        for target in ("198.51.100.9", "jump.example")
        for reason in ("unpublished", "kernel-own", "host-block")
    ]
    + [("jump.example", "empty-dns"), ("jump.example", "mixed-dns")],
)
def test_each_form_uses_the_live_address_guard(
    config_dir, ssh_g, monkeypatch, form, target, reason
):
    if reason == "unpublished":
        monkeypatch.setattr(argv_floor_hosts, "_NETLINK_ADDRS_PUBLISHED", False)
    elif reason == "kernel-own":
        monkeypatch.setattr(argv_floor_hosts, "_kernel_says_own_address", lambda *_a: True)
    elif reason == "host-block":
        (config_dir / "config").write_text(f"Host {target}\n  HostName other.example\n")
    else:
        addresses = [] if reason == "empty-dns" else ["198.51.100.7", "127.0.0.1"]
        monkeypatch.setattr(
            ssh_config_route.socket,
            "getaddrinfo",
            lambda *_a, **_k: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in addresses
            ],
        )
    value = f"ssh -W %h:%p {target}" if form == "proxycommand" else target
    ssh_g.stdout = _effective(**{form: value})
    assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize(
    "command",
    [
        "nc 198.51.100.9 22",
        "socat STDIO TCP:x.example:22",
        "ssh -F f -W %h:%p jump.example",
        "ssh -o X=y -W %h:%p jump.example",
        "ssh -T -W %h:%p jump.example",
        "ssh -W %h:%p jump.example extra",
        "ssh -i key -J jump.example -W %h:%p jump.example",
        "ssh -W %h:%p jump.example;true",
        # A plain program that never dials %h, or names another destination too.
        "corp-proxy connect jump.example",
        "nc -U %h",
        "socat - UNIX-CONNECT:%h",
        "socat - UNIX:%h",
        "socat - ABSTRACT-CONNECT:%h",
        "ncat --unixsock %h",
        "nc -X connect %h %p",
        "corp-proxy --target=%h",
        "socat - OPEN:%h",
        "nc -x 127.0.0.1:3128 %h %p",
        "nc -x127.0.0.1:3128 %h %p",
        "nc -x ::1:3128 %h %p",
        "nc -x proxy.example:3128 %h %p",
        "nc -x localhost %h %p",
        "nc -xlocalhost %h %p",
        "nc -w10 %h %p",
        "nc -w 0 %h %p",
        "socat - TCP:localhost:22,bind=%h",
        "corp-proxy --via=203.0.113.55 %h",
        "corp-proxy --via=::1 %h",
        "corp-proxy --via=me@localhost %h",
        "nc %h.other.example %p",
        "nc other-%h %p",
        # Percent tokens other than %h and %p name other hosts or local state.
        "nc %l %p",
        "nc %h %p %n",
        "nc %%h %p",
        "corp-proxy --user=%r %h",
        # Shell syntax, quoting and expansion are not one plain argv.
        "nc %h %p;true",
        "nc %h %p|tee",
        "nc %h %p>/tmp/x",
        "nc $HOST %h",
        "nc %h `id`",
        "sh -c 'nc %h %p'",
        '"nc" %h %p',
        "~/bin/proxy %h",
        "nc %h* %p",
        # Clients that route through ssh config qualify only as ssh -W, also
        # behind a program that runs them.
        "/usr/bin/ssh %h",
        "scp %h",
        "SFTP.EXE %h",
        "rsync %h",
        "autossh -W %h:%p jump",
        "exec ssh -W %h:%p jump",
        "env ssh %h",
        "busybox ssh %h",
        "timeout ssh -W %h:%p jump",
        "env FOO=1 nc %h %p",
        "-nc %h %p",
        "nc",
    ],
)
def test_other_proxy_commands_are_refused(config_dir, ssh_g, command):
    ssh_g.stdout = _effective(proxycommand=command)
    assert ssh_config_route.route_is_not_self(HOST) is False


_PLAIN_PROXY_COMMANDS = [
    "/usr/local/bin/corp-proxy proxy %h",
    "nc %h %p",
    "/usr/bin/socat - TCP:%h:%p",
    "socat STDIO tcp4:%h:%p",
    "ncat %h:%p",
]


@pytest.mark.parametrize(
    "command", ["nc -x selfproxy %h %p", "nc -x flaky %h %p", "corp-proxy selfproxy %h"]
)
def test_a_plain_proxy_word_dns_maps_to_this_machine_refuses(
    config_dir, ssh_g, monkeypatch, command
):
    def _gai(name, *_args, **_kwargs):
        if name == "selfproxy":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        if name == "flaky":
            raise socket.gaierror(socket.EAI_AGAIN, "temporary failure")
        raise socket.gaierror(socket.EAI_NONAME, "no such name")

    monkeypatch.setattr(ssh_config_route.socket, "getaddrinfo", _gai)
    ssh_g.stdout = _effective(proxycommand=command)
    assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize("command", _PLAIN_PROXY_COMMANDS)
@pytest.mark.parametrize("hostname", [HOST, "far.example", "198.51.100.9", "2001:db8::9"])
def test_a_plain_proxy_command_to_a_remote_name_is_cleared(config_dir, ssh_g, command, hostname):
    ssh_g.stdout = _effective(hostname=hostname, proxycommand=command)
    assert ssh_config_route.route_is_not_self(HOST) is True


def test_a_wildcard_proxy_command_block_clears_its_hosts(config_dir, ssh_g):
    # A proxy tool's generated block: every matching host goes through the proxy.
    (config_dir / "config").write_text(
        "Host *.example\n  ProxyCommand=/usr/local/bin/corp-proxy proxy %h\n"
        "  ServerAliveInterval 15\n  User someone\n",
        encoding="utf-8",
    )
    ssh_g.stdout = _effective(hostname=HOST, proxycommand="/usr/local/bin/corp-proxy proxy %h")
    assert ssh_config_route.route_is_not_self(HOST) is True


@pytest.mark.parametrize(
    "hostname", ["tcp:127.0.0.1:22", "unix-connect:a.sock", "abstract-connect:x.y", "a.b:22"]
)
@pytest.mark.parametrize("command", ["socat - %h", "socat %h -", "nc %h %p"])
def test_a_plain_proxy_command_refuses_a_final_hostname_with_an_address_type(
    config_dir, ssh_g, hostname, command
):
    ssh_g.stdout = _effective(hostname=hostname, proxycommand=command)
    assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize("hostname", ["i-0123456789abcdef0", "desktop", "desktop."])
def test_a_plain_proxy_command_needs_a_dotted_or_ip_final_hostname(config_dir, ssh_g, hostname):
    # A dotless HostName may be an id only the proxy can map, so it is not a proof.
    ssh_g.stdout = _effective(hostname=hostname, proxycommand="aws ssm start-session --target %h")
    assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize(
    "hostname", ["localhost", "127.0.0.1", "203.0.113.55", "crew-host.example", "own.example"]
)
def test_a_plain_proxy_command_to_this_machine_refuses(config_dir, ssh_g, monkeypatch, hostname):
    monkeypatch.setattr(
        argv_floor_hosts,
        "_own_host_names",
        lambda: frozenset({"203.0.113.55", "crew-host.example"}),
    )
    dns = {"own.example": "203.0.113.55"}

    def _gai(name, *_args, **_kwargs):
        if name not in dns:
            raise socket.gaierror(socket.EAI_NONAME, "no such name")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (dns[name], 0))]

    monkeypatch.setattr(ssh_config_route.socket, "getaddrinfo", _gai)
    ssh_g.stdout = _effective(hostname=hostname, proxycommand="nc %h %p")
    assert ssh_config_route.route_is_not_self(HOST) is False


def test_a_plain_proxy_command_with_a_proxy_jump_refuses(config_dir, ssh_g):
    ssh_g.stdout = _effective(proxycommand="nc %h %p", proxyjump="jump.example")
    assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize(
    "jump", ["a.example,b.example", "198.51.100.9,198.51.100.8", "%h", "ssh://jump.example"]
)
def test_only_one_literal_jump_is_cleared(config_dir, ssh_g, jump):
    ssh_g.stdout = _effective(proxyjump=jump)
    assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize(
    "criteria",
    [
        "host X user u",
        "localuser u",
        "tagged t",
        "localnetwork 10.0.0.0/8",
        "exec true",
        "!user u",
    ],
)
def test_match_criteria_outside_the_proof_skip_ssh_g(config_dir, ssh_g, criteria):
    (config_dir / "config").write_text(f"Match {criteria}\n  HostName jump.example\n")
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is False
    assert ssh_g.calls == []


@pytest.mark.parametrize(
    "effective",
    [
        _effective(hostname=HOST),
        _effective(proxyjump="jump.example", proxycommand="ssh -W %h:%p jump.example"),
    ],
)
def test_no_rewrite_or_two_proxy_forms_is_not_a_proof(config_dir, ssh_g, effective):
    ssh_g.stdout = effective
    assert ssh_config_route.route_is_not_self(HOST) is False


_FORM_VALUES = {
    "hostname": "jump.example",
    "proxyjump": "jump.example",
    "proxycommand": "ssh -W %h:%p jump.example",
}
# The ``Host *`` sections Debian/Ubuntu and macOS ship in /etc/ssh/ssh_config.
_STOCK_SYSTEM_CONFIGS = {
    "debian": (
        "Host *\n    SendEnv LANG LC_*\n    HashKnownHosts yes\n    GSSAPIAuthentication yes\n"
    ),
    "macos": "Host *\n    SendEnv LANG LC_*\n",
}


@pytest.mark.parametrize("form", sorted(_FORM_VALUES))
@pytest.mark.parametrize("system", sorted(_STOCK_SYSTEM_CONFIGS))
def test_a_stock_system_host_star_block_does_not_refuse(config_dir, ssh_g, form, system):
    (config_dir.parent / "system" / "ssh_config").write_text(_STOCK_SYSTEM_CONFIGS[system])
    ssh_g.stdout = _effective(**{form: _FORM_VALUES[form]})
    assert ssh_config_route.route_is_not_self(HOST) is True


@pytest.mark.parametrize("form", sorted(_FORM_VALUES))
@pytest.mark.parametrize(
    "section",
    [
        pytest.param("Host *\n  HostName elsewhere.example\n", id="host-star-hostname"),
        pytest.param("Host *\n  ProxyJump elsewhere.example\n", id="host-star-proxyjump"),
        pytest.param("Host *\n  ProxyCommand nc %h %p\n", id="host-star-proxycommand"),
        pytest.param("Host *\n  CanonicalizeHostname yes\n", id="host-star-canonicalize"),
        pytest.param("Host jump.example\n  HostName elsewhere.example\n", id="host-hop-hostname"),
        pytest.param(
            "Match host jump.example\n  HostName elsewhere.example\n", id="match-hop-hostname"
        ),
    ],
)
def test_a_block_that_sets_a_destination_key_for_the_hop_refuses(config_dir, ssh_g, form, section):
    (config_dir.parent / "system" / "ssh_config").write_text(
        _STOCK_SYSTEM_CONFIGS["debian"] + section
    )
    ssh_g.stdout = _effective(**{form: _FORM_VALUES[form]})
    assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize("form", ["hostname", "proxyjump", "proxycommand"])
@pytest.mark.parametrize(
    ("target", "pattern", "cleared"),
    [
        pytest.param(
            "198.51.100.9",
            "* !proxy-only.example !198.51.100.[9]",
            False,
            id="negated-brackets-are-literal",
        ),
        pytest.param("198.51.100.9", "198.51.100.[9]", True, id="positive-brackets-are-literal"),
        pytest.param("198.51.100.9", "198.51.*", False, id="star-wildcard"),
        pytest.param("198.51.100.9", "198.51.100.?", False, id="question-wildcard"),
        pytest.param("198.51.100.9", "* !198.51.*", True, id="negated-star-wildcard"),
        pytest.param("198.51.100.9", "* !198.51.100.?", True, id="negated-question-wildcard"),
        # OpenSSH matches Host words case-sensitively (checked with ssh -G 9.9p1).
        pytest.param("jump.example", "JUMP.EXAMPLE", True, id="case-sensitive-positive"),
        pytest.param("jump.example", "* !JUMP.EXAMPLE", False, id="case-sensitive-negative"),
    ],
)
def test_host_patterns_use_openssh_wildcards(config_dir, ssh_g, form, target, pattern, cleared):
    value = f"ssh -W %h:%p {target}" if form == "proxycommand" else target
    (config_dir / "config").write_text(
        # The first block applies to every spelling of HOST, so only the hop is judged.
        f"Host * !{target}\n  {form} {value}\n  HostName 198.51.100.9\n"
        f"Host {pattern}\n  ProxyCommand nc 203.0.113.1 22\n"
        "  HostName 203.0.113.1\n",
        encoding="utf-8",
    )
    ssh_g.stdout = _effective(**{form: value})
    assert ssh_config_route.route_is_not_self(HOST) is cleared


def test_nul_cannot_hide_a_proxy_hops_local_hostname(config_dir, ssh_g):
    (config_dir / "config").write_bytes(
        f"Host {HOST}\n  HostName 198.51.100.8\n  ProxyJump 198.51.100.9\n".encode()
        + b"Host 198.51.100.9\x00 ignored\n  HostName 127.0.0.1\n"
    )
    ssh_g.stdout = _effective(hostname="198.51.100.8", proxyjump="198.51.100.9")
    assert ssh_config_route.route_is_not_self(HOST) is False
    assert ssh_g.calls == []


def test_unicode_whitespace_cannot_hide_a_proxy_hops_local_hostname(config_dir, ssh_g):
    (config_dir / "config").write_bytes(
        (
            f"Host {HOST}\n  HostName 198.51.100.8\n  ProxyJump 198.51.100.9\n"
            "Host * !198.51.100.9\u00a0junk\n  HostName 127.0.0.1\n"
        ).encode()
    )
    ssh_g.stdout = _effective(hostname="198.51.100.8", proxyjump="198.51.100.9")
    assert ssh_config_route.route_is_not_self(HOST) is False
    assert ssh_g.calls == []


@pytest.mark.parametrize("included", [False, True], ids=["user", "include"])
@pytest.mark.parametrize(
    "character",
    [
        "\x00",
        "\r",
        "\v",
        "\f",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x85",
        "\u2028",
        "\u2029",
        "\u00a0",
        "\u1680",
        "\u2003",
        "\u202f",
        "\u205f",
        "\u3000",
    ],
)
def test_ambiguous_config_characters_skip_ssh_g(config_dir, ssh_g, included, character):
    target = config_dir / "config"
    if included:
        target.write_bytes(b"Include extra.conf\n")
        target = config_dir / "extra.conf"
    target.write_bytes(
        f"# before{character}# after\nHost {HOST}\n  HostName 198.51.100.9\n".encode()
    )
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is False
    assert ssh_g.calls == []


@pytest.mark.parametrize("line_end", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("form", ["hostname", "proxyjump"])
@pytest.mark.parametrize("comment", ["", "# café\n"], ids=["no-comment", "unicode-comment"])
def test_normal_line_endings_and_tabs_are_allowed(config_dir, ssh_g, line_end, form, comment):
    text = comment + f"Host\t{HOST}\n\tHostName 198.51.100.8\n"
    if form == "proxyjump":
        text += "\tProxyJump 198.51.100.9\n"
    (config_dir / "config").write_bytes(text.replace("\n", line_end).encode())
    ssh_g.stdout = _effective(
        hostname="198.51.100.8", proxyjump="198.51.100.9" if form == "proxyjump" else None
    )
    assert ssh_config_route.route_is_not_self(HOST) is True
    assert ssh_g.snapshots == [text + "Match all\n"]


@pytest.mark.parametrize("form", ["proxyjump", "proxycommand"])
@pytest.mark.parametrize(
    "hop", ["[198.51.100.9]", "[198.51.100.9]:22", "u@[198.51.100.9]", "[2001:db8::9]"]
)
def test_a_bracketed_hop_is_not_a_proof(config_dir, ssh_g, form, hop):
    # OpenSSH matches a Host block on the bracketed spelling, which the proof does not model.
    value = hop if form == "proxyjump" else f"ssh -W %h:%p {hop}"
    (config_dir / "config").write_text(f"Host {hop}\n  HostName 127.0.0.1\n", encoding="utf-8")
    ssh_g.stdout = _effective(hostname="198.51.100.8", **{form: value})
    assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(
            f"Host {HOST}\n  HostName 198.51.100.9\n"
            f"Host {HOST.upper()} {HOST}.\n  HostName 127.0.0.1\n",
            id="case-and-dot-spelling-block",
        ),
        pytest.param(
            f"Host Proxy-Only.*\n  HostName 127.0.0.1\nHost {HOST}\n  HostName 198.51.100.9\n",
            id="mixed-case-wildcard-before",
        ),
        pytest.param(
            f"Host * !{HOST}\n  HostName 127.0.0.1\nHost {HOST}\n  HostName 198.51.100.9\n",
            id="a-variant-escapes-the-negation",
        ),
        pytest.param(
            f"Host {HOST}\n  HostName 198.51.100.9\nHost *\n  HostName 127.0.0.1\n",
            id="a-catch-all-after-the-exact-block",
        ),
        pytest.param(
            "Host proxy-*\n  HostName 198.51.100.9\nHost *.example\n  HostName 127.0.0.1\n",
            id="two-spelling-dependent-blocks",
        ),
        pytest.param(
            f"Host {HOST}\n  HostName 198.51.100.9\nMatch all\n  HostName 127.0.0.1\n",
            id="a-routing-match-after-the-exact-block",
        ),
    ],
)
def test_a_route_another_spelling_could_take_is_not_a_proof(config_dir, ssh_g, config):
    # The gate checks the lowercased, dot-stripped name; OpenSSH matches the typed spelling.
    (config_dir / "config").write_text(config, encoding="utf-8")
    ssh_g.stdout = _effective()
    assert ssh_config_route.route_is_not_self(HOST) is False


@pytest.mark.parametrize(
    ("config", "effective"),
    [
        pytest.param(
            f"Host {HOST}\n  HostName 198.51.100.9\n", _effective(), id="one-exact-rewrite"
        ),
        pytest.param(
            "Host proxy-*\n  ProxyJump 198.51.100.7\nHost *\n  User me\n  ServerAliveInterval 60\n",
            _effective(hostname=HOST, proxyjump="198.51.100.7"),
            id="wildcard-jump-then-stock-star",
        ),
        pytest.param(
            "Host * !198.51.100.7\n  ProxyJump 198.51.100.7\n",
            _effective(hostname=HOST, proxyjump="198.51.100.7"),
            id="one-jump-for-every-spelling",
        ),
        pytest.param(
            f"Host * !198.51.100.7\n  ProxyJump 198.51.100.7\nHost {HOST}\n  HostName 198.51.100.9\n",
            _effective(proxyjump="198.51.100.7"),
            id="uniform-block-before-the-exact-block",
        ),
    ],
)
def test_a_route_every_spelling_shares_is_still_cleared(config_dir, ssh_g, config, effective):
    (config_dir / "config").write_text(config, encoding="utf-8")
    ssh_g.stdout = effective
    assert ssh_config_route.route_is_not_self(HOST) is True
