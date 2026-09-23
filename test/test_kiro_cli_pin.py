"""The two sync kiro-cli spawns resolve through the PATH-excluding pin.

``kirocrew update`` (``cli_server._update``) and the diagnostics support
bundle (``diagnostics._kiro_cli_version``) both spawn kiro-cli from a process
whose inherited ``PATH`` can lead with an agent-writable directory — a worktree
venv's ``bin``. A bare ``"kiro-cli"`` argv0 is re-resolved off that ``PATH``
inside ``exec``, so whatever that directory names would run as the gateway
user. Both sites now route through :func:`kiro_crew.kiro_cli.pin_kiro_cli`,
the sync form of the pin the unattended gateway spawns already use: an
absolute path from the known install directories plus the operator's
``KIROCREW_KIRO_BIN``, with the inherited ``PATH`` excluded, refusing rather
than falling back to the bare name.

The structural guard at the bottom keeps the count of bare-name kiro-cli
spawns under ``src/kiro_crew`` at zero, so a new site of the same shape fails
here rather than in a review lane.
"""

from __future__ import annotations

import ast
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# One cached read of the package, shared by every AST ratchet in this suite.
from source_corpus import parsed_candidates, src_root

# The coverage module owns the `_update` harness: a git-checkout project dir with
# git stubbed by argv prefix. Reused rather than copied so the two stay one.
from test_cli_server_more_coverage import _GitStub, git_checkout  # noqa: F401

from kiro_crew import cli_server, diagnostics, kiro_cli

_SRC_ROOT = src_root()

# Captured at import, before any fixture rebinds the module attribute: the
# end-to-end tests below want the REAL resolver against a fake home and PATH.
_REAL_RESOLVE = kiro_cli.resolve_kiro_cli


# --------------------------------------------------------------------------- #
# The shared sync pin                                                          #
# --------------------------------------------------------------------------- #


def _plant_shim(directory: Path) -> Path:
    """An executable ``kiro-cli`` in ``directory`` that records if it ever ran."""
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "kiro-cli"
    shim.write_text("#!/bin/sh\necho SHIM_RAN\n", encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


@pytest.fixture
def shadowed_path(monkeypatch, tmp_path):
    """A host whose only kiro-cli is a shim in an agent-writable PATH entry.

    Home is empty (no ``~/.local/bin`` / ``~/.cargo/bin`` install), the operator
    override is unset, and ``PATH`` leads with the shim's directory — the
    worktree-venv shape the pin exists to refuse.

    The fixed macOS system locations are emptied too, because no fixture argument
    reaches them: ``known_kiro_cli_dirs`` derives every other entry from ``home``
    or ``environ``, but ``/Applications/Kiro CLI.app/Contents/MacOS``,
    ``/opt/homebrew/bin`` and ``/usr/local/bin`` are constants. Without this a
    developer machine with a real install resolves a genuinely pinned absolute
    path there — the product behaving CORRECTLY — and the "nothing was spawned"
    assertions below fail for a reason that is about the machine and not about the
    code. Found by the seventh five-run sweep, where both were red in all five
    rounds on macOS and green on every CI runner. ``_MACOS_SYSTEM_DIRS`` keeps its
    real values under ``TestKnownDirsAreFenceable``, so emptying it here cannot
    become the shipped default.
    """
    home = tmp_path / "home"
    home.mkdir()
    shim = _plant_shim(tmp_path / "venv" / "bin")
    # The `_update` harness pins the resolver to "nothing installed"; put the
    # real one back so this is a genuine lookup over the fake host.
    monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", _REAL_RESOLVE)
    monkeypatch.setattr(kiro_cli, "_MACOS_SYSTEM_DIRS", ())
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
    monkeypatch.setenv("PATH", str(shim.parent))
    return shim


class TestKnownDirsAreFenceable:
    """``shadowed_path`` empties ``_MACOS_SYSTEM_DIRS``, so the real values and the
    search order they are spliced into both need holding here. Otherwise a fixture
    that fences the fixed locations and a product that stopped searching them look
    the same from the test suite."""

    _FIXED = (
        "/Applications/Kiro CLI.app/Contents/MacOS",
        "/opt/homebrew/bin",
        "/usr/local/bin",
    )
    # A fake account root, plus the user-bundle path DERIVED from it rather than
    # written out: the expectation then cannot drift from the input, and no
    # home-shaped absolute path is spelled literally. The internal-content scan
    # reads one as a leaked identity path -- correctly, because a literal here is
    # the pattern the next test copies -- so the root carries no `home` component
    # either, rather than relying on where that rule draws its line.
    _ACCOUNT_ROOT = Path("/nowhere/acct")
    _USER_APP = str(_ACCOUNT_ROOT / "Applications" / "Kiro CLI.app" / "Contents" / "MacOS")

    def _darwin_dirs(self, **kw) -> list[str]:
        return kiro_cli.known_kiro_cli_dirs(
            "darwin", self._ACCOUNT_ROOT, {}, include_inherited_path=False, **kw
        )

    def test_the_fixed_macos_locations_are_still_searched(self) -> None:
        assert kiro_cli._MACOS_SYSTEM_DIRS == self._FIXED
        dirs = self._darwin_dirs()
        for fixed in self._FIXED:
            assert fixed in dirs, fixed

    def test_the_user_bundle_is_searched_between_the_system_ones(self) -> None:
        """The system bundle first, the user's own next, then the shared bin dirs:
        the order the splice has to preserve."""
        dirs = self._darwin_dirs()
        assert dirs.index(self._FIXED[0]) < dirs.index(self._USER_APP) < dirs.index(self._FIXED[1])
        assert dirs.index(self._FIXED[1]) < dirs.index(self._FIXED[2])

    def test_emptying_the_constant_leaves_the_user_bundle(self, monkeypatch) -> None:
        """What the fixture relies on: fencing the fixed locations must not also
        drop the home-derived entry spliced between them."""
        monkeypatch.setattr(kiro_cli, "_MACOS_SYSTEM_DIRS", ())
        dirs = self._darwin_dirs()
        assert self._USER_APP in dirs
        for fixed in self._FIXED:
            assert fixed not in dirs, fixed


posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="PATH shadowing with a chmod +x shim is a POSIX shape"
)


