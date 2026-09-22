"""Tests for :mod:`kiro_crew.diag.procs` -- the read-only process-tree view.

The whole suite runs against a FAKE process table built under ``tmp_path``, so
nothing here reads the real ``/proc``, spawns ``ps``, or touches a live process.
That is also what makes the negative assertions meaningful: a stranger process
and a non-allowlisted environment key exist in the fixture, and the tests assert
they never reach the output.

``kiro_crew.diag`` has no ``__init__.py`` in this branch -- worker A owns that
file -- so the package resolves as a namespace package. The import below is the
test that this works; no conftest shim is needed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kiro_crew import platform_compat, session_pid
from kiro_crew.diag import procs

NUL = b"\x00"


def nul_argv(*tokens: bytes) -> bytes:
    """Argv in the NUL-separated shape ``/proc/<pid>/cmdline`` uses."""
    return NUL.join(tokens) + NUL


# -- the family under test ---------------------------------------------------

OUTSIDER = 900  # a systemd-like parent that is NOT a family member
GATEWAY = 1000
CHAT = 1001
BROWSER = 1002
ORPHAN = 1003
ORPHAN_CHILD = 1004
RUNNER = 1005
STRANGER = 1006

MARKER = {session_pid.KIROCREW_SPAWNED_ENV: session_pid.KIROCREW_SPAWNED_VALUE}

GATEWAY_ARGV = nul_argv(b"/v/bin/python3", b"-m", b"kiro_crew.cli", b"gateway")
CHAT_ARGV = nul_argv(b"/usr/local/bin/kiro-cli-chat", b"--acp")
BROWSER_ARGV = nul_argv(b"node", b"/n/playwright-core/lib/entry/cliDaemon.js", b"kc-abc")
RENDERER_ARGV = nul_argv(b"/opt/chrome/chrome-headless", b"--type=renderer")
RUNNER_ARGV = nul_argv(b"/v/bin/pytest", b"-q", b"test/test_diag_procs.py")
STRANGER_ARGV = nul_argv(b"/usr/sbin/sshd", b"-D")


class FakeProcTable:
    """A writable stand-in for ``/proc``, one directory per process.

    Only the files :mod:`kiro_crew.diag.procs` reads are created, and each is
    written in the real kernel's shape -- including a ``comm`` containing a space
    and a ``)``, because surviving that is the ``stat`` parser's whole job.
    """

    def __init__(self, root: Path, uptime: float = 100_000.0) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.uptime = uptime
        self.clk_tck = procs._clock_ticks() or 100
        (root / "uptime").write_text(f"{uptime:.2f} 50000.00\n", encoding="utf-8")

    def add(
        self,
        pid: int,
        ppid: int,
        *,
        comm: str = "proc (x)",
        state: str = "S",
        cmdline: bytes = b"",
        env: dict[str, str] | None = None,
        utime: int = 0,
        stime: int = 0,
        thread_states: tuple[str, ...] = ("S",),
        thread_wchans: tuple[str, ...] | None = None,
        rss_kb: int | None = 2048,
        swap_kb: int | None = 0,
        fds: int = 3,
        cwd: str | None = "/work/project",
        runq_ns: int = 0,
        age_secs: float = 60.0,
    ) -> None:
        pdir = self.root / str(pid)
        (pdir / "fd").mkdir(parents=True, exist_ok=True)

        fields = ["0"] * 20
        fields[0] = state
        fields[1] = str(ppid)
        fields[2] = str(pid)
        fields[11] = str(utime)
        fields[12] = str(stime)
        fields[17] = str(len(thread_states))
        fields[19] = str(int((self.uptime - age_secs) * self.clk_tck))
        (pdir / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n", encoding="utf-8")

        status = [f"Name:\t{comm}", f"State:\t{state}", f"PPid:\t{ppid}"]
        status.append(f"Threads:\t{len(thread_states)}")
        if rss_kb is not None:
            status.append(f"VmRSS:\t{rss_kb} kB")
        if swap_kb is not None:
            status.append(f"VmSwap:\t{swap_kb} kB")
        (pdir / "status").write_text("\n".join(status) + "\n", encoding="utf-8")

        (pdir / "cmdline").write_bytes(cmdline)
        if env is not None:
            blob = NUL.join(f"{k}={v}".encode() for k, v in env.items()) + NUL
            (pdir / "environ").write_bytes(blob)
        (pdir / "schedstat").write_text(f"1000 {runq_ns} 7\n", encoding="utf-8")
        if cwd is not None:
            (pdir / "cwd").write_text(cwd, encoding="utf-8")
        for index in range(fds):
            (pdir / "fd" / str(index)).write_text("", encoding="utf-8")

        wchans = thread_wchans if thread_wchans is not None else ("poll_schedule_timeout",)
        for offset, tstate in enumerate(thread_states):
            tdir = pdir / "task" / str(pid + offset)
            tdir.mkdir(parents=True, exist_ok=True)
            tfields = ["0"] * 20
            tfields[0] = tstate
            tfields[1] = str(ppid)
            (tdir / "stat").write_text(
                f"{pid + offset} ({comm}) " + " ".join(tfields) + "\n", encoding="utf-8"
            )
            wchan = wchans[offset] if offset < len(wchans) else wchans[-1]
            (tdir / "wchan").write_text(wchan, encoding="utf-8")

    def scan(self, prev: procs.Roster | None = None, **kwargs: object) -> procs.Roster:
        """Scan this table with every production seam replaced by a stub.

        ``clk_tck`` is passed explicitly because Windows has no
        ``os.sysconf``: without it every cpu and age figure read ``None`` on a
        Windows runner and the delta tests failed there while passing on Linux.
        """
        params: dict[str, object] = {
            "proc_root": self.root,
            "platform_name": "linux",
            "gateway_pid": GATEWAY,
            "clk_tck": self.clk_tck,
            "subreaper_pids_fn": lambda: {1},
            "tracked_pids_fn": set,
            "tracked_owners_fn": dict,
            "unreachable_orphan_fn": lambda pid, cmdline, tracked: False,
        }
        params.update(kwargs)
        return procs.scan(prev, **params)  # type: ignore[arg-type]


@pytest.fixture
def family(tmp_path: Path) -> FakeProcTable:
    """A table holding one member of every family arm, plus one stranger."""
    table = FakeProcTable(tmp_path / "proc")
    table.add(GATEWAY, OUTSIDER, comm="python3", cmdline=GATEWAY_ARGV, env=dict(MARKER))
    table.add(CHAT, GATEWAY, comm="kiro-cli-chat", cmdline=CHAT_ARGV, env=dict(MARKER))
    # A descendant that CLEARED its environment: admitted by descent, and the
    # marker read comes back False rather than None.
    table.add(BROWSER, CHAT, comm="node", cmdline=BROWSER_ARGV, env={"PATH": "/usr/bin"})
    # Reparented to init: this is the orphan the reaper hunts.
    table.add(ORPHAN, 1, comm="kiro-cli-chat", cmdline=CHAT_ARGV, env=dict(MARKER))
    # No marker of its own and NOT a gateway descendant: only the ppid chain to
    # a marker bearer can admit it.
    table.add(
        ORPHAN_CHILD, ORPHAN, comm="chrome-headless", cmdline=RENDERER_ARGV, env={"PATH": "/bin"}
    )
    # Outside the gateway tree, no marker: admitted by argv alone.
    table.add(RUNNER, OUTSIDER, comm="pytest", cmdline=RUNNER_ARGV, env={"PATH": "/bin"})
    # Not ours by any arm. Must never appear.
    table.add(STRANGER, OUTSIDER, comm="sshd", cmdline=STRANGER_ARGV, env={"PATH": "/bin"})
    return table


# -- family detection --------------------------------------------------------


def test_family_admits_every_arm_and_excludes_a_stranger(family: FakeProcTable) -> None:
    roster = family.scan()
    assert set(roster.nodes) == {GATEWAY, CHAT, BROWSER, ORPHAN, ORPHAN_CHILD, RUNNER}
    assert STRANGER not in roster.nodes
    assert roster.nodes[GATEWAY].family_reason == "descendant"
    assert roster.nodes[BROWSER].family_reason == "descendant"
    assert roster.nodes[ORPHAN].family_reason == "marker"
    assert roster.nodes[RUNNER].family_reason == "cmdline"


def test_reparented_to_init_is_reported_as_an_orphan(family: FakeProcTable) -> None:
    roster = family.scan()
    orphan = roster.nodes[ORPHAN]
    assert orphan.reparented is True
    assert orphan.orphan is True
    assert orphan.ppid == 1
    # The verdict names the reaper's own function, so a reader can check that
    # the view and the reaper are answering from the same rule.
    assert "_accepted_subreaper_pids" in orphan.orphan_rule
    assert roster.nodes[CHAT].reparented is False
    assert roster.nodes[CHAT].orphan is False
    assert roster.nodes[CHAT].orphan_rule == ""
    assert roster.orphan_count() == 1


def test_a_systemd_launched_service_is_reparented_but_not_an_orphan(
    tmp_path: Path,
) -> None:
    """Regression found against the real host, where a first draft read 11 orphans.

    A service ``systemd --user`` started has the user manager as its parent for
    its whole life, so reparenting alone cannot be the verdict. Ownership is the
    second half of it, and an unreadable marker fails closed.
    """
    table = FakeProcTable(tmp_path / "proc")
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    # A systemd-launched gateway: parented to init, family by argv, but it is the
    # SPAWNER and so carries no spawn marker of its own.
    table.add(1040, 1, comm="python3", cmdline=GATEWAY_ARGV, env={"PATH": "/bin"})
    # Marker unreadable at all (another uid, or a masked environ) -- also not a
    # verdict, because ownership is unproven.
    table.add(1041, 1, comm="kiro-cli-chat", cmdline=CHAT_ARGV, env=None)
    # Ours by marker AND reparented: this one is genuinely abandoned.
    table.add(1042, 1, comm="kiro-cli-chat", cmdline=CHAT_ARGV, env=dict(MARKER))

    roster = table.scan()
    for pid in (1040, 1041, 1042):
        assert roster.nodes[pid].reparented is True, pid
    assert roster.nodes[1040].orphan is False, "a systemd-launched gateway is not abandoned"
    assert roster.nodes[1041].orphan is False, "an unreadable marker fails closed"
    assert roster.nodes[1041].marker is None
    assert roster.nodes[1042].orphan is True
    assert roster.orphan_count() == 1


def test_ppid_chain_reaches_a_marker_bearer_without_its_own_marker(
    family: FakeProcTable,
) -> None:
    roster = family.scan()
    child = roster.nodes[ORPHAN_CHILD]
    assert child.marker is False, "the fixture cleared the marker from this process"
    assert child.family_reason == "marker-chain"
    assert child.ppid == ORPHAN


def test_orphan_reachability_comes_from_the_injected_reaper_predicate(
    family: FakeProcTable,
) -> None:
    seen: list[int] = []

    def unreachable(pid: int, cmdline: bytes, tracked: set[int]) -> bool:
        seen.append(pid)
        return True

    roster = family.scan(unreachable_orphan_fn=unreachable)
    # Asked only about orphans -- a process whose parent is alive is by
    # definition still reachable by its own session's teardown.
    assert seen == [ORPHAN]
    assert roster.nodes[ORPHAN].orphan_unreachable is True
    assert roster.nodes[CHAT].orphan_unreachable is False


# -- kind classification -----------------------------------------------------


def test_kind_classification(tmp_path: Path) -> None:
    table = FakeProcTable(tmp_path / "proc")
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    table.add(
        1010,
        GATEWAY,
        cmdline=nul_argv(b"/v/bin/python3", b"-m", b"kiro_crew.mcp_gateway.gatewayd", b"-s", b"/x"),
        env=dict(MARKER),
    )
    table.add(1011, GATEWAY, cmdline=CHAT_ARGV, env=dict(MARKER))
    table.add(1012, GATEWAY, cmdline=BROWSER_ARGV, env=dict(MARKER))
    table.add(1013, GATEWAY, cmdline=RUNNER_ARGV, env=dict(MARKER))
    table.add(
        1014, GATEWAY, cmdline=nul_argv(b"npx", b"@playwright/mcp", b"--headless"), env=dict(MARKER)
    )
    table.add(1015, GATEWAY, cmdline=nul_argv(b"/bin/sleep", b"600"), env=dict(MARKER))

    roster = table.scan()
    kinds = {pid: node.kind for pid, node in roster.nodes.items()}
    assert kinds[GATEWAY] == "gateway"
    assert kinds[1010] == "gatewayd"
    assert kinds[1011] == "chat"
    assert kinds[1012] == "browser"
    assert kinds[1013] == "test"
    assert kinds[1014] == "mcp-server"
    assert kinds[1015] == "other"
    assert set(kinds.values()) <= set(procs.KINDS)


def test_the_shipped_gateway_entry_form_is_classified_as_the_gateway(
    tmp_path: Path,
) -> None:
    """Regression: the live gateway on this host runs as ``python -m kiro_crew gateway``.

    Its argv names the PACKAGE, so neither ``kiro_crew.cli`` nor
    ``kiro_crew.__main__`` appears in it and the reaper's module markers do not
    match. A first draft classified a real running gateway as ``other``.
    """
    table = FakeProcTable(tmp_path / "proc")
    shipped = nul_argv(
        b"/rt/python3.12/bin/real/python3.12", b"-m", b"kiro_crew", b"gateway", b"--port", b"8476"
    )
    console = nul_argv(b"/home/u/.local/bin/kirocrew", b"gateway", b"--port", b"9000")
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    table.add(1030, GATEWAY, cmdline=shipped, env=dict(MARKER))
    table.add(1031, GATEWAY, cmdline=console, env=dict(MARKER))

    roster = table.scan()
    assert roster.nodes[1030].kind == "gateway"
    assert roster.nodes[1031].kind == "gateway"


def test_an_owner_label_promotes_a_harness_to_subagent(tmp_path: Path) -> None:
    table = FakeProcTable(tmp_path / "proc")
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    table.add(1020, GATEWAY, cmdline=CHAT_ARGV, env=dict(MARKER))
    table.add(1021, GATEWAY, cmdline=CHAT_ARGV, env=dict(MARKER))

    roster = table.scan(owner_map={1020: "Subagent runtime (chat-7)", 1021: "chat-7"})
    assert roster.nodes[1020].kind == "subagent"
    assert roster.nodes[1021].kind == "chat"
    assert roster.nodes[1021].owner == "chat-7"


def test_owner_falls_back_to_the_registry_and_then_to_the_tracked_root(
    tmp_path: Path,
) -> None:
    table = FakeProcTable(tmp_path / "proc")
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    table.add(CHAT, GATEWAY, cmdline=CHAT_ARGV, env=dict(MARKER))
    table.add(BROWSER, CHAT, cmdline=BROWSER_ARGV, env=dict(MARKER))

    roster = table.scan(tracked_owners_fn=lambda: {CHAT: GATEWAY})
    # A tracked runtime reports the gateway that tracks it...
    assert roster.nodes[CHAT].owner == f"gateway:{GATEWAY}"
    # ...and its descendant reports the tracked runtime it hangs off. Neither
    # spelling can be mistaken for a session key.
    assert roster.nodes[BROWSER].owner == f"runtime:{CHAT}"


# -- environment allowlist ---------------------------------------------------


def test_env_reports_only_allowlisted_keys_and_never_leaks_the_others(
    tmp_path: Path,
) -> None:
    secret_value = "s3cret-value-that-must-not-appear"
    table = FakeProcTable(tmp_path / "proc")
    table.add(
        GATEWAY,
        OUTSIDER,
        cmdline=GATEWAY_ARGV,
        env={
            session_pid.KIROCREW_SPAWNED_ENV: session_pid.KIROCREW_SPAWNED_VALUE,
            "KIROCREW_HOME": "/home/u/.kiro/crew",
            "TMPDIR": "/tmp",
            "KIROCREW_SCRATCH": "/scratch/run",
            "AWS_SECRET_ACCESS_KEY": secret_value,
            "KIROCREW_GATEWAY_TOKEN": secret_value,
        },
    )
    roster = table.scan()
    node = roster.nodes[GATEWAY]

    assert set(node.env) <= set(procs.ENV_ALLOWLIST)
    assert node.env["KIROCREW_HOME"] == "/home/u/.kiro/crew"
    assert node.env["TMPDIR"] == "/tmp"
    assert "KIROCREW_POD_ROOT" not in node.env, "an unset allowlisted key is omitted, not empty"

    rendered = json.dumps(procs.tree(roster, "flat", include_env=True))
    assert secret_value not in rendered
    assert "AWS_SECRET_ACCESS_KEY" not in rendered
    assert "KIROCREW_GATEWAY_TOKEN" not in rendered
    # Even the marker the family test depends on is not an output field.
    assert session_pid.KIROCREW_SPAWNED_ENV not in rendered


def test_env_is_omitted_entirely_unless_requested(family: FakeProcTable) -> None:
    roster = family.scan()
    default_view = procs.tree(roster, "flat")
    assert default_view["env_included"] is False
    assert all("env" not in row for row in default_view["nodes"])

    asked = procs.tree(roster, "flat", include_env=True)
    assert asked["env_included"] is True
    assert all("env" in row for row in asked["nodes"])


def test_unreadable_environ_degrades_instead_of_guessing(tmp_path: Path) -> None:
    table = FakeProcTable(tmp_path / "proc")
    # env=None means no environ file at all, which is what a process owned by
    # another uid looks like from here.
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=None)
    table.add(CHAT, GATEWAY, cmdline=CHAT_ARGV, env=dict(MARKER))

    roster = table.scan()
    assert roster.nodes[GATEWAY].env == {}
    assert any("environ unreadable" in note for note in roster.degraded_report())


def test_a_repeated_degradation_is_counted_once_not_narrated_per_process(
    tmp_path: Path,
) -> None:
    """Regression measured on a real host: 927 of 946 processes hit this one case.

    One line per process made the degraded list the largest thing in the output,
    so a route capped at 64 KB would spend the cap on repetition instead of on
    the tree. The count carries strictly more information in one line.
    """
    table = FakeProcTable(tmp_path / "proc")
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    for offset in range(12):
        table.add(1100 + offset, GATEWAY, cmdline=CHAT_ARGV, env=None)

    roster = table.scan()
    report = roster.degraded_report()
    unreadable = [note for note in report if "environ unreadable" in note]
    assert len(unreadable) == 1, f"expected one counted line, got {unreadable}"
    assert "12 occurrence(s)" in unreadable[0]
    # And the header carries the aggregated form, not 12 near-identical lines.
    view = procs.tree(roster, "flat")
    assert view["degraded"] == report


# -- redaction ---------------------------------------------------------------


def test_cmdline_credentials_are_redacted(tmp_path: Path) -> None:
    table = FakeProcTable(tmp_path / "proc")
    table.add(
        GATEWAY,
        OUTSIDER,
        cmdline=nul_argv(b"/v/bin/python3", b"-m", b"kiro_crew.cli", b"--key=AKIAIOSFODNN7EXAMPLE"),
        env=dict(MARKER),
    )
    roster = table.scan()
    rendered = roster.nodes[GATEWAY].cmdline
    assert "AKIAIOSFODNN7EXAMPLE" not in rendered
    assert "REDACTED" in rendered
    assert "kiro_crew.cli" in rendered, "redaction must not eat the identifying argv"


def test_every_emitted_string_passes_through_the_canonical_redactor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The security property, asserted on the wiring rather than on a regex.

    Patching the shim itself is what proves cmdline, cwd and environment values
    all leave through it; a regex-shaped assertion would only prove that one
    pattern happens to be covered today.
    """
    from kiro_crew.platform import context

    monkeypatch.setattr(context, "redact_via_context", lambda text: f"<scrubbed:{text}>")

    table = FakeProcTable(tmp_path / "proc")
    table.add(
        GATEWAY,
        OUTSIDER,
        cmdline=GATEWAY_ARGV,
        cwd="/home/u/project",
        env={**MARKER, "KIROCREW_HOME": "/home/u/.kiro/crew"},
    )
    roster = table.scan()
    node = roster.nodes[GATEWAY]
    assert node.cmdline.startswith("<scrubbed:")
    assert node.cwd == "<scrubbed:/home/u/project>"
    assert node.env["KIROCREW_HOME"] == "<scrubbed:/home/u/.kiro/crew>"


