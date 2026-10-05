"""Tests for the namespace launcher's Gradle-daemon guard.

Left to itself, Gradle keeps a daemon running after the sandboxed command that
started it exits. That daemon holds the sandbox's mount namespace open with the
credential paths still masked, and keeps the inherited seccomp filter and
emptied capability bounding set. Nothing in the launcher changes what Gradle
keys its daemon context on, so a later build run *outside* the sandbox matches
that context and adopts the daemon. The launcher therefore disables the daemon
for builds it starts, so none is left behind.

The guard lives in ``scrub_env``, the launcher stage that writes the environment
the agent inherits (``kiro_crew.sandbox_launcher_program``). These tests run that
stage on a plain dict handed to a ``Launch``, so the guard that runs is the shipped
one and the process environment is never touched; a whole child run
(``run_child``) with a recording libc shows the flag is in place when the agent is
exec'd, and the plan ``_build_launcher_script`` renders for every tier reaches it.
"""

from __future__ import annotations

import sys

import pytest
from test_sandbox_launcher_program import RecordingLibc, payload, rendered_payload

import kiro_crew.sandbox as sandbox_mod
from kiro_crew import sandbox_launcher_program
from kiro_crew.sandbox import _build_launcher_script

program = sandbox_launcher_program

# The launcher's payload carries POSIX-only ``os.getuid``/``os.getgid`` (the
# namespace launcher is Linux-only), so building one raises AttributeError on
# Windows. Same skip as test_sandbox_argv.py.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the launcher payload uses POSIX-only os.getuid",
)

_FLAG = "-Dorg.gradle.daemon=false"
_DIRECTIVE = "-Dorg.gradle.daemon="
_SANDBOX_LEVELS = ("strict", "standard", "cc")


@pytest.fixture(autouse=True)
def _no_host_ssh_probe(monkeypatch):
    """``_build_launcher_script`` asks the HOST's ``ssh -V`` for accept-new support.

    None of these tests is about that probe, and a real ssh spawned from the test
    process is a host dependency the launcher must not vary with. Pinned at the
    module seam ``_build_launcher_script`` reads, so no binary runs.
    """
    monkeypatch.setattr(sandbox_mod, "_ssh_supports_accept_new", lambda: True)


def _effective_daemon_directive(opts: str) -> str | None:
    """Return the LAST ``-Dorg.gradle.daemon=`` directive in *opts*, or None.

    Duplicate ``-D`` resolves last-wins in the JVM, so this -- not substring
    presence -- is what decides whether the daemon is actually disabled.
    """
    found = [tok for tok in opts.split() if tok.startswith(_DIRECTIVE)]
    return found[-1] if found else None


def _scrubbed(environ: dict[str, str], plan: dict | None = None) -> dict[str, str]:
    """The environment the launcher's ``scrub_env`` leaves for the agent, from *environ*."""
    run = program.Launch(plan or payload(), RecordingLibc(), environ=dict(environ))
    program.scrub_env(run)
    return run.environ


@_POSIX_ONLY
class TestGradleDaemonGuardBehaviour:
    def test_sets_the_flag_when_gradle_opts_is_absent(self):
        env = _scrubbed({})
        # No leading space: GRADLE_OPTS is parsed as JVM args, and a bare
        # leading separator is noise in every log that echoes it back.
        assert env["GRADLE_OPTS"] == _FLAG

    def test_sets_the_flag_when_gradle_opts_is_empty(self):
        env = _scrubbed({"GRADLE_OPTS": ""})
        assert env["GRADLE_OPTS"] == _FLAG

    def test_preserves_an_explicit_caller_value(self):
        # Appending rather than assigning keeps the caller's tuning; clobbering
        # GRADLE_OPTS would silently drop a heap size the build depends on.
        env = _scrubbed({"GRADLE_OPTS": "-Xmx2g"})
        assert env["GRADLE_OPTS"] == f"-Xmx2g {_FLAG}"

    def test_does_not_add_the_flag_twice(self):
        already = f"-Xmx2g {_FLAG}"
        env = _scrubbed({"GRADLE_OPTS": already})
        assert env["GRADLE_OPTS"] == already
        assert env["GRADLE_OPTS"].count(_FLAG) == 1

    def test_the_flag_wins_over_an_inherited_daemon_true(self):
        # A later -D beats an earlier one in JVM argument order, so appending is
        # what neutralises an inherited -Dorg.gradle.daemon=true.
        env = _scrubbed({"GRADLE_OPTS": "-Dorg.gradle.daemon=true"})
        opts = env["GRADLE_OPTS"]
        assert opts.index("-Dorg.gradle.daemon=true") < opts.index(_FLAG)

    def test_the_flag_wins_when_a_true_directive_comes_last(self):
        # GRADLE_OPTS carrying BOTH forms with =true LAST. Measured on openjdk 21:
        # duplicate -D resolves last-wins, so the mere PRESENCE of the false form
        # does not disable the daemon -- the guard must key on the effective last
        # directive or the daemon stays enabled and outlives the sandbox.
        env = _scrubbed({"GRADLE_OPTS": f"{_FLAG} -Dorg.gradle.daemon=true"})
        assert _effective_daemon_directive(env["GRADLE_OPTS"]) == _FLAG

    def test_does_not_append_when_the_false_flag_is_already_effective(self):
        # Guards the OPPOSITE direction from the test above: an unconditional
        # append would accrete a duplicate flag on every nested invocation.
        already = f"{_FLAG} -Xmx2g"
        env = _scrubbed({"GRADLE_OPTS": already})
        assert env["GRADLE_OPTS"] == already
        assert env["GRADLE_OPTS"].count(_FLAG) == 1


@_POSIX_ONLY
class TestGradleDaemonGuardPlacement:
    def test_the_guard_is_emitted_at_every_sandbox_level(self):
        # Every level unshares a mount namespace and masks credential paths, so
        # a daemon left behind by any of them is adoptable from outside. The plan
        # each level's launcher carries is the one its scrub runs with.
        for level in _SANDBOX_LEVELS:
            plan = rendered_payload(_build_launcher_script(level))
            assert plan["sandbox_level"] == level
            assert _scrubbed({}, plan)["GRADLE_OPTS"] == _FLAG, level

    def test_the_guard_runs_before_the_exec(self, monkeypatch: pytest.MonkeyPatch):
        # Set after exec, the flag would never reach the build. The environment is
        # read at the moment the child execs the agent.
        # The seccomp stage refuses a machine it has no syscall table for; the
        # recording libc installs nothing, so the machine is one the table carries.
        monkeypatch.setattr(program._plat, "machine", lambda: "x86_64")
        at_exec: list[dict[str, str]] = []
        run = program.Launch(
            payload(),
            RecordingLibc(),
            environ={"GRADLE_OPTS": "-Dorg.gradle.daemon=true"},
            execvp=lambda file, argv: at_exec.append(dict(run.environ)),
        )
        program.run_child(run, ["/usr/bin/gradle", "build"])
        assert len(at_exec) == 1, "the child never reached the exec"
        assert _effective_daemon_directive(at_exec[0]["GRADLE_OPTS"]) == _FLAG

    def test_the_generated_launcher_still_compiles_at_every_level(self):
        # The plan is substituted into the program as a Python literal, so a value
        # spelled the JSON way (true, null) would compile and then die at run time;
        # the literal parse is what refuses that.
        for level in _SANDBOX_LEVELS:
            script = _build_launcher_script(level)
            compile(script, "<launcher>", "exec")
            assert rendered_payload(script)["sandbox_level"] == level