class TestPinKiroCli:
    def test_pinned_install_is_returned_and_not_flagged(self, monkeypatch) -> None:
        calls: list[dict] = []

        def _resolve(**kw):
            calls.append(kw)
            return "/opt/pinned/bin/kiro-cli"

        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", _resolve)
        assert kiro_cli.pin_kiro_cli() == ("/opt/pinned/bin/kiro-cli", False)
        # One lookup, with the inherited PATH excluded.
        assert calls == [{"include_inherited_path": False}]

    def test_path_only_install_is_refused_and_flagged(self, monkeypatch) -> None:
        calls: list[dict] = []

        def _resolve(**kw):
            calls.append(kw)
            return None if kw.get("include_inherited_path") is False else "/venv/bin/kiro-cli"

        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", _resolve)
        assert kiro_cli.pin_kiro_cli() == (None, True)
        # The PATH-inclusive lookup runs only to decide whether the refusal is
        # worth reporting; it never names what gets spawned.
        assert calls[0] == {"include_inherited_path": False}
        assert calls[1] == {}

    def test_absent_backend_is_refused_quietly(self, monkeypatch) -> None:
        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda **kw: None)
        assert kiro_cli.pin_kiro_cli() == (None, False)

    def test_relative_override_is_refused_and_flagged(self, monkeypatch) -> None:
        """A relative pin is not a pin.

        ``KIROCREW_KIRO_BIN=kiro-cli`` passes the resolver's existence check
        against the current directory, but ``exec`` would re-resolve that bare
        argv0 off ``PATH`` — probe and spawn naming different files is the
        divergence the pin removes. Refused, and reported so the operator learns
        the override wants an absolute path.
        """
        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda **kw: "kiro-cli")
        assert kiro_cli.pin_kiro_cli() == (None, True)

    @posix_only
    def test_relative_override_never_yields_a_cwd_shim(self, monkeypatch, tmp_path) -> None:
        """End to end with the REAL resolver: a shim in the current directory,
        named by a relative override and also first on ``PATH``, is refused."""
        home = tmp_path / "home"
        home.mkdir()
        cwd = tmp_path / "cwd"
        shim = _plant_shim(cwd)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.setenv("KIROCREW_KIRO_BIN", "kiro-cli")
        monkeypatch.setenv("PATH", str(cwd))
        monkeypatch.chdir(cwd)
        pinned, unpinned_exists = kiro_cli.pin_kiro_cli()
        assert pinned is None
        assert unpinned_exists is True
        assert shim.exists()


# --------------------------------------------------------------------------- #
# Shared fixtures: a PATH-shadowed shim in a fake home                         #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# cli_server._update: the `kiro-cli update` step                               #
# --------------------------------------------------------------------------- #


class _UpdateRun(_GitStub):
    """The coverage module's ``_update`` stub, plus a kiro-cli argv filter."""

    def kiro_argvs(self) -> list[list[str]]:
        return [c for c in self.calls if c and Path(c[0]).name == "kiro-cli"]