# -- deltas ------------------------------------------------------------------


def test_cpu_and_runq_are_none_on_a_first_scan(family: FakeProcTable) -> None:
    roster = family.scan()
    assert roster.nodes[CHAT].cpu_pct is None
    assert roster.nodes[CHAT].runq_wait_pct is None


def test_cpu_and_runq_percentages_are_deltas_between_two_scans(tmp_path: Path) -> None:
    table = FakeProcTable(tmp_path / "proc")
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    table.add(CHAT, GATEWAY, cmdline=CHAT_ARGV, env=dict(MARKER), utime=0, runq_ns=0)

    first = table.scan()
    # Move the first scan's clock back so the second scan sees a known 10s gap
    # instead of the microseconds two back-to-back scans really take.
    first.monotonic -= 10.0

    # 5 CPU-seconds of user time and 2 seconds of run-queue wait over 10s wall.
    table.add(
        CHAT,
        GATEWAY,
        cmdline=CHAT_ARGV,
        env=dict(MARKER),
        utime=table.clk_tck * 5,
        runq_ns=2_000_000_000,
    )
    second = table.scan(first)

    assert second.nodes[CHAT].cpu_pct == pytest.approx(50.0, abs=1.0)
    assert second.nodes[CHAT].runq_wait_pct == pytest.approx(20.0, abs=1.0)


