"""Every transient scope ``cgroup_scope_argv`` builds asks the manager to collect it.

``systemd-run --scope`` registers its own pid with the user manager and waits for the
manager to attach that pid before it execs the target. When the wrapper is killed
during that wait (a caller's timeout), the manager later finds no pid to attach and the
scope fails with ``Result=resources``; an OOM kill fails it too. A failed transient unit
stays loaded until ``reset-failed`` unless its ``CollectMode`` is
``inactive-or-failed``, which is what ``--collect`` sets. These tests pin that flag on
the shared builder, that the recognizer and the named-scope step keep it, and that a
run of failed spawns leaves no unit behind.

The last test drives a real user manager. It is opt-in (``KIROCREW_SCOPE_COLLECT_REAL=1``)
and starts three short scopes of its own in the manager's default slice, each
OOM-killed by a 16 MiB ceiling, then reads back that the manager unloaded every one of
them. Run it directly with ``python test/test_scope_collect_policy.py`` on a host whose
user bus is reachable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
import sandbox_argv_helpers

from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

pytestmark = pytest.mark.usefixtures("systemd_run_resolvable")

systemd_run_resolvable = sandbox_argv_helpers.systemd_run_resolvable

REAL_SCOPE_ENV = "KIROCREW_SCOPE_COLLECT_REAL"


def _wrap(argv: list[str], *, cpu: bool = True) -> list[str]:
    import kiro_crew.sandbox as sb

    sb._CGROUP_SCOPE_PROBE = None
    sb._CGROUP_WARNED = False
    with (
        patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
        patch("kiro_crew.sandbox._cgroup_limits_from_config", return_value=(512, 4096, 50, 150)),
        patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=cpu),
        patch("kiro_crew.sandbox._reconcile_slice_memory_high_off_thread"),
    ):
        return sb.cgroup_scope_argv(argv)


def _wrapper_options(argv: list[str]) -> list[str]:
    """The systemd-run options: everything before the ``--`` that ends them."""
    return argv[1 : argv.index("--")]


@pytest.mark.parametrize("cpu", [True, False])
def test_every_scope_is_collected_and_keeps_every_limit(cpu: bool) -> None:
    out = _wrap(["kiro-cli", "acp"], cpu=cpu)
    options = _wrapper_options(out)
    assert options.count("--collect") == 1
    assert "--scope" in options and "--user" in options
    # The limits are unchanged: collection frees the unit record after the scope
    # ends and changes nothing the kernel enforces while it runs.
    for prop in ("TasksMax=512", "MemoryMax=4096M", "MemorySwapMax=0"):
        assert prop in options
    assert any(p.startswith("MemoryHigh=") for p in options)
    assert ("CPUWeight=50" in options) is cpu
    assert ("CPUQuota=150%" in options) is cpu
    # Before the separator, so systemd-run reads it as its own option and never
    # hands it to the wrapped command.
    assert out[out.index("--") + 1 :] == ["kiro-cli", "acp"]


def test_named_scope_keeps_collection_and_names_the_unit() -> None:
    import kiro_crew.sandbox as sb

    wrapped = _wrap(["kiro-cli", "acp"])
    named = sb.name_scope_unit(wrapped, "abc123")
    assert named is not wrapped
    options = _wrapper_options(named)
    assert "--collect" in options
    assert options[options.index("--unit") + 1] == "kirocrew-rt-abc123.scope"
    assert named[named.index("--") + 1 :] == ["kiro-cli", "acp"]


def test_unwrapped_host_gets_no_flag() -> None:
    """A host without cgroup delegation runs the bare command: there is no scope to collect."""
    import kiro_crew.sandbox as sb

    sb._CGROUP_SCOPE_PROBE = None
    sb._CGROUP_WARNED = False
    with patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(False, "not Linux")):
        assert sb.cgroup_scope_argv(["git", "status"]) == ["git", "status"]


@dataclass
class _UserManager:
    """The part of a systemd user manager that decides whether a failed scope stays loaded.

    ``systemd-run`` turns ``--collect`` into ``CollectMode=inactive-or-failed`` on the
    transient unit; without it the unit gets the default ``inactive``, which unloads a
    unit that ended cleanly and keeps one that failed (systemd.unit(5), CollectMode=).
    """

    loaded: dict[str, str] = field(default_factory=dict)
    started: int = 0

    def start_scope_whose_pid_vanished(self, argv: list[str]) -> None:
        options = argv[1 : argv.index("--")]
        mode = "inactive-or-failed" if "--collect" in options else "inactive"
        self.started += 1
        unit = f"run-p{1000 + self.started}-i{self.started}.scope"
        # The wrapper was killed before the manager attached it: "No PIDs left to
        # attach", Result=resources, so the scope enters the failed state.
        if mode != "inactive-or-failed":
            self.loaded[unit] = "failed"

    def failed_count(self) -> int:
        return sum(1 for state in self.loaded.values() if state == "failed")


def test_repeated_failed_spawns_do_not_grow_the_failed_unit_count() -> None:
    manager = _UserManager()
    for _ in range(25):
        manager.start_scope_whose_pid_vanished(_wrap(["systemctl", "--user", "show", "x"]))
    assert manager.started == 25
    assert manager.failed_count() == 0


def test_the_model_counts_the_failures_a_scope_without_collection_leaves() -> None:
    """Control for the test above: the same failures without ``--collect`` stay loaded."""
    manager = _UserManager()
    for _ in range(25):
        argv = [a for a in _wrap(["systemctl", "--user", "show", "x"]) if a != "--collect"]
        manager.start_scope_whose_pid_vanished(argv)
    assert manager.failed_count() == 25


# --- opt-in: a real user manager --------------------------------------------------


def _real_skip_reason(*, opted_in: bool = False) -> str | None:
    if not opted_in and os.environ.get(REAL_SCOPE_ENV) != "1":
        return f"opt-in real-manager exercise; set {REAL_SCOPE_ENV}=1"
    if sys.platform != "linux":
        return "not Linux"
    if shutil.which("systemctl") is None:
        return "systemctl not found"
    probe = subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        **UTF8_TEXT,
    )
    # ``offline`` and ``unknown`` are what systemctl prints where no manager runs.
    if probe.stdout.strip() in {"", "offline", "unknown"}:
        return f"user manager unreachable: {probe.stderr.strip()[:200] or probe.stdout.strip()}"
    return None


def _load_state(unit: str) -> str:
    return subprocess.run(
        ["systemctl", "--user", "show", "-p", "LoadState", "--value", unit],
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        **UTF8_TEXT,
    ).stdout.strip()


def run_real_collect_exercise(count: int = 3) -> dict[str, str]:
    """Start *count* real scopes that the kernel OOM-kills; return each unit's LoadState.

    The argv is the shared builder's own, named through ``name_scope_unit``, with its
    ``--slice`` dropped (so the scopes land in the manager's default slice and no new
    slice unit is created) and a 16 MiB ``MemoryMax`` added so each child fails the same
    way every time. No unit outlives the call: a collected scope unloads itself, and the
    caller asserts that it did. The slice reconcile the builder starts is skipped: it
    writes to the instance's agents slice, which this exercise does not use.
    """
    import kiro_crew.sandbox as sb

    sb._CGROUP_SCOPE_PROBE = None
    tag = uuid.uuid4().hex[:10]
    states: dict[str, str] = {}
    hog = "b = bytearray(256 << 20)\nfor i in range(0, len(b), 4096): b[i] = 1\n"
    for i in range(count):
        token = f"collecttest{tag}{i}"
        with patch("kiro_crew.sandbox._reconcile_slice_memory_high_off_thread"):
            wrapped = sb.cgroup_scope_argv([sys.executable, "-c", hog])
        argv = sb.name_scope_unit(wrapped, token)
        assert "--collect" in argv, "the host did not wrap: no scope to observe"
        at = argv.index("--")
        options = [a for a in argv[:at] if not a.startswith("--slice=")]
        argv = [*options, "-p", "MemoryMax=16M", "-p", "MemoryHigh=infinity", *argv[at:]]
        subprocess.run(argv, capture_output=True, timeout=FALLBACK_TIMEOUT_SECS)
        unit = sb.scope_unit_name(token)
        assert unit is not None
        # The manager handles the scope's exit after systemd-run returns, so the
        # unit can still read ``loaded`` for a moment.
        wait_until(
            lambda: _load_state(unit) == "not-found",
            timeout=FALLBACK_TIMEOUT_SECS,
            describe=lambda: f"{unit} LoadState={_load_state(unit)!r}",
        )
        states[unit] = _load_state(unit)
    return states


_REAL_SKIP = _real_skip_reason()


@pytest.mark.skipif(_REAL_SKIP is not None, reason=f"real manager test skipped: {_REAL_SKIP}")
@pytest.mark.usefixtures("real_user_session")
def test_real_failed_scopes_are_unloaded() -> None:
    states = run_real_collect_exercise()
    assert set(states.values()) == {"not-found"}, states


if __name__ == "__main__":
    reason = _real_skip_reason(opted_in=True)
    if reason is not None:
        sys.exit(f"cannot run: {reason}")
    result = run_real_collect_exercise()
    print(result)
    sys.exit(0 if set(result.values()) == {"not-found"} else 1)