@pytest.fixture
def update_checkout(monkeypatch, git_checkout):  # noqa: F811 -- pytest fixture by name
    """``git_checkout`` with every ``_update`` spawn stubbed and argv recorded.

    The fixture pins ``shutil.which`` to a TRUTHY answer so the base code's
    existence probe lets the kiro-cli step run: on the unmodified base that is
    what makes the bare-name spawn reachable, and the fixed code never
    consults ``which`` for kiro-cli at all.
    """
    monkeypatch.setattr(cli_server.shutil, "which", lambda name: f"/venv/bin/{name}")
    run = _UpdateRun()
    monkeypatch.setattr(subprocess, "run", run)
    return run


class TestUpdateKiroCliPin:
    def test_update_execs_the_pinned_absolute_path(self, monkeypatch, update_checkout) -> None:
        """argv0 is the RESOLVED path, never the bare name.

        RED-BEFORE: on the unmodified base argv0 is ``"kiro-cli"`` — the lookup
        that decides which file runs happens inside ``exec`` against the
        inherited ``PATH``.
        """
        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda **kw: "/opt/pinned/bin/kiro-cli")
        cli_server._update()
        assert update_checkout.kiro_argvs() == [["/opt/pinned/bin/kiro-cli", "update"]]

    def test_update_pins_without_the_inherited_path(self, monkeypatch, update_checkout) -> None:
        """The first (deciding) lookup excludes the inherited ``PATH``."""
        calls: list[dict] = []

        def _resolve(**kw):
            calls.append(kw)
            return "/opt/pinned/bin/kiro-cli"

        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", _resolve)
        cli_server._update()
        assert calls and calls[0] == {"include_inherited_path": False}

    def test_update_skips_kiro_cli_when_unresolvable(
        self, monkeypatch, update_checkout, capsys
    ) -> None:
        """No pinned install: the step is SKIPPED, the rest of the update runs."""
        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda **kw: None)
        cli_server._update()
        assert update_checkout.kiro_argvs() == []
        out = capsys.readouterr().out
        assert "Kiro Crew updated!" in out
        # An absent backend is not worth a line: it is optional.
        assert "KIROCREW_KIRO_BIN" not in out

    def test_update_reports_a_path_only_install_it_declined(
        self, monkeypatch, update_checkout, capsys
    ) -> None:
        """Refusing is right; refusing SILENTLY is not.

        A host whose only kiro-cli is reachable through ``PATH`` never gets the
        backend updated by ``kirocrew update`` — the operator is told, and told
        the override that puts it back.
        """
        monkeypatch.setattr(
            kiro_cli,
            "resolve_kiro_cli",
            lambda **kw: (
                None if kw.get("include_inherited_path") is False else "/venv/bin/kiro-cli"
            ),
        )
        cli_server._update()
        assert update_checkout.kiro_argvs() == []
        out = capsys.readouterr().out
        assert "KIROCREW_KIRO_BIN" in out
        assert "Kiro Crew updated!" in out

    @posix_only
    def test_update_never_runs_a_path_shadowed_shim(
        self, update_checkout, shadowed_path, capsys
    ) -> None:
        """End to end with the REAL resolver: a shim first on ``PATH`` is refused.

        RED-BEFORE: ``shutil.which`` finds the shim, so the base spawns
        ``["kiro-cli", "update"]`` and ``exec`` would resolve that to the shim.
        """
        cli_server._update()
        spawned = update_checkout.kiro_argvs()
        assert spawned == [], f"a PATH-only kiro-cli was spawned: {spawned}"
        assert all(c[0] != "kiro-cli" for c in update_checkout.calls)
        assert "KIROCREW_KIRO_BIN" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# diagnostics._kiro_cli_version: the support-bundle probe                      #
# --------------------------------------------------------------------------- #