def test_a_backwards_counter_reads_as_unknown_not_as_a_negative_rate(
    tmp_path: Path,
) -> None:
    """A recycled pid's counters belong to another process and must not be differenced."""
    table = FakeProcTable(tmp_path / "proc")
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    table.add(CHAT, GATEWAY, cmdline=CHAT_ARGV, env=dict(MARKER), utime=table.clk_tck * 50)

    first = table.scan()
    first.monotonic -= 10.0
    table.add(CHAT, GATEWAY, cmdline=CHAT_ARGV, env=dict(MARKER), utime=1)
    second = table.scan(first)

    assert second.nodes[CHAT].cpu_pct is None


# -- the GIL hint ------------------------------------------------------------


def test_gil_hint_fires_on_a_pinned_python_process_with_futex_waiters(
    tmp_path: Path,
) -> None:
    table = FakeProcTable(tmp_path / "proc")
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    argv = nul_argv(b"/v/bin/python3", b"worker.py")
    states = ("R", "S", "S")
    wchans = ("0", "futex_wait_queue_me", "futex_wait_queue_me")

    table.add(
        CHAT, GATEWAY, cmdline=argv, env=dict(MARKER), thread_states=states, thread_wchans=wchans
    )
    first = table.scan()
    first.monotonic -= 10.0
    table.add(
        CHAT,
        GATEWAY,
        cmdline=argv,
        env=dict(MARKER),
        thread_states=states,
        thread_wchans=wchans,
        utime=table.clk_tck * 9,
    )
    second = table.scan(first)

    node = second.nodes[CHAT]
    assert node.threads.futex_wait == 2
    assert node.threads.running == 1
    assert node.threads.count == 3
    assert node.gil_saturated_hint is True
    assert node.gil_hint_basis is not None
    assert "not a GIL measurement" in node.gil_hint_basis


