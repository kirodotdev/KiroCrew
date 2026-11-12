"""Loading the embedding model never redirects the process's standard streams.

The vendored binding wraps its native load in ``suppress_stdout_stderr``, which
upstream implements by pointing ``sys.stdout``, ``sys.stderr`` and descriptors 1
and 2 at the null device for the whole process. The load runs on a background
thread, so a line another thread wrote meanwhile was discarded: the gateway's
``--json-ready`` line, a log record, a traceback, and the output of any child
started in that window. ``embeddings_native._quiet_native_log_without_redirect``
gives the block a narrower meaning (llama.cpp's own log is silent); these tests
hold it open on an event and write from another thread.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
import types
from collections import Counter
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew import embeddings_native
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_SRC = Path(kiro_crew.__file__).resolve().parent

#: A load whose native part is a stand-in, around the REAL vendored ``_utils.py``
#: and ``_logger.py``: the constructor has the vendored one's shape (set the log
#: level, then load inside the quiet block), reports through the callback the
#: binding registers, and stays inside the block until the main thread has written.
_HELD_LOAD = r"""
import os, subprocess, sys, threading, types
from kiro_crew import embeddings_native

verbose = sys.argv[1] == "verbose"
registered = []
package = types.ModuleType("llama_cpp")
package.__path__ = [str(embeddings_native._VENDOR_DIR / "llama_cpp")]
package.llama_log_callback = lambda function: function
package.llama_log_set = lambda callback, user_data: registered.append(callback)
sys.modules["llama_cpp"] = package
import llama_cpp._logger, llama_cpp._utils

held, release = threading.Event(), threading.Event()


class Llama:
    def __init__(self, *, verbose):
        native_log = registered[-1]
        llama_cpp._logger.set_verbose(verbose)
        with llama_cpp._utils.suppress_stdout_stderr(disable=verbose):
            native_log(3, b"NATIVE error during load\n", None)
            native_log(0, b"NATIVE unlevelled during load\n", None)
            native_log(1, b"NATIVE info during load\n", None)
            held.set()
            if not release.wait(60):
                raise SystemExit("the load was never released")
        native_log(3, b"NATIVE error after load\n", None)


package.Llama = Llama
llama_cls = embeddings_native._load_llama_class()
assert llama_cls is Llama, llama_cls

print("OUT before load", flush=True)
print("ERR before load", file=sys.stderr, flush=True)
load = threading.Thread(target=llama_cls, kwargs={"verbose": verbose}, name="kc-embed-load")
load.start()
if not held.wait(60):
    raise SystemExit("the load never started")
print('KIROCREW_READY:{"port": 1}', flush=True)
print("ERR log record during load", file=sys.stderr, flush=True)
os.write(1, b"OUT raw descriptor during load\n")
os.write(2, b"ERR raw descriptor during load\n")
subprocess.run(
    [
        sys.executable,
        "-c",
        "import sys; print('OUT child started during load');"
        " print('ERR child started during load', file=sys.stderr)",
    ],
    check=True,
    timeout=60,
)
release.set()
load.join(60)
print("OUT after load", flush=True)
print("ERR after load", file=sys.stderr, flush=True)
"""

#: The real runtime refusing a file that is not a model: llama.cpp reports that
#: on its log at error level, which is the output a quiet load has to hide.
_REFUSED_LOAD = r"""
import sys
from kiro_crew import embeddings_native

llama_cls = embeddings_native._load_llama_class()
if llama_cls is None:
    raise SystemExit("the vendored llama.cpp runtime did not load")
if not embeddings_native._NativeQuiet.callbacks:
    raise SystemExit("the binding kept its own redirect: the quiet callback is not installed")
try:
    llama_cls(model_path=sys.argv[2], embedding=True, verbose=sys.argv[1] == "verbose")
except ValueError:
    print("OUT refused", flush=True)