class TestDiagnosticsKiroCliPin:
    @staticmethod
    def _capture(monkeypatch, stdout: str = "kiro-cli 9.9.9\n"):
        argvs: list[list[str]] = []

        def _run(argv, **kw):
            argvs.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        monkeypatch.setattr(subprocess, "run", _run)
        return argvs

    def test_probe_execs_the_pinned_absolute_path(self, monkeypatch) -> None:
        """RED-BEFORE: the base spawns ``["kiro-cli", "--version"]`` bare."""
        argvs = self._capture(monkeypatch)
        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda **kw: "/opt/pinned/bin/kiro-cli")
        assert diagnostics._kiro_cli_version() == "kiro-cli 9.9.9"
        assert argvs == [["/opt/pinned/bin/kiro-cli", "--version"]]

    def test_probe_pins_without_the_inherited_path(self, monkeypatch) -> None:
        self._capture(monkeypatch)
        calls: list[dict] = []

        def _resolve(**kw):
            calls.append(kw)
            return "/opt/pinned/bin/kiro-cli"

        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", _resolve)
        diagnostics._kiro_cli_version()
        assert calls and calls[0] == {"include_inherited_path": False}

    def test_absent_backend_reads_unavailable_without_spawning(self, monkeypatch) -> None:
        argvs = self._capture(monkeypatch)
        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda **kw: None)
        assert diagnostics._kiro_cli_version() == "unavailable"
        assert argvs == []

    def test_path_only_install_is_named_as_such_not_as_absent(self, monkeypatch) -> None:
        """A maintainer reading versions.txt must be able to tell "not installed"
        from "installed where the pin does not look" — the second is the
        operator's fix (``KIROCREW_KIRO_BIN``), the first is not a defect."""
        argvs = self._capture(monkeypatch)
        monkeypatch.setattr(
            kiro_cli,
            "resolve_kiro_cli",
            lambda **kw: (
                None if kw.get("include_inherited_path") is False else "/venv/bin/kiro-cli"
            ),
        )
        line = diagnostics._kiro_cli_version()
        assert argvs == []
        assert line != "unavailable"
        assert "PATH" in line and "KIROCREW_KIRO_BIN" in line

    def test_probe_failure_still_reads_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda **kw: "/opt/pinned/bin/kiro-cli")

        def _run(argv, **kw):
            raise OSError("boom")

        monkeypatch.setattr(subprocess, "run", _run)
        assert diagnostics._kiro_cli_version() == "unavailable"

    def test_resolver_failure_still_reads_unavailable(self, monkeypatch) -> None:
        """Diagnostics must work precisely when the rest is broken: a raising
        lookup degrades to the same answer a failed spawn does."""

        def _resolve(**kw):
            raise OSError("home unreadable")

        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", _resolve)
        assert diagnostics._kiro_cli_version() == "unavailable"

    @posix_only
    def test_probe_never_runs_a_path_shadowed_shim(self, shadowed_path, monkeypatch) -> None:
        """End to end with the REAL resolver, reached from the dashboard's
        diagnostics-collect handler shape: a shim first on ``PATH`` is refused.

        RED-BEFORE: the base spawns the bare name and ``exec`` resolves it to
        the shim.
        """
        argvs = self._capture(monkeypatch, stdout="SHIM_RAN\n")
        line = diagnostics._kiro_cli_version()
        assert argvs == [], f"a PATH-only kiro-cli was spawned: {argvs}"
        assert line != "SHIM_RAN"
        assert "KIROCREW_KIRO_BIN" in line


# --------------------------------------------------------------------------- #
# Structural guard: no bare-name kiro-cli spawn anywhere under src/kiro_crew   #
# --------------------------------------------------------------------------- #

_SPAWN_FUNCS = {
    "run",
    "Popen",
    "call",
    "check_call",
    "check_output",
    "create_subprocess_exec",
}

#: Constants the package spells the bare name through. A spawn whose argv0 is
#: one of these names is the same shape as the literal, just harder to grep.
_BARE_NAME_CONSTANTS = {"KIRO_CLI_BIN", "KIRO_CLI_NAME"}

#: Bare-name sites the guard SEES but this ratchet still admits, as
#: ``<path>::<function>``. All are interactive: they run under an operator's
#: own ``kirocrew doctor`` / ``kirocrew setup`` on a TTY, where the inherited
#: ``PATH`` is the documented default (see ``resolve_kiro_cli``) and the
#: exposure the pin closes — an unattended or click-reachable spawn — does not
#: apply. Shrink-only: a fixed site must be deleted here or the test fails,
#: and a new site anywhere is refused outright.
_KNOWN_INTERACTIVE_SITES = frozenset(
    {
        # `kirocrew doctor` sign-in row: which(KIRO_CLI_BIN) probe, then
        # [KIRO_CLI_BIN, "whoami"].
        "cli_doctor.py::_kiro_cli_signed_in",
        # `kirocrew doctor` dependency + connectivity rows: which(KIRO_CLI_BIN),
        # then [KIRO_CLI_BIN, "--version"].
        "cli_doctor.py::_doctor",
        # `kirocrew setup` prerequisite notice: which(KIRO_CLI_BIN), no spawn.
        "cli_setup.py::_ensure_prerequisites",
    }
)


