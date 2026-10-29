"""The managed ``playwright-cli`` launcher and its attach time limit.

Four things are pinned here:

* the launcher passes every argv through unchanged, sends the ones that mention
  ``attach`` through the guard, and the guard limits only what the CLI's own
  parser reads as ``attach``;
* the guard leaves a finished attach's output and exit status alone, and stops a
  stalled one: its CLI child is gone and it exits 124 telling the person to accept
  Chrome's prompt or retry;
* the shell installer and the gateway write the same two files byte for byte;
* the gateway's refresh writes only a missing or stale file, never touches a host
  without a managed install, and survives a failed write.

The guard tests run the real guard script under the host's Node against a fake
CLI script, with the guard's deadline shortened by text substitution. Nothing
asserts on elapsed time: every wait is bounded and fails the test when it runs out.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import os
import shutil
import signal
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
from playwright_cli_installer_helpers import (  # noqa: F401 -- fixtures are used by name
    INSTALLER_SH,
    _fake_node,
    _fake_npm_succeeding,
    _run,
    posix_only,
    stubs_fixture,
)

from kiro_crew import platform_compat
from kiro_crew.browser_cli import install as install_mod
from kiro_crew.browser_cli import managed_launcher as ml
from kiro_crew.testing.wait import wait_until

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    os.name == "nt" or _NODE is None, reason="runs the guard under a host Node (POSIX)"
)

#: The guard's real deadline, as written in the script.
_DEADLINE_LINE = "const TIMEOUT_MS = 120000;"
#: The exit status of a stopped attach (the shell convention for a timed-out command).
_TIMEOUT_EXIT = 124
#: Replaces the deadline line so the guard's deadline timer fires once the fake
#: attach has written its pidfile (polled every 10 ms) instead of after a time.
#: The module-scope ``setTimeout``/``clearTimeout`` shadow the globals for the
#: guard's code below; every other timer (the kill grace, the signal re-raise)
#: passes straight through.
_ON_READY_DEADLINE = """const TIMEOUT_MS = 120000;
const setTimeout = (fn, ms) => {
  if (ms !== TIMEOUT_MS) return global.setTimeout(fn, ms);
  const handle = { ready: null };
  const poll = () => {
    if (fs.existsSync(process.env.FAKE_PIDFILE)) fn();
    else handle.ready = global.setTimeout(poll, 10);
  };
  handle.ready = global.setTimeout(poll, 10);
  return handle;
};
const clearTimeout = (h) => global.clearTimeout(h && 'ready' in h ? h.ready : h);"""

#: A stand-in for ``playwright-cli.js``. It records each invocation, and ``attach``
#: behaves as FAKE_MODE says: ``hang`` waits forever (the unapproved extension),
#: ``term0`` waits too but exits 0 on SIGTERM (what 0.1.22's own SIGTERM handler can
#: leave it doing), ``ok`` and ``fail`` finish with fixed output. It reads its verb
#: with the fake parser below when one is installed, as the real CLI does.
_FAKE_CLI = r"""
const fs = require('fs');
const args = process.argv.slice(2);
let verb;
try {
  const client = __dirname + '/node_modules/playwright-core/lib/tools/cli-client/';
  const { minimist } = require(client + 'minimist.js');
  const boolean = [...require(client + 'help.json').booleanOptions];
  try {
    const list = /const booleanOptions = \[([^\]]*)\]/.exec(fs.readFileSync(client + 'program.js', 'utf-8'));
    boolean.push(...[...list[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]));
  } catch (error) {}
  verb = minimist(args, { boolean })._[0];
} catch (error) {
  verb = args.find((a) => !a.startsWith('-'));
}
if (process.env.FAKE_VERB) verb = process.env.FAKE_VERB;
fs.appendFileSync(process.env.FAKE_LOG, JSON.stringify({
  args, session: process.env.PLAYWRIGHT_CLI_SESSION || null,
}) + '\n');
if (verb === 'attach') {
  const mode = process.env.FAKE_MODE;
  if (mode === 'hang' || mode === 'term0') {
    if (mode === 'term0') process.on('SIGTERM', () => process.exit(0));
    fs.writeFileSync(process.env.FAKE_PIDFILE, String(process.pid));
    setInterval(() => {}, 1000);
  } else if (mode === 'ok') {
    process.stdout.write('### Session\nattached to chrome\n');
    process.stderr.write('note from the cli\n');
    process.exit(0);
  } else {
    process.stderr.write('Error: extension not found\n');
    process.exit(3);
  }
} else {
  process.stdout.write(`ran ${verb}\n`);
  setTimeout(() => process.exit(Number(process.env.FAKE_RC || 0)),
    Number(process.env.FAKE_SLOW_MS || 0));
}
"""

#: A small stand-in for playwright-core's own argv parser and option list,
#: installed beside the fake CLI where the guard looks for the real ones. An
#: option without ``=`` takes the next non-option word unless it is boolean.
_FAKE_MINIMIST = r"""
exports.minimist = function (args, opts) {
  const bools = new Set(opts.boolean || []);
  const out = { _: [] };
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === '--') { out._.push(...args.slice(i + 1)); break; }
    let m = /^--?([^=]+)=([\s\S]*)$/.exec(a);
    if (m) { out[m[1]] = m[2]; continue; }
    m = /^--?(.+)$/.exec(a);
    if (m) {
      const next = args[i + 1];
      if (!bools.has(m[1]) && next !== undefined && !next.startsWith('-')) {
        out[m[1]] = next;
        i++;
      } else {
        out[m[1]] = true;
      }
      continue;
    }
    out._.push(a);
  }
  return out;
};
"""
_FAKE_PACKAGE = (
    '{"name": "playwright-core", "exports": {"./package.json": "./package.json", '
    '"./lib/tools/cli-client/program": "./lib/tools/cli-client/program.js"}}'
)
_FAKE_HELP = '{"booleanOptions": ["headed"], "commands": {}}'


def _wait_for(path: Path, what: str) -> str:
    """The content of *path* once it exists; fails the test, naming *what*, when it
    never does."""

    def _content() -> str:
        return path.read_text() if path.exists() else ""

    return wait_until(_content, describe=lambda: f"{what} never appeared at {path}")


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _run_group(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run *argv* in a process group of its own and return what it produced.

    A guard that hangs past the bound is stopped together with its CLI child: the
    whole group is killed, so a failing test never leaves the fake CLI running.
    """
    proc = subprocess.Popen(
        argv,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=60)
        raise
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