"""


def _child(script: str, cwd: Path, *argv: str, env: dict[str, str] | None = None):
    """Run *script* in a child whose stdout and stderr are pipes."""
    done = subprocess.run(
        [sys.executable, "-c", script, *argv],
        cwd=cwd,
        # The child imports vendored modules from the source tree; it must not
        # leave their bytecode there.
        env={**(env or os.environ), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done


def _marked(text: str) -> list[str]:
    """The lines a child script wrote, without anything its interpreter added."""
    return [
        line
        for line in text.splitlines()
        if line.startswith(("OUT ", "ERR ", "NATIVE ", "KIROCREW_READY:"))
    ]


def _held_load(tmp_path: Path, mode: str) -> tuple[list, list]:
    if embeddings_native._platform_libs_dirname() is None:
        pytest.skip("no vendored llama.cpp runtime for this platform")
    operator_libs = tmp_path / "operator-libs"
    operator_libs.mkdir()
    # An operator's library directory skips the bundled-runtime host checks, so
    # the loader reaches its import on every supported platform.
    env = {**os.environ, embeddings_native._LIB_PATH_ENV: str(operator_libs)}
    done = _child(_HELD_LOAD, tmp_path, mode, env=env)
    return _marked(done.stdout), _marked(done.stderr)


_WRITTEN_DURING_LOAD_OUT = [
    "OUT before load",
    'KIROCREW_READY:{"port": 1}',
    "OUT raw descriptor during load",
    "OUT child started during load",
    "OUT after load",
]


def test_what_other_threads_write_while_the_model_loads_arrives(tmp_path: Path) -> None:
    """The ready line, a log record, a raw descriptor write and a child's output all
    reach the pipes, llama.cpp's own lines do not, and its log is back at its own
    level once the load ends (the error after the load prints, as it always has)."""
    out, err = _held_load(tmp_path, "quiet")
    assert out == _WRITTEN_DURING_LOAD_OUT
    assert err == [
        "ERR before load",
        "ERR log record during load",
        "ERR raw descriptor during load",
        "ERR child started during load",
        "NATIVE error after load",
        "ERR after load",
    ]


def test_a_verbose_load_still_prints_what_the_runtime_reports(tmp_path: Path) -> None:
    out, err = _held_load(tmp_path, "verbose")
    assert out == _WRITTEN_DURING_LOAD_OUT
    assert err == [
        "ERR before load",
        "NATIVE error during load",
        "NATIVE unlevelled during load",
        "NATIVE info during load",
        "ERR log record during load",
        "ERR raw descriptor during load",
        "ERR child started during load",
        "NATIVE error after load",
        "ERR after load",
    ]


def _bundled_runtime_cannot_run_here() -> str | None:
    """Why this host cannot run the bundled runtime, by the loader's own host checks.

    Only a host limit skips the test below. A runtime that fails to load on a
    host that passes these checks is a defect, and the child then exits non-zero.
    """
    libs = embeddings_native._platform_libs_dirname()
    if libs is None:
        return "no vendored llama.cpp runtime for this platform"
    if libs == "win_amd64" and embeddings_native._missing_windows_msvc_runtime():
        return "the Microsoft Visual C++ runtime is not installed"
    if libs == "linux_x86_64":
        flags = embeddings_native._linux_x86_64_cpu_flags()
        if flags is None or embeddings_native._LINUX_X86_64_REQUIRED_CPU_FLAGS - flags:
            return "this CPU lacks an instruction the bundled build needs"
    if libs == "macos_x86_64" and embeddings_native._macos_x86_64_missing_cpu_flags() != []:
        return "this CPU lacks an instruction the bundled build needs"
    return None


def test_the_real_runtime_reports_nothing_when_a_quiet_load_fails(tmp_path: Path) -> None:
    """With the vendored native library itself: a file that is not a model is
    refused, llama.cpp says why on its log, and none of that reaches the pipes
    unless the load was verbose."""
    not_a_model = tmp_path / "not-a-model.gguf"
    not_a_model.write_bytes(b"not a gguf file, just bytes\n")
    cannot_run = _bundled_runtime_cannot_run_here()
    if cannot_run:
        pytest.skip(cannot_run)
    quiet = _child(_REFUSED_LOAD, tmp_path, "quiet", str(not_a_model))
    verbose = _child(_REFUSED_LOAD, tmp_path, "verbose", str(not_a_model))
    assert _marked(quiet.stdout) == _marked(verbose.stdout) == ["OUT refused"]
    reported = set(verbose.stderr.splitlines())
    assert any("failed to load model" in line for line in reported), verbose.stderr
    assert not reported & set(quiet.stderr.splitlines())


def _vendored(name: str, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """The real vendored ``llama_cpp.<name>`` module, loaded for this test only."""
    spec = importlib.util.spec_from_file_location(
        f"llama_cpp.{name}", embeddings_native._VENDOR_DIR / "llama_cpp" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, f"llama_cpp.{name}", module)
    return module


@pytest.fixture
def binding(monkeypatch: pytest.MonkeyPatch):
    """The real vendored quiet block and logger around a stand-in native library.

    Yields the block, the callback llama.cpp would call, ``set_verbose``, and
    every registration llama.cpp received: the callback, and whether the
    install lock was held when it arrived.
    """
    # Loading the vendored files must not leave their bytecode in the source tree.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    registered: list = []
    package = types.ModuleType("llama_cpp")
    package.llama_log_callback = lambda function: function  # type: ignore[attr-defined]
    package.llama_log_set = (  # type: ignore[attr-defined]
        lambda callback, user_data: registered.append(
            (callback, embeddings_native._NativeQuiet.lock.locked())
        )
    )
    monkeypatch.setitem(sys.modules, "llama_cpp", package)
    utils = _vendored("_utils", monkeypatch)
    logger_module = _vendored("_logger", monkeypatch)
    level = logger_module.logger.level
    logger_module.set_verbose(False)
    embeddings_native._quiet_native_log_without_redirect()
    try:
        yield utils.suppress_stdout_stderr, registered[-1][0], logger_module.set_verbose, registered
    finally:
        logger_module.logger.setLevel(level)
        utils.outnull_file.close()
        utils.errnull_file.close()


def test_loads_that_overlap_stay_quiet_until_the_last_one_ends(binding, capsys) -> None:
    block, native_log, _set_verbose, _registered = binding
    first, second = block(disable=False), block(disable=False)
    first.__enter__()
    second.__enter__()
    first.__exit__(None, None, None)
    native_log(3, b"NATIVE while one load is still open\n", None)
    second.__exit__(None, None, None)
    native_log(3, b"NATIVE after both\n", None)
    assert capsys.readouterr().err == "NATIVE after both\n"


def test_a_second_load_starting_does_not_end_the_silence_of_the_first(binding, capsys) -> None:
    """Every constructor starts with ``set_verbose``, which resets the binding's
    logger. A model swapped in while another is still loading must not make the
    first one's lines print."""
    block, native_log, set_verbose, _registered = binding
    with block(disable=False):
        set_verbose(False)
        native_log(3, b"NATIVE error of the load still open\n", None)
        with block(disable=False):
            native_log(3, b"NATIVE error of the second load\n", None)
    assert capsys.readouterr().err == ""


