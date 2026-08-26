"""Tests for the namespace launcher's Gradle-daemon guard.

Left to itself, Gradle keeps a daemon running after the sandboxed command that
started it exits. That daemon holds the sandbox's mount namespace open with the
credential paths still masked, and keeps the inherited seccomp filter and
emptied capability bounding set. Nothing in the launcher changes what Gradle
keys its daemon context on, so a later build run *outside* the sandbox matches
that context and adopts the daemon. The launcher therefore disables the daemon
for builds it starts, so none is left behind.

These tests assert on the launcher source that ``_build_launcher_script``
returns -- the launcher's ``main()`` is a literal segment of that f-string, so
the guard is extractable from the returned string. This matches the house idiom
in ``test/test_sandbox_argv.py``.
"""

from __future__ import annotations

import ast
import sys
import types

import pytest

from kiro_crew.sandbox import _build_launcher_script

# ``_build_launcher_script`` calls POSIX-only ``os.getuid``/``os.getgid`` (the
# namespace launcher is Linux-only), so building it raises AttributeError on
# Windows. Same skip as test_sandbox_argv.py. See #2041.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="_build_launcher_script uses POSIX-only os.getuid (#2041)",
)

_FLAG = "-Dorg.gradle.daemon=false"
_DIRECTIVE = "-Dorg.gradle.daemon="
_SANDBOX_LEVELS = ("strict", "standard", "cc")


def _effective_daemon_directive(opts: str) -> str | None:
    """Return the LAST ``-Dorg.gradle.daemon=`` directive in *opts*, or None.

    Duplicate ``-D`` resolves last-wins in the JVM, so this -- not substring
    presence -- is what decides whether the daemon is actually disabled.
    """
    found = [tok for tok in opts.split() if tok.startswith(_DIRECTIVE)]
    return found[-1] if found else None


def _guard_node(script: str) -> ast.If:
    """Return the launcher's Gradle-daemon guard node, or raise if absent.

    Locating the guard by AST rather than by line offset keeps the extraction
    independent of the template's indentation and of unrelated edits above it.
    Raising when the guard is gone is deliberate: a refactor that drops the
    guard turns this module red instead of leaving it silently unreachable.
    """
    for node in ast.walk(ast.parse(script)):
        if isinstance(node, ast.If) and _FLAG in ast.unparse(node.test):
            return node
    raise AssertionError(
        f"no guard testing {_FLAG!r} in the generated launcher: a Gradle daemon "
        "started inside the sandbox can now outlive it and poison a later "
        "unsandboxed build"
    )


def _run_guard(script: str, environ: dict[str, str]) -> dict[str, str]:
    """Execute the extracted guard against *environ* and return the result.

    ``os`` is stubbed with a plain dict so the guard cannot touch the real
    process environment.
    """
    guard = compile(ast.unparse(_guard_node(script)), "<guard>", "exec")
    env = dict(environ)
    # exec runs the SHIPPED guard source rather than a re-implementation, which is
    # what lets this test detect drift; `os` is stubbed, so nothing real is reached.
    exec(  # noqa: S102  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
        guard, {"os": types.SimpleNamespace(environ=env)}
    )
    return env


@_POSIX_ONLY
class TestGradleDaemonGuardBehaviour:
    def test_sets_the_flag_when_gradle_opts_is_absent(self):
        env = _run_guard(_build_launcher_script("strict"), {})
        # No leading space: GRADLE_OPTS is parsed as JVM args, and a bare
        # leading separator is noise in every log that echoes it back.
        assert env["GRADLE_OPTS"] == _FLAG

    def test_sets_the_flag_when_gradle_opts_is_empty(self):
        env = _run_guard(_build_launcher_script("strict"), {"GRADLE_OPTS": ""})
        assert env["GRADLE_OPTS"] == _FLAG

    def test_preserves_an_explicit_caller_value(self):
        # Appending rather than assigning keeps the caller's tuning; clobbering
        # GRADLE_OPTS would silently drop a heap size the build depends on.
        env = _run_guard(_build_launcher_script("strict"), {"GRADLE_OPTS": "-Xmx2g"})
        assert env["GRADLE_OPTS"] == f"-Xmx2g {_FLAG}"

    def test_does_not_add_the_flag_twice(self):
        already = f"-Xmx2g {_FLAG}"
        env = _run_guard(_build_launcher_script("strict"), {"GRADLE_OPTS": already})
        assert env["GRADLE_OPTS"] == already
        assert env["GRADLE_OPTS"].count(_FLAG) == 1

    def test_the_flag_wins_over_an_inherited_daemon_true(self):
        # A later -D beats an earlier one in JVM argument order, so appending is
        # what neutralises an inherited -Dorg.gradle.daemon=true.
        env = _run_guard(
            _build_launcher_script("strict"),
            {"GRADLE_OPTS": "-Dorg.gradle.daemon=true"},
        )
        opts = env["GRADLE_OPTS"]
        assert opts.index("-Dorg.gradle.daemon=true") < opts.index(_FLAG)

    def test_the_flag_wins_when_a_true_directive_comes_last(self):
        # GRADLE_OPTS carrying BOTH forms with =true LAST. Measured on openjdk 21:
        # duplicate -D resolves last-wins, so the mere PRESENCE of the false form
        # does not disable the daemon -- the guard must key on the effective last
        # directive or the daemon stays enabled and outlives the sandbox.
        env = _run_guard(
            _build_launcher_script("strict"),
            {"GRADLE_OPTS": f"{_FLAG} -Dorg.gradle.daemon=true"},
        )
        assert _effective_daemon_directive(env["GRADLE_OPTS"]) == _FLAG

    def test_does_not_append_when_the_false_flag_is_already_effective(self):
        # Guards the OPPOSITE direction from the test above: an unconditional
        # append would accrete a duplicate flag on every nested invocation.
        already = f"{_FLAG} -Xmx2g"
        env = _run_guard(_build_launcher_script("strict"), {"GRADLE_OPTS": already})
        assert env["GRADLE_OPTS"] == already
        assert env["GRADLE_OPTS"].count(_FLAG) == 1


@_POSIX_ONLY
class TestGradleDaemonGuardPlacement:
    def test_the_guard_is_emitted_at_every_sandbox_level(self):
        # Every level unshares a mount namespace and masks credential paths, so
        # a daemon left behind by any of them is adoptable from outside.
        for level in _SANDBOX_LEVELS:
            assert _FLAG in _build_launcher_script(level), level

    def test_the_guard_runs_before_the_exec(self):
        # Set after exec, the flag would never reach the build.
        script = _build_launcher_script("strict")
        assert script.index(_FLAG) < script.index("os.execvp(argv[0], argv)")

    def test_the_generated_launcher_still_compiles_at_every_level(self):
        # The template is an f-string with real placeholders, so an inserted
        # line containing an unescaped brace would break every level.
        for level in _SANDBOX_LEVELS:
            compile(_build_launcher_script(level), "<launcher>", "exec")


@_POSIX_ONLY
class TestGradleDaemonGuardExtractorIsDiscriminating:
    """Negative control: the extractor must fail when the guard is gone."""

    def test_extractor_raises_when_the_guard_line_is_removed(self):
        script = _build_launcher_script("strict")
        node = _guard_node(script)  # fails loudly if the guard is already gone
        lines = script.splitlines(keepends=True)
        stripped = "".join(lines[: node.lineno - 1] + lines[node.end_lineno :])
        with pytest.raises(AssertionError, match="no guard testing"):
            _guard_node(stripped)