class _Cli:
    """A fake CLI and a guard whose deadline is *deadline_ms*.

    With *deadline_ms* None (the default) the deadline fires on an event, not
    after a time: once the fake attach has logged its call and written its
    pidfile, so the stop always reaches a child that is already waiting. A
    number is a real deadline in milliseconds, for tests where the deadline
    must NOT fire (a 1 ms one for a verb that should run unlimited, the real one
    for an attach that finishes).

    *parser* installs the fake playwright-core parser beside the CLI; without it
    the guard runs its fallback, as it would against a CLI whose layout moved.
    """

    def __init__(self, tmp_path: Path, deadline_ms: int | None = None, parser: bool = True) -> None:
        assert ml.ATTACH_GUARD_JS.count(_DEADLINE_LINE) == 1
        self.entry = tmp_path / "playwright-cli.js"
        self.entry.write_text(_FAKE_CLI, encoding="utf-8")
        if parser:
            client = tmp_path / "node_modules" / "playwright-core" / "lib" / "tools" / "cli-client"
            client.mkdir(parents=True)
            (client / "minimist.js").write_text(_FAKE_MINIMIST, encoding="utf-8")
            (client / "help.json").write_text(_FAKE_HELP, encoding="utf-8")
            # The real package's exports map, which hides the parser files from a
            # subpath require; only package.json and program are exported.
            (client.parents[2] / "package.json").write_text(_FAKE_PACKAGE, encoding="utf-8")
        self.guard = tmp_path / ml.GUARD_NAME
        deadline = (
            _ON_READY_DEADLINE if deadline_ms is None else f"const TIMEOUT_MS = {deadline_ms};"
        )
        self.guard.write_text(
            ml.ATTACH_GUARD_JS.replace(_DEADLINE_LINE, deadline), encoding="utf-8"
        )
        self.log = tmp_path / "calls.jsonl"
        self.pidfile = tmp_path / "attach.pid"

    def env(self, mode: str, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "PLAYWRIGHT_CLI_SESSION"}
        env.update(FAKE_MODE=mode, FAKE_LOG=str(self.log), FAKE_PIDFILE=str(self.pidfile))
        env.update(extra)
        return env

    def calls(self) -> list[dict[str, object]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def direct(self, mode: str, *args: str) -> subprocess.CompletedProcess[str]:
        return _run_group([str(_NODE), str(self.entry), *args], self.env(mode))

    def guarded(self, mode: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return _run_group(
            [str(_NODE), str(self.guard), str(self.entry), *args], self.env(mode, **env)
        )


# ── the guard ────────────────────────────────────────────────────────


def test_the_guard_deadline_matches_the_documented_default() -> None:
    assert ml.ATTACH_GUARD_JS.count(_DEADLINE_LINE) == 1
    assert ml.ATTACH_TIMEOUT_S * 1000 == 120000
    assert f"const TIMEOUT_EXIT = {_TIMEOUT_EXIT};" in ml.ATTACH_GUARD_JS


@needs_node
@pytest.mark.parametrize("mode", ["hang", "term0"])
@pytest.mark.parametrize("parser", [True, False])
@pytest.mark.parametrize(
    ("args", "env"),
    [
        (["attach", "--extension=chrome"], {}),
        (["attach", "--extension=chrome"], {"PLAYWRIGHT_CLI_SESSION": "kc-1"}),
    ],
)
def test_a_stalled_attach_is_stopped_and_explained(
    tmp_path: Path, mode: str, parser: bool, args: list[str], env: dict[str, str]
) -> None:
    """``term0`` is a CLI that exits 0 once SIGTERM stops it: still a timeout. With
    or without the CLI's own parser, the stopped attach is the only CLI call: no
    session is detached afterwards."""
    cli = _Cli(tmp_path, parser=parser)

    result = cli.guarded(mode, *args, **env)

    assert result.returncode == _TIMEOUT_EXIT
    assert "Accept Chrome's debugging prompt" in result.stderr
    assert "retry the attach" in result.stderr
    assert result.stdout == ""
    # The CLI child the guard started is gone, not orphaned.
    assert _gone(int(cli.pidfile.read_text()))
    assert [c["args"] for c in cli.calls()] == [args]


@needs_node
@pytest.mark.parametrize(
    ("args", "verb"),
    [
        (["--session", "attach", "snapshot"], "snapshot"),
        (["-s", "attach", "run-code", "x"], "run-code"),
        (["goto", "attach"], "goto"),
    ],
)
def test_without_the_cli_parser_no_other_verb_gets_the_limit(
    tmp_path: Path, args: list[str], verb: str
) -> None:
    """With the parser unavailable, only a leading ``attach`` word is limited: a
    session named ``attach`` or a value ``attach`` never puts a deadline on
    another verb."""
    cli = _Cli(tmp_path, deadline_ms=1, parser=False)
    # The fake CLI is told the verb the real parser reads; without a parser of
    # its own it would take the first bare word.
    slow = {"FAKE_SLOW_MS": "300", "FAKE_RC": "5", "FAKE_VERB": verb}

    result = cli.guarded("hang", *args, **slow)

    assert result.returncode == 5
    assert "Accept Chrome" not in result.stderr
    assert [c["args"] for c in cli.calls()] == [args]


@needs_node
def test_a_verb_the_cli_does_not_read_as_attach_runs_without_the_limit(tmp_path: Path) -> None:
    """The CLI's parser has the last word: ``--session attach snapshot`` is a
    snapshot in session ``attach``, so no deadline applies to it."""
    cli = _Cli(tmp_path, deadline_ms=1)

    result = cli.guarded("hang", "--session", "attach", "snapshot")

    assert result.returncode == 0
    assert "Accept Chrome" not in result.stderr
    assert [c["args"] for c in cli.calls()] == [["--session", "attach", "snapshot"]]


@needs_node
@pytest.mark.parametrize(
    ("args", "parsed"),
    [
        # Each ``parsed`` is what that CLI version's own minimist returns for the
        # argv; these spellings are easy to misread without the CLI's parser.
        (
            ["-s0.", "attach", "--extension=chrome"],
            {"_": ["attach"], "s": "0.", "extension": "chrome"},
        ),
        (
            ["-_", "attach", "--extension=chrome"],
            {"_": ["attach"], "extension": "chrome"},
        ),  # 0.1.17
    ],
)
def test_rare_attach_spellings_get_the_limit(
    tmp_path: Path, args: list[str], parsed: dict[str, object]
) -> None:
    """The guard limits whatever the CLI's own parser reads as ``attach``,
    however it is spelled; the parser here returns the real one's reading."""
    cli = _Cli(tmp_path)
    client = tmp_path / "node_modules" / "playwright-core" / "lib" / "tools" / "cli-client"
    (client / "minimist.js").write_text(
        f"exports.minimist = () => ({json.dumps(parsed)});\n", encoding="utf-8"
    )

    result = cli.guarded("hang", *args)

    assert result.returncode == _TIMEOUT_EXIT
    assert "Accept Chrome's debugging prompt" in result.stderr
    assert [c["args"] for c in cli.calls()] == [args]


@needs_node
@pytest.mark.parametrize(
    ("program_booleans", "limited"),
    [
        # 0.1.17's program.js has no `g`: `-g` takes `attach` as its value and
        # the verb is `snapshot`.
        ('"all", "help", "json", "raw", "version"', False),
        # 0.1.22's adds `g`, so `attach` is the verb.
        ('"all", "g", "help", "json", "raw", "version"', True),
    ],
)
def test_the_guard_reads_the_programs_own_boolean_options(
    tmp_path: Path, program_booleans: str, limited: bool
) -> None:
    cli = _Cli(tmp_path, deadline_ms=1)
    client = tmp_path / "node_modules" / "playwright-core" / "lib" / "tools" / "cli-client"
    (client / "program.js").write_text(
        f"const booleanOptions = [\n  {program_booleans}\n];\n", encoding="utf-8"
    )

    result = cli.guarded("hang", "-g", "attach", "snapshot")

    assert result.returncode == (_TIMEOUT_EXIT if limited else 0)
    assert ("Accept Chrome" in result.stderr) is limited


def _layout_check() -> ModuleType:
    """scripts/check_playwright_cli_attach_guard.py, loaded as a module."""
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "check_playwright_cli_attach_guard.py"
    )
    spec = importlib.util.spec_from_file_location("check_playwright_cli_attach_guard", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_layout_check_finds_the_lines_it_rewrites() -> None:
    probe = _layout_check().probe_guard()

    assert probe is not None
    assert "const TIMEOUT_MS = 1;" in probe
    assert "const PROGRAM_BOOLEANS = [];\n" in probe


@needs_node
@pytest.mark.parametrize(
    ("program_js", "ok"),
    [
        ('const booleanOptions = [\n  "help",\n  "json"\n];\n', True),
        # A release that renamed the list: the guard falls back to its built-in
        # copy, which the check empties, so `--json` swallows `attach`.
        ('const globalBooleans = [\n  "help",\n  "json"\n];\n', False),
    ],
)
def test_the_layout_check_fails_when_the_option_list_stops_matching(
    tmp_path: Path, program_js: str, ok: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _Cli(tmp_path)
    client = tmp_path / "node_modules" / "playwright-core" / "lib" / "tools" / "cli-client"
    (client / "program.js").write_text(program_js, encoding="utf-8")
    for key, value in cli.env("hang").items():
        monkeypatch.setenv(key, value)

    passed, what = _layout_check().check(str(_NODE), cli.entry)

    assert passed is ok, what
    if not ok:
        assert "did not limit '--json attach --help'" in what
    assert not cli.pidfile.exists() or _gone(int(cli.pidfile.read_text()))


@needs_node
@pytest.mark.parametrize("mode", ["ok", "fail"])
def test_an_attach_that_finishes_is_unchanged(tmp_path: Path, mode: str) -> None:
    """Same stdout, stderr and exit status as running the CLI directly, and no
    other CLI call. The deadline is the real one, so it never fires here."""
    cli = _Cli(tmp_path, deadline_ms=120000)
    args = ("attach", "--extension=chrome")

    direct = cli.direct(mode, *args)
    guarded = cli.guarded(mode, *args)

    assert (guarded.returncode, guarded.stdout, guarded.stderr) == (
        direct.returncode,
        direct.stdout,
        direct.stderr,
    )
    assert [c["args"] for c in cli.calls()] == [list(args), list(args)]


@needs_node
def test_a_signal_to_the_guard_reaches_its_cli_child(tmp_path: Path) -> None:
    """A cancelled turn that signals only the guard's pid must not leave the
    attach waiting with nothing watching it."""
    cli = _Cli(tmp_path, deadline_ms=120000)
    # The guard and its CLI child share a process group of their own, so cleanup
    # can stop both, whatever point the test failed at.
    proc = subprocess.Popen(
        [str(_NODE), str(cli.guard), str(cli.entry), "attach", "--extension=chrome"],
        env=cli.env("hang"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        child = int(_wait_for(cli.pidfile, "the attach child's pid"))
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=60)
    finally:
        # Whatever the test reached: nothing in the group outlives it, and the
        # final drain is bounded.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        if proc.poll() is None or proc.stdout and not proc.stdout.closed:
            proc.communicate(timeout=60)

    assert proc.returncode == -signal.SIGTERM
    assert _gone(child)
    assert "Accept Chrome" not in err
    assert [c["args"] for c in cli.calls()] == [["attach", "--extension=chrome"]]


# ── the launcher ─────────────────────────────────────────────────────


def _render_with_recorder(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A rendered launcher whose "Node" prints the argv it was given, one per line."""
    tricky = tmp_path / "dir with 'quote' and $(x)"
    tricky.mkdir()
    node = tricky / "gateway-node"
    node.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit "${RECORDER_RC:-0}"\n')
    node.chmod(0o755)
    entry = tricky / "playwright-cli.js"
    guard = tricky / ml.GUARD_NAME
    launcher = tmp_path / "playwright-cli"
    launcher.write_text(ml._render_posix_launcher(str(node), str(entry), str(guard)))
    launcher.chmod(0o755)
    return launcher, entry, guard


#: Each argv with whether the launcher sends it to the guard: exactly when some
#: argument contains the word ``attach``. Rare spellings whose verb is hard to read
#: without the CLI's own parser (``-s0.``, ``-_``, and 0.1.17's ``-g``/``-1``
#: readings) all mention the word, so all reach the guard, which reads them with
#: that parser.
_ROUTING = [
    (["attach", "--extension=chrome"], True),
    (["-s=work", "attach", "--extension=chrome"], True),
    (["-s", "work", "attach", "--extension=chrome"], True),
    (["-s0.", "attach", "--extension=chrome"], True),
    (["-s123.", "attach", "--extension=chrome"], True),
    (["-_", "attach", "--extension=chrome"], True),
    (["-g", "work", "attach"], True),
    (["-1", "work", "attach", "--extension=chrome"], True),
    (["-gs=", "attach", "--extension=chrome"], True),
    (["--_=attach"], True),
    (["--", "attach"], True),
    (["goto", "https://x/attachments"], True),
    (["fill", "e1", "attach"], True),
    (["snapshot"], False),
    (["-s=work", "goto", "https://example.com/"], False),
    (["--version"], False),
    ([], False),
    (["detach"], False),
    (["tab-list", "--json"], False),
    (["type", "a b  c"], False),
]


@posix_only
@pytest.mark.parametrize(("args", "guarded"), _ROUTING)
def test_the_launcher_sends_every_argv_mentioning_attach_to_the_guard(
    tmp_path: Path, args: list[str], guarded: bool
) -> None:
    """Every argv reaches the CLI unchanged, with its exit status; the ones that
    mention ``attach`` go through the guard, every other one is the plain exec."""
    launcher, entry, guard = _render_with_recorder(tmp_path)

    result = subprocess.run(
        [str(launcher), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
        env={**os.environ, "RECORDER_RC": "7"},
    )

    prefix = [str(guard), str(entry)] if guarded else [str(entry)]
    assert result.stdout.splitlines() == [*prefix, *args]
    assert result.returncode == 7
    assert result.stderr == ""


@needs_node
@pytest.mark.parametrize("args", [["goto", "https://x/attachments"], ["fill", "e1", "attach"]])
def test_a_non_attach_verb_mentioning_attach_is_unchanged_and_unlimited(
    tmp_path: Path, args: list[str]
) -> None:
    """Through the real launcher and guard: the verb outlives the guard's (1 ms)
    deadline, and its stdout, stderr and exit status equal a direct run's, with
    no other CLI call and no timeout message."""
    cli = _Cli(tmp_path, deadline_ms=1)
    launcher = tmp_path / "playwright-cli"
    launcher.write_text(ml._render_posix_launcher(str(_NODE), str(cli.entry), str(cli.guard)))
    slow = {"FAKE_SLOW_MS": "300", "FAKE_RC": "5"}

    direct = _run_group([str(_NODE), str(cli.entry), *args], cli.env("hang", **slow))
    launched = _run_group(["sh", str(launcher), *args], cli.env("hang", **slow))

    assert direct.returncode == 5
    assert (launched.returncode, launched.stdout, launched.stderr) == (
        direct.returncode,
        direct.stdout,
        direct.stderr,
    )
    assert [c["args"] for c in cli.calls()] == [args, args]


def test_the_pass_through_line_is_the_plain_exec() -> None:
    assert (
        ml.POSIX_LAUNCHER_TEMPLATE.splitlines()[-1] == 'exec @KIROCREW_NODE@ @KIROCREW_ENTRY@ "$@"'
    )


def test_shell_quote_matches_the_installer() -> None:
    assert ml._shell_quote("/a b/c") == "'/a b/c'"
    assert ml._shell_quote("it's") == "'it'\\''s'"
    assert "printf \"'%s'\" \"$(printf '%s' \"$1\" | sed \"s/'/'\\\\\\\\''/g\")\"" in (
        INSTALLER_SH.read_text(encoding="utf-8")
    )


# ── the writers agree ────────────────────────────────────────────────


def _heredoc(body: str, marker: str) -> str:
    start = body.index(f"<<'{marker}'")
    start = body.index("\n", body.index("\n", start) + 1) + 1
    end = body.index(f"\n{marker}\n", start)
    return body[start : end + 1]


def test_the_installer_embeds_the_gateway_template_and_guard() -> None:
    body = INSTALLER_SH.read_text(encoding="utf-8")

    assert _heredoc(body, "KIROCREW_LAUNCHER") == ml.POSIX_LAUNCHER_TEMPLATE
    assert _heredoc(body, "KIROCREW_ATTACH_GUARD") == ml.ATTACH_GUARD_JS


@posix_only
def test_the_installer_and_the_gateway_write_identical_files(tmp_path: Path, stubs: Path) -> None:
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(tmp_path, stubs, "--version", "0.1.18")
    assert result.returncode == 0, result.stderr

    root = (tmp_path / "datahome" / "playwright-cli").resolve()
    launcher = root / "managed-bin" / "playwright-cli"
    guard = root / ml.GUARD_NAME
    entry = root / "lib" / "node_modules" / "@playwright" / "cli" / "playwright-cli.js"
    expected = ml._render_posix_launcher(str(root / "gateway-node"), str(entry), str(guard))

    assert launcher.read_text(encoding="utf-8") == expected
    assert guard.read_text(encoding="utf-8") == ml.ATTACH_GUARD_JS
    assert not list(root.glob(".kirocrew-attach-guard.*")), "staging file left behind"


@posix_only
def test_the_gateway_leaves_an_installer_written_launcher_alone(
    tmp_path: Path, stubs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    assert _run(tmp_path, stubs, "--version", "0.1.18").returncode == 0
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(install_mod, "config_dir", lambda: tmp_path / "datahome")

    assert ml.ensure_managed_launcher() is False


# ── the gateway refresh ──────────────────────────────────────────────


def _managed_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    crew = tmp_path / "crew"
    root = crew / "playwright-cli"
    entry = root / "lib" / "node_modules" / "@playwright" / "cli" / "playwright-cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// cli\n", encoding="utf-8")
    node = root / "gateway-node"
    node.write_bytes(b"node")
    node.chmod(0o500)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(install_mod, "config_dir", lambda: crew)
    return root.resolve()


def _expected(root: Path) -> str:
    return ml._render_posix_launcher(
        str(root / "gateway-node"),
        str(root / "lib" / "node_modules" / "@playwright" / "cli" / "playwright-cli.js"),
        str(root / ml.GUARD_NAME),
    )


@posix_only
def test_refresh_writes_a_missing_launcher_and_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _managed_install(tmp_path, monkeypatch)

    assert ml.ensure_managed_launcher() is True

    launcher = root / "managed-bin" / "playwright-cli"
    assert launcher.read_text(encoding="utf-8") == _expected(root)
    assert os.access(launcher, os.X_OK)
    assert (root / ml.GUARD_NAME).read_text(encoding="utf-8") == ml.ATTACH_GUARD_JS
    assert not list(root.rglob("*.incoming"))


@posix_only
def test_refresh_does_not_rewrite_a_current_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _managed_install(tmp_path, monkeypatch)
    assert ml.ensure_managed_launcher() is True
    launcher = root / "managed-bin" / "playwright-cli"
    guard = root / ml.GUARD_NAME
    before = (launcher.stat().st_ino, guard.stat().st_ino)

    assert ml.ensure_managed_launcher() is False

    assert (launcher.stat().st_ino, guard.stat().st_ino) == before


@posix_only
@pytest.mark.parametrize("stale", ["content", "symlink", "mode"])
def test_refresh_replaces_a_stale_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale: str
) -> None:
    root = _managed_install(tmp_path, monkeypatch)
    launcher = root / "managed-bin" / "playwright-cli"
    launcher.parent.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text(_expected(root))
    if stale == "content":
        launcher.write_text('#!/bin/sh\nexec node "$@"\n')
        launcher.chmod(0o755)
    elif stale == "symlink":
        launcher.symlink_to(elsewhere)
    else:
        launcher.write_text(_expected(root))
        launcher.chmod(0o644)

    assert ml.ensure_managed_launcher() is True

    assert not launcher.is_symlink()
    assert launcher.read_text(encoding="utf-8") == _expected(root)
    assert os.access(launcher, os.X_OK)
    assert elsewhere.read_text() == _expected(root), "the link target is never written"


@posix_only
@pytest.mark.parametrize("missing", ["gateway-node", "entry", "root"])
def test_refresh_does_nothing_without_a_managed_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    root = _managed_install(tmp_path, monkeypatch)
    if missing == "gateway-node":
        (root / "gateway-node").unlink()
    elif missing == "entry":
        (root / "lib" / "node_modules" / "@playwright" / "cli" / "playwright-cli.js").unlink()
    else:
        monkeypatch.setattr(install_mod, "config_dir", lambda: tmp_path / "empty")

    assert ml.ensure_managed_launcher() is False

    assert not (root / "managed-bin").exists()
    assert not (root / ml.GUARD_NAME).exists()
    assert not (tmp_path / "empty").exists()


@posix_only
def test_refresh_refuses_a_symlinked_launcher_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = _managed_install(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "managed-bin").symlink_to(outside)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.browser_cli.install"):
        assert ml.ensure_managed_launcher() is False

    assert list(outside.iterdir()) == []
    assert "could not write the managed playwright-cli launcher" in caplog.text


@posix_only
def test_a_failed_write_is_logged_and_never_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = _managed_install(tmp_path, monkeypatch)

    def refuse(src: object, dst: object) -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(ml.os, "replace", refuse)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.browser_cli.install"):
        assert ml.ensure_managed_launcher() is False

    assert "Read-only file system" in caplog.text
    assert not (root / ml.GUARD_NAME).exists()
    assert not list(root.rglob("*.incoming")), "the staging file is removed"


def test_refresh_is_a_no_op_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(install_mod, "config_dir", lambda: tmp_path / "crew")

    assert ml.ensure_managed_launcher() is False
    assert not (tmp_path / "crew").exists()