def test_a_load_that_fails_ends_its_silence_and_a_reload_is_quiet_again(binding, capsys) -> None:
    block, native_log, _set_verbose, _registered = binding
    with pytest.raises(ValueError, match="no such model"):
        with block(disable=False):
            raise ValueError("no such model")
    native_log(3, b"NATIVE between loads\n", None)
    with block(disable=False):
        native_log(3, b"NATIVE during the reload\n", None)
    assert capsys.readouterr().err == "NATIVE between loads\n"


def test_the_two_halves_are_installed_once_and_under_one_lock(binding) -> None:
    """Two threads can both reach the installer first (the loader's cache does not
    serialise them). A second registration would replace the callback llama.cpp
    holds, and releasing the first would leave it calling freed memory."""
    _block, native_log, _set_verbose, registered = binding
    assert registered[-1][1], "registered outside the install lock"
    before = len(registered)
    embeddings_native._quiet_native_log_without_redirect()
    assert len(registered) == before
    assert any(kept is native_log for kept in embeddings_native._NativeQuiet.callbacks)


def test_the_quiet_block_leaves_the_streams_and_their_descriptors_alone(binding) -> None:
    block, _native_log, _set_verbose, _registered = binding
    streams = (sys.stdout, sys.stderr)
    descriptors = [(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in (1, 2)]
    with block(disable=False):
        assert (sys.stdout, sys.stderr) == streams
        assert [(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in (1, 2)] == descriptors


# ── Ratchet: no new process-wide redirect ────────────────────────────────────

#: Every place under ``src/kiro_crew`` that re-points a standard stream or a
#: descriptor for the whole process, with the number of such statements and why
#: each is safe. A redirect is safe only when no other thread can be writing
#: (the first statements of a child's entry point, a forked child before it
#: execs), or when nothing written is discarded. A temporary redirect inside a
#: long-lived process is neither: it is how the ready line was lost. Add a row
#: only for one of those two cases; otherwise silence the writer itself.
_ALLOWED_REDIRECTS: dict[str, tuple[int, str]] = {
    "_process_group_supervisor.py": (1, "a forked child, before it execs ps"),
    "_vendor/llama_cpp/_utils.py": (
        8,
        "upstream's suppress_stdout_stderr; embeddings_native replaces its enter and"
        " exit, so these statements never run (the tests above hold it open)",
    ),
    "cli_logging.py": (
        1,
        "a detached gateway's descriptors 1 and 2 already point at gateway.log and"
        " follow it across a rename; the null device only once the log cannot be written",
    ),
    "connections/l1_smoke.py": (1, "stdout is already dead; rebound so shutdown can flush"),
    "consolidation_write_child.py": (4, "first statements of a child's entry point"),
    "crew_log/slot_backfill_child.py": (2, "first statements of a child's entry point"),
    "desktop_stdio.py": (1, "first call of an entry point, when the desktop shell names a log"),
    "embeddings_child.py": (4, "first statements of a child's entry point"),
    "pod/cli.py": (1, "`pod prune --json`, a one-shot command; the lines are re-printed"),
    "recall_pool.py": (4, "first statements of a child's entry point"),
    "sandbox_pin_scan_child.py": (2, "first statements of a child's entry point"),
}

_STREAMS = frozenset({"stdout", "stderr", "__stdout__", "__stderr__"})
_REDIRECT_CALLS = frozenset({"dup2", "redirect_stdout", "redirect_stderr", "SetStdHandle"})
#: Cheap text screen, so only the handful of files that can match are parsed.
_SCREEN = re.compile(
    r"dup2|redirect_std(?:out|err)|SetStdHandle|sys\.(?:__)?std(?:out|err)(?:__)?\s*=[^=]"
)


def _is_std_stream(node: ast.expr) -> bool:
    """``sys.stdout`` / ``self.sys.stderr`` / ``sys.__stdout__`` as an assignment target."""
    if not (isinstance(node, ast.Attribute) and node.attr in _STREAMS):
        return False
    owner = node.value
    return (isinstance(owner, ast.Name) and owner.id == "sys") or (
        isinstance(owner, ast.Attribute) and owner.attr == "sys"
    )


def _redirects(source: str) -> int:
    count = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            called = node.func
            name = getattr(called, "attr", None) or getattr(called, "id", None)
            count += name in _REDIRECT_CALLS
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            count += sum(_is_std_stream(target) for target in targets)
    return count


def _is_test_file(relative: Path) -> bool:
    return (
        relative.name.startswith("test_")
        or relative.name == "conftest.py"
        or any(part in {"tests", "container_tests"} for part in relative.parts)
    )


def test_no_new_code_redirects_a_standard_stream_for_the_whole_process() -> None:
    found: Counter[str] = Counter()
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC)
        if _is_test_file(relative):
            continue
        source = path.read_text(encoding="utf-8")
        if _SCREEN.search(source):
            found[relative.as_posix()] += _redirects(source)
    assert +found == {name: count for name, (count, _why) in _ALLOWED_REDIRECTS.items()}