def test_gil_hint_stays_off_without_futex_waiters(tmp_path: Path) -> None:
    table = FakeProcTable(tmp_path / "proc")
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    argv = nul_argv(b"/v/bin/python3", b"worker.py")
    states = ("R", "S")
    wchans = ("0", "poll_schedule_timeout")

    table.add(
        CHAT, GATEWAY, cmdline=argv, env=dict(MARKER), thread_states=states, thread_wchans=wchans
    )
    first = table.scan()
    first.monotonic -= 10.0
    table.add(
        CHAT,
        GATEWAY,
        cmdline=argv,
        env=dict(MARKER),
        thread_states=states,
        thread_wchans=wchans,
        utime=table.clk_tck * 9,
    )
    second = table.scan(first)

    assert second.nodes[CHAT].cpu_pct == pytest.approx(90.0, abs=1.0)
    assert second.nodes[CHAT].gil_saturated_hint is False
    assert second.nodes[CHAT].gil_hint_basis is None


# -- retention bounds --------------------------------------------------------


def test_a_long_cmdline_is_bounded_where_it_is_stored(tmp_path: Path) -> None:
    """AUTOSDE `a-bound-bounds-every-field-it-retains`: argv is not ours and is not small."""
    table = FakeProcTable(tmp_path / "proc")
    huge = nul_argv(b"/usr/local/bin/kiro-cli-chat", b"--flag=" + b"z" * 200_000)
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    table.add(CHAT, GATEWAY, cmdline=huge, env=dict(MARKER))

    roster = table.scan()
    stored = roster.nodes[CHAT].cmdline
    assert len(stored) <= procs.MAX_CMDLINE_CHARS
    assert stored.startswith("/usr/local/bin/kiro-cli-chat"), "argv0 survives the cut"
    assert roster.nodes[CHAT].kind == "chat", "a bounded cmdline still classifies"
    assert any("cut to" in n or "truncated" in n for n in roster.degraded_report())


