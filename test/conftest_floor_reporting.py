"""Collection-time marks and failure reports: the real-symlink capability
skip, the node id length guard, the tracked gap lists, the hook that stops the item
timeout before teardown reporting, and the CI annotation text.

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
import tempfile

import pytest

#: Repository root, which ``_apply_tracked_gap_list`` and
#: ``pytest_collection_modifyitems`` read the gap lists under. This module lives in
#: ``test/``, one level below the rootdir conftest that defines the same name.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _root_can_create_real_symlink() -> bool:
    """Probe real-link capability for tests collected outside ``test/`` too.

    Ordinary Windows shells commonly lack ``SeCreateSymbolicLinkPrivilege``.
    This is a capability probe, not an OS guess: Developer Mode/elevated Windows
    runners retain the security coverage, while only the exact tests inventoried
    in ``test/requires-real-symlinks.txt`` skip on an incapable host.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "target")
        os.mkdir(target)
        try:
            os.symlink(
                target,
                os.path.join(tmp, "link"),
                target_is_directory=True,
            )
        except (OSError, NotImplementedError, AttributeError):
            return False
        return True


_ROOT_HAS_REAL_SYMLINKS = _root_can_create_real_symlink()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Stop the item timeout before teardown report processing begins.

    All three timed phases are finished once teardown's ``CallInfo`` completes.
    Report serialization and ``logfinish`` must not be interrupted by that timer:
    those hooks run outside every ``CallInfo``, where an alarm would escape the
    protocol instead of becoming a normal test report.
    """
    if call.when == "teardown" and hasattr(item.ihook, "pytest_timeout_cancel_timer"):
        item.ihook.pytest_timeout_cancel_timer(item=item)


# ── tracked Windows gaps apply to every testpath ──────────────────────


#: Ceiling on a test node id. pytest exports the running item's node id as the
#: ``PYTEST_CURRENT_TEST`` environment variable on every setup/call/teardown, and
#: Windows caps one environment variable at 32767 characters -- ``os.environ``
#: raises ``ValueError`` above that, so the item ERRORS at setup on Windows while
#: passing everywhere else. A parametrized case whose value is a large payload (a
#: 700 KB base64 audio blob, once) is how that happens; every report line for the
#: item then carries the blob too, and the Windows shard ran to its 40-minute cap
#: with its log dropped. The margin below the OS limit leaves room for pytest's
#: `` (setup)`` suffix and an xdist group tag. Collection fails with the offending
#: id named, which is the message the shard log never got to show.
MAX_NODEID_CHARS = 30000


def _refuse_oversized_nodeids(items) -> None:
    oversized = [item for item in items if len(item.nodeid) > MAX_NODEID_CHARS]
    if not oversized:
        return
    worst = max(oversized, key=lambda item: len(item.nodeid))
    raise pytest.UsageError(
        f"{len(oversized)} test node id(s) exceed {MAX_NODEID_CHARS} characters "
        f"(longest: {len(worst.nodeid)}, {worst.nodeid[:160]!r}...). Windows caps an "
        "environment variable at 32767 characters and pytest exports the node id as "
        "PYTEST_CURRENT_TEST, so these error at setup there. Give the parametrize "
        "an explicit ids= list instead of letting the payload become the id."
    )


def pytest_collection_modifyitems(config, items):
    """Apply exact capability skips, then the tracked known-gap skips for this OS.

    Real-symlink tests are listed individually rather than intercepting
    ``os.symlink`` globally.  A global interception also catches production
    compatibility helpers before they can handle WinError 1314 and fall back to
    a junction, silently dropping the Windows behavior those tests exist to
    cover.  Exact collection markers leave every non-link path untouched.

    The lists live in ``test/windows-expected-failures.txt``,
    ``test/macos-expected-failures.txt`` and
    ``test/codebuild-expected-failures.txt`` -- one node id per line, captured from
    the first CI runs on that OS or runner. Each is a burn-down backlog: fixed
    tests get their line deleted, and anything NOT on the list still fails the
    job, so the line holds for the tests that pass today. All three go through the
    same ``_apply_tracked_gap_list`` matcher; do not add a fourth mechanism.

    Two of the three are keyed on the OS and the third on the runner, because the
    environment it describes is a Linux one that ``sys.platform`` cannot tell from
    the hosted Linux shards passing the same tests in the same run. See
    :func:`on_self_hosted_runner`.

    Lives HERE rather than in ``test/conftest.py`` because the lists already name node
    ids under ``src/kiro_crew/apps/builtins/auto_improvement/tests/``, and a hook rooted
    at ``test/`` is never registered when only in-package tests are collected -- which is
    exactly what CI's reduced-scope Windows job does when a diff touches no path under
    ``test/``. Those entries are also absent from ``BACKEND_DESELECTS``, so they were
    collected unskipped and the shard went red for a gap that was already tracked.

    The list files themselves stay under ``test/``, read by path from here. Node ids are
    always spelled with ``/`` even on Windows, so the in-package entries need no
    translation.
    """
    _refuse_oversized_nodeids(items)
    if not _ROOT_HAS_REAL_SYMLINKS:
        listfile = _REPO_ROOT / "test" / "requires-real-symlinks.txt"
        try:
            text = listfile.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - list file absent in a partial checkout
            text = ""
        requires_real_symlink = {
            _base_nodeid(ln.strip())
            for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")
        }
        marker = pytest.mark.skip(
            reason="test requires real symlink creation; host lacks that capability"
        )
        for item in items:
            if _base_nodeid(item.nodeid) in requires_real_symlink:
                item.add_marker(marker)

    pc = platform_compat_or_none()
    if pc is None:
        return
    if pc.IS_WINDOWS:
        _apply_tracked_gap_list(items, "windows-expected-failures.txt", "Windows")
    elif pc.IS_MACOS:
        _apply_tracked_gap_list(items, "macos-expected-failures.txt", "macOS")
    elif on_self_hosted_runner():
        _apply_tracked_gap_list(
            items, "codebuild-expected-failures.txt", "self-hosted Linux runner"
        )


def on_self_hosted_runner() -> bool:
    """Whether this run is on a self-hosted CI runner rather than a hosted one.

    The third gap list is keyed on the RUNNER rather than the OS, because the
    environment that fails those tests is a Linux one: no IPv6 loopback, no ``link``
    on the filesystem, and an unprivileged user whose resolved home is ``/root``.
    ``sys.platform`` cannot tell it from the hosted Linux shards that pass the same
    tests in the same run, so the list would either apply everywhere or nowhere.

    ``RUNNER_ENVIRONMENT`` is GitHub's own answer to that question, and ``ci.yml``
    already gates this job's non-root boundary assertion on the same value
    (``runner.environment == 'self-hosted'``). Keying off it reuses the signal the
    workflow already treats as identifying the runner instead of inventing a second
    one, and needs nothing added to the job's environment.

    Absent outside CI, so a developer machine reads as hosted and applies no list --
    which is right: a local run has none of the three constraints.
    """
    return os.environ.get("RUNNER_ENVIRONMENT") == "self-hosted"


def _apply_tracked_gap_list(items, listname: str, platform_label: str) -> None:
    """Mark every collected item named in ``test/<listname>`` as a STRICT xfail.

    ONE mechanism serves all three gap lists. macOS and the self-hosted Linux runner
    reuse it rather than growing a second matcher, so the node-id spelling rule
    (never ``@group``; ``[params]`` optional, and selective when present -- see the
    parametrization paragraph below) and the burn-down semantics --
    anything NOT listed still fails the job -- are identical for every list by
    construction.

    **``xfail(strict=True)``, not ``skip``, because these files call themselves a
    burn-down backlog and say "fix the test and DELETE the line".** A skip does not
    execute the test, so nothing can ever report that a gap CLOSED: the list could
    only ever grow, and 241 node ids were retired rather than tracked. Under a
    strict xfail a listed test that still fails is green (the gap is known), and one
    that starts PASSING fails the job with XPASS -- which is precisely the signal
    that the line should be deleted, delivered by the same run that earned it.

    Safe for the entries whose failure is intended platform behaviour rather than a
    gap: those still FAIL on the platform they are listed for, so they xfail green
    exactly as before. They only never burn down, which is an honesty problem with
    such an entry's framing and not a mis-fire of this mechanism.

    **A listed test that HANGS must not be in this list, and the platform is why.**
    Executing rather than skipping costs CI minutes, which is fine; what is not
    fine is a hang. On Windows there is no ``SIGALRM``, so pytest-timeout cannot
    interrupt the test -- it kills the process, which is why the Windows shards pass
    ``--max-worker-restart=0``. MEASURED: one listed entry
    (``test_config_loader_workspace_bindings.py::TestAgentWorkspaceBindingsProperties::test_workspace_path_resolution``)
    hangs on a native Windows host, and running the list killed the session at 22%
    with no summary line printed at all. A hanging test therefore cannot be tracked
    here; it needs an explicit ``skipif`` at the test, with the reason, so nothing
    about it is silent.

    **An entry MAY name one parametrization, and that is what makes strict xfail
    expressible at all.** Stripping ``[params]`` from every entry (see
    :func:`_base_nodeid` for why the strip exists) means one line covers every
    parametrization of a test -- which is right when they all fail, and impossible
    when they do not: ``test_seed_audit_uses_rail_tag_not_raw_path`` has two params
    that fail on Windows and one that passes, so a single base entry would either
    un-track the two or red the job forever on the one. So a line WITHOUT ``[`` is
    matched against the param-stripped id, exactly as before, and a line WITH ``[``
    is matched against the id with its params intact. The ``@group`` suffix is
    stripped on both sides either way, because that one is an xdist artifact rather
    than part of the test's identity.
    """
    listfile = _REPO_ROOT / "test" / listname
    try:
        text = listfile.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - list file absent in a partial checkout
        return
    entries = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    expected_base = {_base_nodeid(e) for e in entries if "[" not in e}
    expected_exact = {_ungrouped_nodeid(e) for e in entries if "[" in e}
    if not expected_base and not expected_exact:
        return
    marker = pytest.mark.xfail(
        reason=f"known {platform_label} gap -- tracked in test/{listname}; "
        "delete the line when it starts passing",
        strict=True,
    )
    for item in items:
        if (
            _base_nodeid(item.nodeid) in expected_base
            or _ungrouped_nodeid(item.nodeid) in expected_exact
        ):
            item.add_marker(marker)


def _ungrouped_nodeid(nodeid: str) -> str:
    """A node id with only the xdist ``@group`` suffix removed, params kept.

    The counterpart to :func:`_base_nodeid` for a gap-list entry that names ONE
    parametrization. ``@group`` still has to go -- it is added by
    ``--dist loadgroup`` and absent under ``-n0``, so leaving it in would make the
    same entry match in one invocation and not the other -- but ``[params]`` is part
    of what the entry is identifying and stays.
    """
    return nodeid.split("@")[0]


def _base_nodeid(nodeid: str) -> str:
    """A node id reduced to file + class + function, with params and xdist group gone.

    Under ``--dist loadgroup`` -- which ``setup.cfg`` supplies to EVERY run -- xdist
    rewrites each item's nodeid to ``<nodeid>@<group>`` for anything carrying an
    ``xdist_group`` mark. Splitting on ``[`` alone therefore compares different strings
    in different invocations: a PARAMETRIZED id loses the suffix with its params, while
    an UNPARAMETRIZED one keeps it. So a plain entry silently stopped matching the
    grouped tests under the default invocation, and an entry written WITH the suffix to
    compensate then stopped matching under ``-n0``, where xdist never adds it.

    Stripping both parts makes one spelling -- the plain node id -- correct in every
    mode, which is the only spelling the list file documents.
    """
    return nodeid.split("[")[0].split("@")[0]


def platform_compat_or_none():
    """``kiro_crew.platform_compat``, or ``None`` when it cannot be imported.

    Imported lazily so the rootdir floor keeps its module-level imports to the
    stdlib: it is loaded before every collection, and a module-scope import of the
    package under test would make the whole suite depend on that package's import-time
    side effects.
    """
    try:
        from kiro_crew import platform_compat
    except ImportError:  # pragma: no cover - partial checkout
        return None
    return platform_compat


#: Failures that get their own annotation before the rest are only counted.
#: GitHub keeps a bounded number per check run and drops the excess with no
#: notice, so past this point the annotation list stops being something anyone
#: reads and the short test summary in the log is the better artifact.
_MAX_ANNOTATED_FAILURES = 10

#: Annotation messages are cut to this many characters. A full assertion diff is
#: a page long, does not fit the check-run UI, and is already in the log.
_MAX_ANNOTATION_CHARS = 400


def _gha_escape(value: str, *, is_property: bool) -> str:
    """Escape *value* for a GitHub Actions workflow command.

    The runner splits a command on ``,`` and on ``::``, and every pytest node id
    contains ``::`` -- unescaped, the annotation is truncated at the first one
    and names the FILE instead of the test, which is most of the defect this
    exists to fix. Property values need the two structural characters escaped on
    top of the data set, per the workflow-commands spec.
    """
    escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if is_property:
        escaped = escaped.replace(":", "%3A").replace(",", "%2C")
    return escaped


def _report_location(report: object) -> tuple[str, int | None]:
    """Repository-relative path and 1-based line for *report*, best effort.

    ``report.location`` is a ``(path, lineno, domain)`` triple whose line is
    0-BASED, while an annotation line is 1-based. A collection error carries no
    line at all, and a guessed one points the reader at the wrong statement, so
    ``None`` means "omit ``line=``" rather than "line 1".
    """
    location = getattr(report, "location", None) or ()
    path = location[0] if len(location) >= 1 and isinstance(location[0], str) else ""
    if not path:
        path = str(getattr(report, "fspath", "") or "")
    raw_line = location[1] if len(location) >= 2 else None
    line = raw_line + 1 if isinstance(raw_line, int) and raw_line >= 0 else None
    return path, line


def _cut(reason: str) -> str:
    """*reason* trimmed to :data:`_MAX_ANNOTATION_CHARS`."""
    reason = reason.strip()
    if len(reason) > _MAX_ANNOTATION_CHARS:
        return reason[: _MAX_ANNOTATION_CHARS - 3] + "..."
    return reason


def _failure_reason(report: object) -> str:
    """One-line reason for *report*, cut to :data:`_MAX_ANNOTATION_CHARS`.

    Prefers the line pytest marks with ``E`` in the first column, which is the
    error itself. ``longrepr.reprcrash.message`` looks like the obvious source
    and is right for an assertion, but for a FIXTURE error it is the preamble --
    MEASURED, an annotation built from it reads ``file <path>, line 5`` and
    spends its whole width saying nothing, while the ``E`` line two rows down
    says ``fixture 'x' not found``. Source lines in the same block are indented,
    so anchoring at column 0 does not mistake a statement for the verdict.

    ``reprcrash`` is the fallback, then the first rendered line; a report with
    none of the three still gets an annotation, because the node id was the part
    that was missing.
    """
    rendered = str(getattr(report, "longreprtext", "") or "")
    for raw in rendered.splitlines():
        if raw.startswith("E ") and raw[1:].strip():
            return _cut(raw[1:])
    crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
    reason = str(getattr(crash, "message", "") or "")
    if not reason.strip():
        reason = rendered
    lines = [line for line in reason.strip().splitlines() if line.strip()]
    if not lines:
        return "no failure detail in the report"
    return _cut(lines[0])
