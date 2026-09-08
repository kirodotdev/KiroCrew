"""Windows-support assertions for the Design Tweak backend.

Two things are pinned here: the manifest's platform declaration, and the code
paths that decided whether the app could honestly claim Windows at all.

The load-bearing one is owned dev-server port discovery. `detect_dev_servers`
answers "which listener belongs to this folder" through port -> pid -> working
directory, and this repo has no cross-platform source for that second hop. When
that was the ONLY channel, starting a project's dev server on a host without
`lsof` spawned a healthy server, failed to find its port for 45s, killed it, and
told the user it never started listening. The second channel closes that, and
these tests pin both its success case and — more importantly — its refusals.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.design_tweak.backend import dev_preview, request_state

MANIFEST = Path(dev_preview.__file__).resolve().parent.parent / "app.json"

# `os.path.normcase` folds case only where the filesystem does. Probing it is how
# a case-sensitivity test stays honest on both kinds of host.
_CASE_FOLDING = os.path.normcase("A") != "A"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# --- manifest platform declaration ---


def test_manifest_declares_every_platform_the_app_runs_on():
    """`platform.os` summarises the whole app, and every part of it now runs.

    Pinned because the previous `["macos", "linux"]` read as a technical
    limitation when it was an unfinished adaptation: the only macOS-specific
    piece is the native folder chooser, which already answered 501 on Linux with
    a documented fallback.
    """
    assert _manifest()["platform"]["os"] == ["macos", "linux", "windows"]


def test_declared_platforms_all_resolve_to_a_real_sys_platform():
    """Every declared name must map to a sys.platform value.

    An unmapped name is silently accepted into the list and then never matches,
    so a declaration can claim a platform the gate rejects.
    """
    from kiro_crew.apps.manifest import PlatformConfig

    cfg = PlatformConfig(os=_manifest()["platform"]["os"])
    for sys_platform in ("darwin", "linux", "win32"):
        assert cfg.supports_platform(sys_platform), sys_platform


# --- port discovery from the dev server's own output ---


def _log_runtime() -> SimpleNamespace:
    return SimpleNamespace(
        Path=Path,
        _LOG_TAIL_BYTES=dev_preview.LOG_TAIL_BYTES,
        _LOG_PORT_LIMIT=dev_preview.LOG_PORT_LIMIT,
        _ANSI_RE=dev_preview.ANSI_RE,
        _LOG_URL_RE=dev_preview.LOG_URL_RE,
    )


def test_dev_log_ports_reads_the_port_through_vite_colour_escapes(tmp_path):
    """Vite wraps the port DIGITS in escapes, so de-ANSI must precede the match.

    The Network line is deliberately a different port on a LAN address: a
    non-loopback host must not contribute a candidate, and using the same port
    for both would let dedupe hide that.
    """
    log = tmp_path / "devserver-p1.log"
    log.write_text(
        "  VITE v5.0.0  ready in 412 ms\n"
        "  \x1b[32m\u279c\x1b[39m  Local:   http://localhost:\x1b[1m5173\x1b[22m/\n"
        "  \x1b[32m\u279c\x1b[39m  Network: http://192.168.1.9:\x1b[1m4173\x1b[22m/\n",
        encoding="utf-8",
    )
    assert dev_preview.dev_log_ports(_log_runtime(), str(log)) == [5173]


def test_dev_log_ports_keeps_first_appearance_order_and_dedupes(tmp_path):
    """The primary URL is printed first, so order is the ranking."""
    log = tmp_path / "devserver-p2.log"
    log.write_text(
        "ready on http://127.0.0.1:3000\n"
        "also http://localhost:3000\n"
        "proxy http://localhost:8080\n",
        encoding="utf-8",
    )
    assert dev_preview.dev_log_ports(_log_runtime(), str(log)) == [3000, 8080]


def test_dev_log_ports_is_empty_when_the_log_is_absent_or_silent(tmp_path):
    """A missing or URL-free log degrades to no candidates, never an exception."""
    assert dev_preview.dev_log_ports(_log_runtime(), str(tmp_path / "nope.log")) == []
    quiet = tmp_path / "devserver-p3.log"
    quiet.write_bytes(b"compiling...\n\xff\xfe not utf-8\n")
    assert dev_preview.dev_log_ports(_log_runtime(), str(quiet)) == []


# --- ownership is proved, never taken from the log ---


def _listener_runtime(entries, *, in_tree: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        _dev_log_ports=lambda _path: [5173],
        _tcp_listeners=lambda port: entries if port == 5173 else [],
        address_covers_loopback=platform_compat.address_covers_loopback,
        _in_proc_tree=lambda _pid, _root, _pgid: in_tree,
    )


def test_owned_listener_adopts_a_port_its_own_pid_holds():
    runtime = _listener_runtime([platform_compat.PortListener(4242, "0.0.0.0", "4")])
    found = dev_preview.owned_listener(runtime, "devserver.log", 4242, None)
    assert found is not None
    assert found["port"] == 5173
    assert found["url"] == "http://localhost:5173"
    # No pid -> cwd source is consulted; that is the whole point of this channel.
    assert found["cwd"] == ""


def test_owned_listener_adopts_a_port_held_by_a_child_of_the_spawned_process():
    """npm forks the real listener, so the holder is usually a descendant."""
    runtime = _listener_runtime(
        [platform_compat.PortListener(9001, "127.0.0.1", "4")],
        in_tree=True,
    )
    found = dev_preview.owned_listener(runtime, "devserver.log", 4242, None)
    assert found is not None and found["pid"] == 9001


def test_owned_listener_refuses_a_port_held_by_an_unrelated_process():
    """The log only NOMINATES a port; a foreign holder must not be adopted.

    This is the security property of the channel: the log is untrusted project
    output, so a project that printed someone else's URL still cannot make the
    backend front a port its own process tree does not hold.
    """
    runtime = _listener_runtime([platform_compat.PortListener(7, "0.0.0.0", "4")])
    assert dev_preview.owned_listener(runtime, "devserver.log", 4242, None) is None


def test_owned_listener_refuses_a_listener_not_reachable_from_loopback():
    """Right pid, wrong bind: the preview proxy only ever talks to 127.0.0.1."""
    runtime = _listener_runtime([platform_compat.PortListener(4242, "10.0.0.5", "4")])
    assert dev_preview.owned_listener(runtime, "devserver.log", 4242, None) is None


def test_foreign_detection_availability_tracks_the_lsof_binary():
    """Adopting a server the USER started needs lsof; report that, don't guess.

    Probed rather than branched on `sys.platform` so a POSIX host without lsof
    gets the same honest answer as Windows.
    """
    absent = SimpleNamespace(trusted_system_bin=lambda _name: None)
    present = SimpleNamespace(trusted_system_bin=lambda _name: "lsof")
    assert dev_preview.foreign_detection_available(absent) is False
    assert dev_preview.foreign_detection_available(present) is True


# --- the fix, end to end through the composition root ---


def test_start_dev_proc_finds_the_port_without_any_pid_to_cwd_source(monkeypatch, tmp_path):
    """The blocking bug: a healthy dev server started, killed, and misreported.

    `_detect_dev_servers` returns [] here — exactly what it does on a host with
    no `lsof` — and the fake `npm` writes the banner a real one prints. The
    listener table then proves the pid, so the server is adopted instead of being
    torn down after 45 seconds with "did not start listening".
    """
    from kiro_crew.apps.builtins.design_tweak.backend import server as mod

    root = tmp_path / "project"
    (root / "node_modules").mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()

    def fake_popen(argv, **kwargs):
        handle = kwargs["stdout"]
        handle.write(b"  Local:   http://localhost:5173/\n")
        handle.flush()
        return SimpleNamespace(pid=4242, poll=lambda: None, returncode=None)

    monkeypatch.setattr(mod, "DATA_DIR", data)
    monkeypatch.setattr(mod, "_DEV_PROCS", {})
    monkeypatch.setattr(mod, "IS_POSIX", False)
    monkeypatch.setattr(mod, "_dev_command", lambda _root: ["npm", "run", "dev"])
    monkeypatch.setattr(mod, "_resolve_bin", lambda _name: tmp_path / "npm")
    monkeypatch.setattr(mod, "_child_env", lambda _bin_dir: {})
    monkeypatch.setattr(mod, "_detect_dev_servers", lambda _root, probe=True: [])
    monkeypatch.setattr(
        mod,
        "_tcp_listeners",
        lambda port: [platform_compat.PortListener(4242, "127.0.0.1", "4")] if port == 5173 else [],
    )
    monkeypatch.setattr(mod, "_front_with_proxy", lambda _pid, _url: "http://127.0.0.1:9999")
    monkeypatch.setattr(
        mod,
        "subprocess",
        SimpleNamespace(
            Popen=fake_popen,
            STDOUT=-2,
            SubprocessError=Exception,
        ),
    )

    result = mod._start_dev_proc("p1", root)

    assert result["ok"] is True
    assert result["devUrl"] == "http://localhost:5173"
    assert result["port"] == 5173
    assert result["injected"] is True
    assert mod._DEV_PROCS["p1"]["url"] == "http://localhost:5173"


# --- containment survives the case-normalized comparison ---


def test_contain_path_still_refuses_escapes_and_sibling_prefixes(tmp_path):
    """normcase must not weaken the barrier it was added to make reliable.

    The sibling case is the one a plain `startswith` gets wrong: `queue-other`
    shares the whole `queue` prefix, and only the separator check rejects it.
    """
    base = tmp_path / "queue"
    base.mkdir()
    (tmp_path / "queue-other").mkdir()

    assert request_state.contain_path(base, "r-1.json").name == "r-1.json"
    assert request_state.contain_path(base) == Path(os.path.realpath(base))

    with pytest.raises(request_state.PathEscape):
        request_state.contain_path(base, "..")
    with pytest.raises(request_state.PathEscape):
        request_state.contain_path(base, os.path.join("..", "queue-other", "x.json"))


@pytest.mark.skipif(not _CASE_FOLDING, reason="normcase folds case only on Windows")
def test_contain_path_accepts_a_base_spelled_in_another_case(tmp_path):
    """On a case-insensitive filesystem one directory has many spellings."""
    base = tmp_path / "queue"
    base.mkdir()
    contained = request_state.contain_path(str(base).upper(), "r-2.json")
    assert contained.name == "r-2.json"