def test_a_long_env_value_and_cwd_are_bounded(tmp_path: Path) -> None:
    table = FakeProcTable(tmp_path / "proc")
    table.add(
        GATEWAY,
        OUTSIDER,
        cmdline=GATEWAY_ARGV,
        cwd="/" + "d" * 5000,
        env={**MARKER, "KIROCREW_HOME": "/" + "h" * 5000},
    )
    roster = table.scan()
    node = roster.nodes[GATEWAY]
    assert node.cwd is not None and len(node.cwd) <= procs.MAX_PATH_CHARS
    assert len(node.env["KIROCREW_HOME"]) <= procs.MAX_PATH_CHARS


def test_the_population_cap_keeps_the_gateway_and_every_candidate_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cap that drops the orphans defeats the errand, so admission is prioritised."""
    monkeypatch.setattr(procs, "MAX_NODES", 5)
    table = FakeProcTable(tmp_path / "proc")
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    # Two reparented candidates, deliberately given the HIGHEST pids so a plain
    # sort would drop them first.
    for pid in (9001, 9002):
        table.add(pid, 1, comm="kiro-cli-chat", cmdline=CHAT_ARGV, env=dict(MARKER))
    # Plenty of ordinary members with lower pids.
    for offset in range(10):
        table.add(2000 + offset, GATEWAY, cmdline=CHAT_ARGV, env=dict(MARKER))

    roster = table.scan()
    assert len(roster.nodes) == 5
    assert GATEWAY in roster.nodes, "the gateway is never dropped"
    assert 9001 in roster.nodes and 9002 in roster.nodes, "candidate orphans are never dropped"
    assert any("node cap 5 reached" in n for n in roster.degraded_report())
    assert any("omitted" in n for n in roster.degraded_report())


def test_a_recycled_pid_gets_no_rate_and_reports_a_death_and_a_birth(
    tmp_path: Path,
) -> None:
    """A pid is not an identity, and on a busy host numbers get reused.

    The old guard only caught a recycled pid whose counter went BACKWARDS. When
    the replacement happens to hold a higher cumulative total, differencing the
    two produces a confident wrong rate -- and a diff keyed on pid alone loses
    both the death and the birth.
    """
    table = FakeProcTable(tmp_path / "proc")
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    table.add(CHAT, GATEWAY, cmdline=CHAT_ARGV, env=dict(MARKER), utime=100, age_secs=900.0)
    first = table.scan()
    first.monotonic -= 10.0

    # SAME pid, different process: a later start time and a higher cumulative
    # total, which is exactly the case the backwards-counter guard misses.
    later = FakeProcTable(tmp_path / "proc2")
    later.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    later.add(CHAT, GATEWAY, cmdline=CHAT_ARGV, env=dict(MARKER), utime=100_000, age_secs=2.0)
    second = later.scan(first)

    assert second.nodes[CHAT].cpu_pct is None, "no rate across two different processes"
    assert second.nodes[CHAT].runq_wait_pct is None
    assert any("pid recycled" in n for n in second.degraded_report())

    diff = procs.roster_diff(first, second)
    assert [row["pid"] for row in diff["born"]] == [CHAT]
    assert [row["pid"] for row in diff["died"]] == [CHAT]
    assert diff["recycled_pids"] == [CHAT]


def test_an_unchanged_process_still_gets_its_rate(tmp_path: Path) -> None:
    """The identity gate must not suppress the ordinary case it exists to protect."""
    table = FakeProcTable(tmp_path / "proc")
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    table.add(CHAT, GATEWAY, cmdline=CHAT_ARGV, env=dict(MARKER), utime=0, age_secs=500.0)
    first = table.scan()
    first.monotonic -= 10.0
    table.add(
        CHAT,
        GATEWAY,
        cmdline=CHAT_ARGV,
        env=dict(MARKER),
        utime=table.clk_tck * 5,
        age_secs=500.0,
    )
    second = table.scan(first)
    assert second.nodes[CHAT].cpu_pct == pytest.approx(50.0, abs=1.0)
    assert not any("pid recycled" in n for n in second.degraded_report())


def test_a_credential_straddling_the_bound_is_still_redacted(tmp_path: Path) -> None:
    """Order is the security property: redact first, bound second.

    Bounding before redacting would cut the pattern the redactor matches on, so
    the surviving prefix would carry the secret in clear. Cutting afterwards can
    only ever drop text that is already scrubbed.
    """
    table = FakeProcTable(tmp_path / "proc")
    # Pad so the credential sits astride MAX_CMDLINE_CHARS.
    pad = b"x" * (procs.MAX_CMDLINE_CHARS - 10)
    argv = nul_argv(b"/v/bin/python3", b"-m", b"kiro_crew.cli", pad + b"--key=AKIAIOSFODNN7EXAMPLE")
    table.add(GATEWAY, OUTSIDER, cmdline=argv, env=dict(MARKER))

    roster = table.scan()
    stored = roster.nodes[GATEWAY].cmdline
    assert len(stored) <= procs.MAX_CMDLINE_CHARS
    assert "AKIAIOSFODNN7EXAMPLE" not in stored
    # Neither may a recognisable fragment of it survive the cut.
    for cut in range(6, 20):
        assert "AKIAIOSFODNN7EXAMPLE"[:cut] not in stored


def test_a_credential_straddling_the_env_bound_is_still_redacted(tmp_path: Path) -> None:
    table = FakeProcTable(tmp_path / "proc")
    pad = "p" * (procs.MAX_PATH_CHARS - 8)
    table.add(
        GATEWAY,
        OUTSIDER,
        cmdline=GATEWAY_ARGV,
        env={**MARKER, "KIROCREW_HOME": pad + "AKIAIOSFODNN7EXAMPLE"},
    )
    stored = table.scan().nodes[GATEWAY].env["KIROCREW_HOME"]
    assert len(stored) <= procs.MAX_PATH_CHARS
    assert "AKIAIOSFODNN7EXAMPLE" not in stored


def test_a_chain_deeper_than_the_recursion_limit_still_renders(tmp_path: Path) -> None:
    """The view has to work when the host is unwell, so depth must not crash it.

    A chain longer than CPython's frame limit is what a per-level recursive
    build cannot survive. The nesting is built flat and linked by reference, so
    depth costs no frames.
    """
    depth = sys.getrecursionlimit() + 200
    table = FakeProcTable(tmp_path / "proc")
    table.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    parent = GATEWAY
    for offset in range(depth):
        pid = 20_000 + offset
        table.add(pid, parent, cmdline=CHAT_ARGV, env=dict(MARKER), fds=0)
        parent = pid

    roster = table.scan()
    view = procs.tree(roster)  # must not raise RecursionError
    assert len(view["roots"]) == 1
    # Walk down iteratively and confirm the chain really is that deep.
    node = view["roots"][0]
    walked = 1
    while "children" in node:
        node = node["children"][0]
        walked += 1
    assert walked == min(depth + 1, procs.MAX_NODES)


# -- roster_diff -------------------------------------------------------------


def test_roster_diff_reports_born_and_died(family: FakeProcTable, tmp_path: Path) -> None:
    before = family.scan()

    later = FakeProcTable(tmp_path / "proc2")
    later.add(GATEWAY, OUTSIDER, cmdline=GATEWAY_ARGV, env=dict(MARKER))
    later.add(CHAT, GATEWAY, cmdline=CHAT_ARGV, env=dict(MARKER))
    later.add(1099, CHAT, cmdline=RUNNER_ARGV, env=dict(MARKER), rss_kb=7777)
    after = later.scan()

    diff = procs.roster_diff(before, after)
    assert diff["baseline"] is False
    assert [row["pid"] for row in diff["born"]] == [1099]
    assert [row["pid"] for row in diff["died"]] == [BROWSER, ORPHAN, ORPHAN_CHILD, RUNNER]
    assert diff["born"][0]["rss_kb"] == 7777
    assert diff["born"][0]["kind"] == "test"
    assert diff["died_count"] == 4


def test_roster_diff_first_sample_is_a_baseline_not_a_burst(family: FakeProcTable) -> None:
    roster = family.scan()
    diff = procs.roster_diff(None, roster)
    assert diff["baseline"] is True
    assert diff["born"] == []
    assert diff["born_count"] == 0
    assert diff["total"] == len(roster.nodes)


# -- views -------------------------------------------------------------------


def test_tree_nests_children_under_their_parent(family: FakeProcTable) -> None:
    view = procs.tree(family.scan())
    roots = {row["pid"]: row for row in view["roots"]}
    assert set(roots) == {GATEWAY, ORPHAN, RUNNER}
    chat = roots[GATEWAY]["children"][0]
    assert chat["pid"] == CHAT
    assert chat["children"][0]["pid"] == BROWSER
    assert roots[ORPHAN]["children"][0]["pid"] == ORPHAN_CHILD


def test_a_filter_keeps_the_ancestor_chain_of_a_match(family: FakeProcTable) -> None:
    # Two members classify as browsers: the playwright daemon under the chat
    # runtime, and the renderer under the orphan. Both sit two levels below a
    # root, so both exercise the ancestor rule.
    view = procs.tree(family.scan(), "tree", kind="browser")
    assert view["matched"] == 2
    roots = {row["pid"]: row for row in view["roots"]}
    assert set(roots) == {GATEWAY, ORPHAN}
    assert roots[GATEWAY]["matched"] is False, "an ancestor is carried, not claimed as a match"

    chat = roots[GATEWAY]["children"][0]
    assert chat["pid"] == CHAT
    assert chat["matched"] is False
    assert chat["children"][0]["pid"] == BROWSER
    assert chat["children"][0]["matched"] is True

    renderer = roots[ORPHAN]["children"][0]
    assert renderer["pid"] == ORPHAN_CHILD
    assert renderer["matched"] is True
    assert "children" not in renderer, "a leaf carries no children key"


def test_orphan_only_and_flat_format(family: FakeProcTable) -> None:
    view = procs.tree(family.scan(), "flat", orphan_only=True)
    assert [row["pid"] for row in view["nodes"]] == [ORPHAN]
    assert view["orphans"] == 1
    assert view["total"] == 6


def test_tree_header_reports_counts_and_the_allowlist(family: FakeProcTable) -> None:
    view = procs.tree(family.scan(), "flat")
    assert view["counts_by_kind"] == {"gateway": 1, "chat": 2, "browser": 2, "test": 1}
    assert view["env_allowlist"] == list(procs.ENV_ALLOWLIST)
    assert view["gateway_pid"] == GATEWAY


# -- platform branches -------------------------------------------------------


def test_darwin_uses_the_sanctioned_ps_snapshot() -> None:
    """macOS has no ``/proc``; the ps snapshot carries parent edges only.

    It reads the snapshot in `platform_compat`, NOT the one in `acp.runtime`:
    the agent-SDK import boundary is a blocking gate and forbids application
    code from reaching the ACP layer, whichever helper looks handier.
    """
    parents = {GATEWAY: OUTSIDER, CHAT: GATEWAY, BROWSER: CHAT, OUTSIDER: 1, 4242: 1}

    roster = procs.scan(
        platform_name="darwin",
        gateway_pid=GATEWAY,
        ps_snapshot_fn=lambda: parents,
    )

    assert set(roster.nodes) == {GATEWAY, CHAT, BROWSER}
    assert 4242 not in roster.nodes, "a sibling outside the gateway tree is not family"
    assert roster.nodes[GATEWAY].kind == "gateway"
    assert roster.nodes[CHAT].ppid == GATEWAY
    # Everything the snapshot cannot carry is null, and the roster says so --
    # including rss, which this snapshot reads no column for.
    assert roster.nodes[CHAT].state is None
    assert roster.nodes[CHAT].cmdline == ""
    assert roster.nodes[CHAT].env == {}
    assert roster.nodes[CHAT].rss_kb is None
    assert roster.nodes[CHAT].threads.count is None
    assert any("carries pid/ppid only" in note for note in roster.degraded_report())
    # orphans: 0 on macOS must read as UNMEASURED, never as a measured zero.
    assert roster.orphan_count() == 0
    assert any("orphan detection did not run" in note for note in roster.degraded_report())
    assert any("UNMEASURED" in note for note in roster.degraded_report())


def test_the_darwin_adapter_reads_the_real_snapshot_row_field(monkeypatch) -> None:
    """Pins the adapter against the REAL row type, not an invented shape.

    The test above injects `ps_snapshot_fn`, which sits ABOVE this adapter, so it
    proves nothing about the field name the adapter reads. This one patches the
    platform snapshot itself with genuine `_PosixProcessSnapshotRow` values, so a
    field rename fails here instead of emptying the family on macOS only.
    """
    row = platform_compat._PosixProcessSnapshotRow
    snapshot = {
        GATEWAY: row(ppid=OUTSIDER, start_time=None),
        CHAT: row(ppid=GATEWAY, start_time=None),
    }
    monkeypatch.setattr(platform_compat, "_posix_process_snapshot", lambda: snapshot, raising=True)

    edges = procs._darwin_parent_edges()

    assert edges == {GATEWAY: OUTSIDER, CHAT: GATEWAY}, "the adapter must read `ppid`"


def test_darwin_reports_an_unavailable_ps_rather_than_an_empty_family() -> None:
    roster = procs.scan(platform_name="darwin", gateway_pid=GATEWAY, ps_snapshot_fn=lambda: None)
    assert roster.nodes == {}
    assert any("ps snapshot unavailable" in note for note in roster.degraded_report())


def test_windows_returns_an_empty_roster_with_a_reason() -> None:
    roster = procs.scan(platform_name="win32", gateway_pid=GATEWAY)
    assert roster.nodes == {}
    assert roster.platform == "win32"
    assert any("windows" in note for note in roster.degraded)
    view = procs.tree(roster)
    assert view["total"] == 0
    assert view["roots"] == []


def test_an_unreadable_proc_root_degrades_rather_than_raising(tmp_path: Path) -> None:
    roster = procs.scan(
        proc_root=tmp_path / "absent",
        platform_name="linux",
        gateway_pid=GATEWAY,
    )
    assert roster.nodes == {}
    assert any("no process entries readable" in note for note in roster.degraded)


# -- the session_pid accessor ------------------------------------------------


def test_tracked_agent_pid_owners_reads_both_registry_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_file = tmp_path / "kiro_session_pids.txt"
    child_file = tmp_path / "kiro_pids.txt"
    # <gateway>:<child>[:token]
    session_file.write_text("1000:1001:abc\n1000:1002\n", encoding="utf-8")
    # <child>:<parent>[:token], plus a legacy bare-PID line
    child_file.write_text("2001:1001:tok\n2002:1001\n3003\n", encoding="utf-8")

    monkeypatch.setattr(session_pid, "_session_pid_file_path", lambda: session_file)
    monkeypatch.setattr(session_pid, "_pid_file_path", lambda: child_file)

    owners = session_pid.tracked_agent_pid_owners()
    assert owners == {1001: 1000, 1002: 1000, 2001: 1001, 2002: 1001}
    assert 3003 not in owners, "a bare-PID line records no owner and must not invent one"


def test_tracked_agent_pid_owners_prefers_the_session_entry_on_a_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_file = tmp_path / "kiro_session_pids.txt"
    child_file = tmp_path / "kiro_pids.txt"
    session_file.write_text("1000:5000:abc\n", encoding="utf-8")
    child_file.write_text("5000:4321\n", encoding="utf-8")

    monkeypatch.setattr(session_pid, "_session_pid_file_path", lambda: session_file)
    monkeypatch.setattr(session_pid, "_pid_file_path", lambda: child_file)

    # The session entry is the one that names a gateway, so it wins.
    assert session_pid.tracked_agent_pid_owners() == {5000: 1000}


def test_tracked_agent_pid_owners_is_empty_when_the_registry_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_pid, "_session_pid_file_path", lambda: tmp_path / "nope-a.txt")
    monkeypatch.setattr(session_pid, "_pid_file_path", lambda: tmp_path / "nope-b.txt")
    assert session_pid.tracked_agent_pid_owners() == {}


# -- read-only posture -------------------------------------------------------


def test_the_module_exposes_nothing_that_signals_or_writes() -> None:
    """A ratchet on the contract: reclaim stays with the reaper, not with this view."""
    exported = {name for name in dir(procs) if not name.startswith("__")}
    for forbidden in ("kill", "signal", "terminate", "reap"):
        assert not any(forbidden in name.lower() for name in exported), forbidden
    source = Path(procs.__file__).read_text(encoding="utf-8")
    for banned in ("os.kill", "pidfd_send_signal", "killpg", "SIGKILL", "SIGTERM"):
        assert banned not in source, banned