def _spells_bare_name(node: ast.AST) -> bool:
    """Whether ``node`` is the literal ``"kiro-cli"`` or a constant that names it."""
    if isinstance(node, ast.Constant):
        return node.value == "kiro-cli"
    if isinstance(node, ast.Name):
        return node.id in _BARE_NAME_CONSTANTS
    if isinstance(node, ast.Attribute):
        return node.attr in _BARE_NAME_CONSTANTS
    return False


def _bare_kiro_cli_spawns(tree: ast.AST) -> list[tuple[int, str]]:
    """``(line, enclosing function)`` of spawn calls whose argv0 is the bare name.

    Covers the ``subprocess.*`` list-argv shape (``run(["kiro-cli", ...])``),
    the ``asyncio.create_subprocess_exec("kiro-cli", ...)`` varargs shape, and
    both spellings of the name — the literal and the ``KIRO_CLI_BIN`` /
    ``KIRO_CLI_NAME`` constants. A ``shutil.which(<name>)`` existence probe is
    flagged too: the probe-then-discard + bare-argv0 pair is one shape, and the
    probe's answer is never what ``exec`` resolves. The enclosing function is
    ``<module>`` for a module-level call.
    """
    hits: list[tuple[int, str]] = []

    def _scan(body: ast.AST, owner: str) -> None:
        for node in ast.walk(body):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            first = node.args[0]
            if name == "which":
                if _spells_bare_name(first):
                    hits.append((node.lineno, owner))
                continue
            if name not in _SPAWN_FUNCS:
                continue
            if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
                first = first.elts[0]
            if _spells_bare_name(first):
                hits.append((node.lineno, owner))

    # Top-level functions own their whole body (nested defs included); anything
    # outside one is attributed to the module.
    functions = [
        n
        for n in getattr(tree, "body", [])
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for fn in functions:
        _scan(fn, fn.name)
    owned = {id(fn) for fn in functions}
    for node in getattr(tree, "body", []):
        if id(node) not in owned:
            _scan(node, "<module>")
    return sorted(hits)


def test_no_bare_name_kiro_cli_spawn_remains_in_the_package() -> None:
    """RED-BEFORE: two sites — ``cli_server._update`` and
    ``diagnostics._kiro_cli_version``. The interactive sites in the known set
    are admitted and counted; any other hit, in either spelling, fails."""
    offenders: list[str] = []
    seen_known: set[str] = set()
    # Only files that spell the name can hold a match; the corpus pre-filters on
    # either spelling so this gate parses a handful of modules, not the package.
    for path, _text, tree in parsed_candidates(require_any=("kiro-cli", *_BARE_NAME_CONSTANTS)):
        if "tests" in path.parts or "test" in path.parts:
            continue
        rel = path.relative_to(_SRC_ROOT).as_posix()
        for lineno, owner in _bare_kiro_cli_spawns(tree):
            key = f"{rel}::{owner}"
            if key in _KNOWN_INTERACTIVE_SITES:
                seen_known.add(key)
                continue
            offenders.append(f"{rel}:{lineno} ({owner})")
    assert offenders == [], (
        "bare-name kiro-cli spawns resolve argv0 off the inherited PATH inside "
        "exec; route them through kiro_cli.pin_kiro_cli (sync) or the gateway's "
        f"_pinned_kiro_cli (async): {offenders}"
    )
    # Shrink-only: an entry whose site has stopped spawning by bare name is debt
    # that was paid, so the entry must go.
    stale = _KNOWN_INTERACTIVE_SITES - seen_known
    assert not stale, f"known-site entries with no remaining bare-name spawn: {sorted(stale)}"


def test_structural_guard_sees_every_shape() -> None:
    """The guard's own detector, pinned so a refactor cannot blind it."""
    src = (
        "import shutil, subprocess, asyncio\n"
        'shutil.which("kiro-cli")\n'
        'subprocess.run(["kiro-cli", "update"])\n'
        'subprocess.Popen(("kiro-cli", "--version"))\n'
        'asyncio.create_subprocess_exec("kiro-cli", "--version")\n'
        'subprocess.run([binary, "--version"])\n'
        'shutil.which("git")\n'
        "def probe():\n"
        "    shutil.which(KIRO_CLI_BIN)\n"
        '    subprocess.run([KIRO_CLI_BIN, "whoami"])\n'
        '    subprocess.run([client.KIRO_CLI_NAME, "--version"])\n'
        '    subprocess.run([pinned, "--version"])\n'
    )
    assert _bare_kiro_cli_spawns(ast.parse(src)) == [
        (2, "<module>"),
        (3, "<module>"),
        (4, "<module>"),
        (5, "<module>"),
        (9, "probe"),
        (10, "probe"),
        (11, "probe"),
    ]
