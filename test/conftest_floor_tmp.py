"""The temp-directory floor: how the run names its temp roots, where ``tempfile``
points, and the report of what a run left behind.

Part of the rootdir ``conftest.py``'s host-mutation floor. That file loads this module by
path and binds the names it needs, so pytest registers every fixture and hook here as
the rootdir conftest's own; see "the floor's own modules" there. Module-level imports
are stdlib, pytest and the floor parts loaded before this one, and nothing here imports
``conftest``: the name is ambiguous, and an in-package app suite runs with ``test/`` off
``sys.path``.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

import pytest
from conftest_floor_home import _tmp_root_prefix_for_run

#: A short, path-shaped temp prefix for Darwin. macOS resolves the per-user temp dir to
#: ``/var/folders/<2 chars>/<30 random chars>/T``, which is both LONG and free of any
#: character outside ``[A-Za-z0-9/]`` -- and two limits the suite has nothing to do with
#: fall straight out of that:
#:
#: * ``sun_path`` for an AF_UNIX socket is 104 bytes on Darwin (108 on Linux), so a
#:   socket under a pytest temp dir cannot be bound at all: ``OSError: AF_UNIX path too
#:   long`` is raised before the behaviour under test runs.
#: * ``security._BARE_SECRET_RUN_RE`` matches a >=40-character run of ``[A-Za-z0-9+/]``,
#:   and ``/`` is IN that class, so a temp path is ONE contiguous run. The 30-character
#:   random segment carries that run over the 4.3-bits/char entropy floor whose whole
#:   job is to keep file paths out, so any temp path echoed through a redactor comes
#:   back ``[REDACTED: credential]`` and the assertion sees a mangled path.
#:
#: Both are properties of the HOST's temp prefix rather than of the code under test:
#: these same tests pass on Linux and in CI, where ``gettempdir()`` is ``/tmp``. Giving
#: macOS the prefix Linux already has is the smallest change that removes both, and it
#: removes them for every test at once instead of teaching each one about a platform
#: limit it is not about.
#:
#: The posture does not change. ``/tmp`` is world-writable with a sticky bit on Linux
#: too, pytest still builds its per-user ``pytest-of-<user>`` tree inside it, and this
#: file's own temp root is still ``mkdtemp``-created with O_EXCL and mode 0700 -- see
#: :func:`_create_tmp_root` for why that is what makes a shared parent safe.
_SHORT_TMP_BASE = "/tmp"


def _prefer_short_tmp_base() -> None:
    """Point this run's temp base at :data:`_SHORT_TMP_BASE` on Darwin.

    Called from ``pytest_configure``, which is early enough: pytest resolves its
    ``basetemp`` lazily from ``tempfile.gettempdir()`` on the first ``mktemp`` /
    ``getbasetemp()``, and that cannot happen before a fixture runs. Both the module
    global and the env vars are set, because ``gettempdir()`` MEMOISES into
    ``tempfile.tempdir`` and a spawned child reads the env instead.
    """
    if sys.platform != "darwin" or not os.path.isdir(_SHORT_TMP_BASE):
        return
    tempfile.tempdir = _SHORT_TMP_BASE
    for name in _TMP_ENV_VARS:
        os.environ[name] = _SHORT_TMP_BASE


# ``_TMP_ROOT_PREFIX`` and ``_tmp_root_prefix_for_run`` are defined in
# ``conftest_floor_home``, beside the import-time data-home floor, rather than here
# beside their main consumer, because ``_create_host_home_floor`` calls the helper when
# that part is loaded -- before this one is -- to name the floor after the run that made
# it. Everything else about the run's temp naming is in this module.


def _create_tmp_root(parent: pathlib.Path) -> pathlib.Path:
    """Create this run's temp root under *parent*, atomically and unguessably.

    ``mkdtemp`` rather than ``mkdir(exist_ok=True)`` on a name derived from the pid, and
    the difference is a local privilege boundary rather than a style choice. The platform
    temp root is world-writable with a sticky bit on POSIX, and a pid-derived name is
    PREDICTABLE -- so another local account can pre-create that exact name as a SYMLINK to
    a directory it controls. ``mkdir(..., exist_ok=True)`` succeeds against a
    symlink-to-directory, the session redirect then follows it, and every temp write in
    the run -- including whatever secrets a test fixture fabricates -- lands somewhere the
    other account chose and can read.

    ``mkdtemp`` closes that in three ways at once: it appends a random component, so the
    name cannot be guessed ahead of time; it creates with ``O_EXCL``, so it fails rather
    than adopting anything that already exists; and it sets mode ``0o700``, so the
    directory is unreadable to other accounts once made. It also makes the name
    UNPREDICTABLE to this suite, which is what lets the invariant below hold: a run can
    only ever name the root it created itself.

    The pid stays in the name for a human reading a stray directory, not for machinery:
    nothing parses it. See :func:`_isolate_tempfile_base` for why this run never touches a
    root it did not create.
    """
    return pathlib.Path(tempfile.mkdtemp(prefix=_tmp_root_prefix_for_run(), dir=parent))


#: Env var carrying this run's SHORT temp root, so xdist workers reuse the controller's.
_SHORT_TMP_ROOT_ENV = "KIROCREW_TEST_SHORT_TMP_ROOT"


#: Env vars ``tempfile`` consults, so a CHILD process inherits the redirect too.
#: A test that spawns a helper which writes to its temp dir would otherwise put
#: that file in the real ``/tmp``, where nothing prunes it.
_TMP_ENV_VARS = ("TMPDIR", "TEMP", "TMP")

#: Opt-in diagnostic: give every test its OWN temp base, named after the test.
#:
#: Off by default because a directory per test is precisely the per-test cost the
#: suite's fixture audit exists to avoid (paid ~26.5k times). It is the escape hatch
#: for the one question the session-scoped ``_isolate_tempfile_base`` cannot answer: a single stray
#: directory in a 26k-test run names no culprit. Re-run the suspect subset with
#: ``KIROCREW_TMP_PER_TEST=1`` and the residue's parent directory IS the test id.
_TMP_PER_TEST_ENV = "KIROCREW_TMP_PER_TEST"

#: Whether this run is in that mode, read ONCE at conftest import.
#:
#: Read here rather than per test because it decides whether the per-test fixture below
#: is DEFINED at all, and an autouse fixture that exists costs whether or not its body
#: does anything: its name joins the fixture closure of every collected item (~106 bytes
#: each, so ~5.8 MiB per xdist worker retained for the whole session) and the fixture
#: protocol sets it up once per test (~50us, ~3 CPU-seconds across a full run). A
#: whole-run operator switch is the right thing to read once: each xdist worker is a
#: fresh process that inherits the environment, so the answer cannot differ between the
#: registration decision and the teardown that reads it.
_TMP_PER_TEST = bool(os.environ.get(_TMP_PER_TEST_ENV))

#: Names under the run's temp base that are NOT this suite's residue.
#:
#: Each entry is something OTHER than a test creating a directory it forgot to remove,
#: which is the only thing this guard is about. Measured from CI, where the surfaces this
#: developer host cannot reach are exercised:
#:
#: * ``pytest-of-`` -- a NESTED pytest's own ``basetemp`` tree. Several tests spawn one,
#:   and it resolves ``gettempdir()`` after the redirect has taken effect, so it computes
#:   its basetemp inside ours. A child runner's bookkeeping, with its own retention.
#: * ``kirocrew-computer-shots`` -- the computer-use screenshot spool, which production
#:   deliberately keeps under ``tempfile.gettempdir()`` as a persistent ring buffer
#:   (pinned by ``test_computer_use_capture.py``). Long-lived BY DESIGN, so its presence
#:   is the feature working, not a test leaking.
#: * ``playwright-`` / ``.org.chromium.`` -- created by the browser and its driver, which
#:   inherit the redirected ``TMPDIR`` like any other child. Third-party scratch this
#:   suite does not own and cannot register cleanup for.
_TMP_RESIDUE_ALLOWED_PREFIXES: tuple[str, ...] = (
    "pytest-of-",
    "kirocrew-computer-shots",
    "playwright-",
    ".org.chromium.",
)

#: Make temp residue FAIL the run rather than warn.
#:
#: The residue is removed and REPORTED either way; this decides only whether the report
#: fails the run. CI's ``Backend Tests*`` jobs in ``ci.yml`` set it, so a test that leaves
#: residue fails the shard it runs in. A local run warns by default, because a developer
#: host has its own tools writing under the redirected ``TMPDIR`` that CI does not have;
#: set it yourself to hold a local run to the CI rule.
_TMP_RESIDUE_STRICT_ENV = "KIROCREW_TMP_RESIDUE_STRICT"


def _redirect_tempfile_base(base: pathlib.Path) -> None:
    """Point ``tempfile`` AND every child process at *base*.

    ``tempfile.tempdir`` is the module global ``gettempdir()`` memoises into, so
    assigning it directly is what covers code already holding a reference to the
    module; the env vars are what cover a child process, which re-derives its own.
    Both are needed: patching only the global leaves subprocess writes in the real
    temp dir, and setting only the env vars is a no-op for a process that already
    resolved ``gettempdir()`` once.

    All three names are set because the platforms disagree on which one is real:
    ``TMPDIR`` is the POSIX spelling and ``TEMP``/``TMP`` are what Windows and the
    tools spawned there read. Setting a name the running platform ignores is inert,
    so there is no need to branch.
    """
    base.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(base)
    for name in _TMP_ENV_VARS:
        os.environ[name] = str(base)


def _remove_tree(path: pathlib.Path) -> bool:
    """Delete *path*, defeating Windows read-only files. True when it is gone.

    ``shutil.rmtree(..., ignore_errors=True)`` is the reflexive spelling and it is the
    WRONG one here: on Windows a mode-444 file cannot be unlinked, a git checkout is
    full of them (loose objects are written read-only), and ``ignore_errors`` swallows
    every such failure so the caller reports success over a tree still on disk. Five
    test modules combine a bare ``mkdtemp()`` with a real ``git`` spawn, so that is not
    hypothetical. ``platform_compat.rmtree_force`` clears the attribute and retries, and
    returns a filesystem-derived boolean rather than the hook's opinion.
    """
    try:
        from kiro_crew import platform_compat as _pc
    except ImportError:  # pragma: no cover - partial checkout
        shutil.rmtree(path, ignore_errors=True)
        return not path.exists()
    return _pc.rmtree_force(path)


def _stop_git_discovery_above_the_temp_roots(base, tmp_path_factory) -> None:
    """Fence git's repository discovery at this run's temp roots.

    A test that builds "a directory that is not a repository" under ``tmp_path`` is
    asserting a property of the HOST unless something bounds git's upward walk: when the
    temp root sits inside a checkout -- an operator with ``TMPDIR=./tmp``, a harness that
    pins its scratch under the worktree -- ``git rev-parse`` climbs out of ``tmp_path`` and
    answers about the enclosing repository instead. One sweep measured 66 tests
    failing exactly that way across two clusters, and the shape is worse in a LINKED
    worktree, where the ``.git`` the walk finds is a FILE and a marker-based probe declines
    before the arm under test ever runs.

    ``GIT_CEILING_DIRECTORIES`` is git's own seam for this and is read by every ``git``
    child the suite spawns, so one write here covers the production helpers a test cannot
    reach. Per-site fixtures still exist and are still correct -- they document intent, and
    they cover the walks this cannot fence (an ``install.sh`` marker search, a nested pytest
    session's ``rootdir``) -- but this is what keeps the NEXT test from inheriting the
    checkout by default. Absolute, symlink-resolved paths: git ignores a ceiling entry that
    is not both.
    """
    entries: list[str] = []
    for candidate in (base, tmp_path_factory.getbasetemp(), os.environ.get(_SHORT_TMP_ROOT_ENV)):
        if not candidate:
            continue
        try:
            resolved = os.path.realpath(str(candidate))
        except OSError:  # pragma: no cover - unreadable temp root
            continue
        if resolved not in entries:
            entries.append(resolved)
    existing = os.environ.get("GIT_CEILING_DIRECTORIES")
    if existing:
        entries.append(existing)
    if entries:
        os.environ["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(entries)  # flake-ok: run-wide fence


def _tmp_residue(base: pathlib.Path, *, per_test: bool) -> list[str]:
    """Names left under *base*, excluding what is not a leak.

    In per-test mode the immediate children are the per-test bases the fixture itself
    created, so the scan descends one level and reports ``<test id>/<name>``. Without
    that, every test in the run would be reported as its own leak and the mode would
    answer nothing. The allow-list is applied to the LEAF in that mode: the by-design
    entries it names (the screenshot spool, a nested basetemp) land inside the per-test
    base, so matching only the base's name would report every one of them as a leak
    of the test that happened to reach the feature.
    """
    try:
        children = sorted(base.iterdir())
    except OSError:
        return []
    residue: list[str] = []
    for child in children:
        if child.name.startswith(_TMP_RESIDUE_ALLOWED_PREFIXES):
            continue
        if not per_test:
            residue.append(child.name)
            continue
        try:
            residue.extend(
                f"{child.name}/{leaf.name}"
                for leaf in sorted(child.iterdir())
                if not leaf.name.startswith(_TMP_RESIDUE_ALLOWED_PREFIXES)
            )
        except OSError:
            continue
    return residue


def _tmp_creator_note(name: str, creators: dict[str, tuple[str, bool, str]]) -> str:
    """Who made *name*, from what ``_note_tmp_creator`` saw in this worker."""
    found = creators.get(name)
    if found is None:
        return "not made by tempfile in this worker: a child process, or a direct mkdir/open"
    nodeid, during, thread_name = found
    on_thread = f" on thread {thread_name!r}" if thread_name else ""
    if not nodeid:
        return (
            f"made by tempfile{on_thread} before the first test "
            "(collection or a session fixture)"
        )
    if during:
        return f"made by tempfile{on_thread} during {nodeid}"
    return (
        f"made by tempfile{on_thread} after {nodeid} finished: "
        "a thread or callback it left running"
    )


def _tmp_residue_report(
    base: pathlib.Path,
    leaked: list[str],
    *,
    per_test: bool,
    creators: dict[str, tuple[str, bool, str]] | None = None,
) -> str:
    """The message naming what the run left behind, and how to find its owner.

    *creators* is what the audit hook recorded for this run's base. Per-test mode gives
    each test its own base, so the hook records nothing there and the name alone answers.
    """
    shown = leaked[:20]
    if creators is not None and not per_test:
        lines = "".join(f"    {name}  <- {_tmp_creator_note(name, creators)}\n" for name in shown)
    else:
        lines = "".join(f"    {name}\n" for name in shown)
    more = f"    ... and {len(leaked) - len(shown)} more\n" if len(leaked) > len(shown) else ""
    hint = (
        "Each name above is a test id, so the leak is in that test."
        if per_test
        else f"Re-run the suspect subset with {_TMP_PER_TEST_ENV}=1 and each residue "
        f"name becomes the id of the test that leaked it."
    )
    return (
        f"{len(leaked)} temporary entr{'y' if len(leaked) == 1 else 'ies'} outlived "
        f"this run under {base}:\n"
        + lines
        + more
        + "\nThis is reported at session teardown, so it is attributed to the last "
        "test this worker ran -- that test is almost certainly NOT the culprit.\n"
        "A test must register the destruction of anything it creates in the SAME "
        "scope. Use pytest's tmp_path, or pair every tempfile.mkdtemp() with "
        "self.addCleanup(shutil.rmtree, path, ignore_errors=True) on the line "
        "after it -- NOT an rmtree in tearDown, which unittest skips entirely when "
        "setUp raises.\n" + hint
    )


if _TMP_PER_TEST:

    @pytest.fixture(autouse=True)
    def _isolate_tempfile_base_per_test(_isolate_tempfile_base, request):
        """Opt-in: give this test its own temp base so a leak names its own test.

        DEFINED only in that mode, rather than defined always with an env-guarded early
        return, so the steady-state cost is zero rather than "one environment read per
        test". The read was never the expensive half: an autouse fixture that exists is
        added to the fixture closure of every collected item (~106 bytes per item, ~5.8
        MiB per xdist worker held for the whole session) and goes through setup/teardown
        once per test (~3 CPU-seconds across a full run) even when its body returns
        immediately -- which, since nothing in the tree sets the variable, it did 100% of
        the time. See ``_TMP_PER_TEST``; the diagnostic itself is unchanged.

        Named from the NODEID, not ``node.name``. The bare function name carries no module
        or class, and 807 function names are duplicated across this suite (``test_defaults``
        appears 17 times, ``test_invalid_json_is_400`` 29), so a name-keyed directory would
        report a leak against a name shared by dozens of tests -- answering the wrong
        question in the one mode that exists to answer it precisely. The nodeid is kept
        TAIL-first under the length cap, because the distinguishing part is at the end.
        """
        safe = "".join(ch if (ch.isalnum() or ch in "-._") else "_" for ch in request.node.nodeid)
        _redirect_tempfile_base(_isolate_tempfile_base / safe[-100:])
