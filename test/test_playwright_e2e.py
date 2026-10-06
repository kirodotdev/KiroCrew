"""Dashboard Playwright E2E suite, folded into ``test_e2e`` (E2eTestCommand).

Boots a real gateway via the same ``spawn_feature_gateway`` harness the smoke
suite uses, then runs the credential-less, crash-free Playwright spec set
(``website/playwright``) against it. Uses Playwright's own bundled Chromium
(``playwright install`` at website-setup time); this OSS fork does not vend a
browser binary.

Gating:
  * ``KIROCREW_E2E`` (set by E2eTestCommand) lifts the skipif, same as the
    smoke suite.
  * Skips gracefully when the in-tree ``website`` dir or its Playwright CLI
    can't be resolved (e.g. a python-only checkout without the built frontend
    dependency).
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import NoReturn

import pytest

# Gate the browser suite behind KIROCREW_E2E so it never runs in the default
# unit-test pass. Applied as a decorator rather than a module-level pytestmark so
# the floor helper's own tests below DO run in the default pass -- an unverified
# guard against silent darkening is no guard.
_requires_e2e = pytest.mark.skipif(
    not os.environ.get("KIROCREW_E2E"),
    reason="E2E Playwright suite. Set KIROCREW_E2E=1 to run.",
)

_WEBSITE = "website"

# Executed-spec floor.
#
# This suite has been silently darkened before: 36 of 103 authored specs were
# excluded by `grepInvert` in playwright.config.ts, so they were never collected
# and never reported as skips. The gate stayed green while a third of the suite
# did not run. An exit code alone cannot catch that, so assert the count.
#
# Every dark spec that was later un-darkened had ALSO rotted -- stale selectors
# for UI that had moved -- because nothing exercised them. That is the argument
# for a floor rather than a periodic audit.
#
# RAISE this when you add specs. Only LOWER it with a written reason in the
# commit body: a drop means specs stopped running.
# The offline browser floor adds ten member memory scenarios to the base floor.
MIN_EXECUTED_SPECS = 239

# Skips are silent passes. A spec should seed its preconditions rather than skip
# when they are absent, so the intended steady state is zero. Specs excluded by
# tag are never collected, so they do not count here.
MAX_SKIPPED_SPECS = 0

# Flaky specs: ones that failed and then passed on a CI retry. Retries are a
# detector, never the fix (docs/ci/e2e-gate.md), so a run that needed MORE retried
# passes than this fails, and each title is printed so the spec gets fixed.
# Measured 0 in two full local passes (323 specs each, CI=1, so retries were on).
# SHRINK-ONLY: it cannot go lower, and raising it hides a flake.
MAX_FLAKY_SPECS = 0

# A bound on the WHOLE Playwright run, taken from the CI job's budget so the run's
# report survives. It is not a lost-run ceiling (that bounds one wait inside a test
# at 10x its measured worst case), and it can stop a slow run that would have
# passed. ci.yml's 25-minute `e2e` job reaches this step about 5 minutes in, and
# the suite took 12.8 min there (run 37340204150; 9-10 min locally). 17 min of
# Playwright plus _STOP_GRACE_SECS leaves about 2 min to read the report and write
# the job summary. It sits below the 1800 s pytest timeout of setup.py test_e2e.
PLAYWRIGHT_RUN_CEILING_SECS = 1020

# A stopped run first gets SIGINT, so Playwright can run its reporters and close
# its browsers, and is killed with its whole process group after this long.
_STOP_GRACE_SECS = 60.0

# How often the run checks that the gateway it drives is still alive. A dead
# gateway turns every remaining spec and every retry into a timeout against
# nothing, which is what a retry must never be mistaken for. Only an exited
# gateway stops the run: a slow one under load is still the subject.
_GATEWAY_POLL_SECS = 2.0


def _read_counts(report: Path) -> tuple[dict, list[str]]:
    """``(stats, flaky titles)`` from Playwright's JSON report; fails if unreadable."""
    try:
        stats = json.loads(report.read_text()).get("stats") or {}
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive
        pytest.fail(f"could not read Playwright JSON report at {report}: {exc}")
    return stats, _flaky_titles(report)


def _counts_line(stats: dict) -> str:
    executed = int(stats.get("expected", 0)) + int(stats.get("flaky", 0))
    skipped = int(stats.get("skipped", 0))
    return f"[test:e2e:playwright] executed={executed} skipped={skipped} stats={stats}"


def _assert_suite_not_darkened(report: Path) -> None:
    """Fail if the run executed fewer specs than the floor, skipped any, or was flaky.

    Reads Playwright's JSON report rather than scraping stdout. `expected` and
    `flaky` both mean "ran and ultimately passed", so both count as executed; a
    flaky spec is then failed by the separate MAX_FLAKY_SPECS ceiling.
    """
    stats, flaky = _read_counts(report)
    executed = int(stats.get("expected", 0)) + int(stats.get("flaky", 0))
    skipped = int(stats.get("skipped", 0))
    print(_counts_line(stats), flush=True)

    assert executed >= MIN_EXECUTED_SPECS, (
        f"only {executed} specs executed, floor is {MIN_EXECUTED_SPECS}. "
        "Specs stopped running rather than failing. Check grepInvert in "
        "website/playwright.config.ts for a newly excluded tag, and check that no "
        "spec file was renamed out of the testDir. If the drop is intended, lower "
        "MIN_EXECUTED_SPECS and say why in the commit body."
    )
    assert skipped <= MAX_SKIPPED_SPECS, (
        f"{skipped} spec(s) skipped, ceiling is {MAX_SKIPPED_SPECS}. A skip reports "
        "green while verifying nothing. Seed the precondition in a fixture instead "
        "of skipping on its absence."
    )
    flaky_count = max(int(stats.get("flaky", 0)), len(flaky))
    assert flaky_count <= MAX_FLAKY_SPECS, (
        f"{flaky_count} spec(s) passed only on a retry, ceiling is {MAX_FLAKY_SPECS}: "
        + ("; ".join(flaky) or "titles not in the report")
        + ". A retry detects a flaky spec, it does not fix it: fix the spec "
        "(website/docs/testing.md § Rules every test keeps), never raise retries or "
        "this ceiling."
    )


def _flaky_titles(report: Path) -> list[str]:
    """``file > describe > title`` for every test the JSON report marks flaky.

    A top-level suite is the spec file, so only nested suite titles (the
    ``describe`` blocks) join the path. Duplicates stay: each is a flaky test.
    """
    try:
        data = json.loads(report.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    found: list[str] = []

    def walk(suite: dict, path: list[str]) -> None:
        for spec in suite.get("specs") or ():
            for test in spec.get("tests") or ():
                if test.get("status") == "flaky":
                    found.append(" > ".join([spec.get("file", "?"), *path, spec.get("title", "?")]))
        for child in suite.get("suites") or ():
            walk(child, path + [child["title"]] if child.get("title") else path)

    for suite in data.get("suites") or ():
        walk(suite, [])
    return sorted(found)


def _salvaged_counts(report: Path) -> tuple[str, list[str]] | None:
    """``(counts line, flaky titles)`` from whatever report a run left, or ``None``.

    Never fails: a stopped run can leave no report or a partial one, and its stop
    reason must stay the failure the reader sees.
    """
    try:
        stats = json.loads(report.read_text()).get("stats") or {}
        return _counts_line(stats), _flaky_titles(report)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _write_step_summary(line: str, flaky: list[str]) -> None:
    """Append the run's counts, and any flaky spec titles, to the job summary.

    Best effort: a runner whose summary file this user cannot write (the CodeBuild
    fleet's) still gets both in the log and in the assertion message.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    body = [f"`{line}`", ""]
    if flaky:
        body.append(f"**{len(flaky)} flaky spec(s)** (passed only on a retry):")
        body.extend(f"- {title}" for title in flaky)
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(body) + "\n")
    except OSError as exc:
        print(f"[test:e2e:playwright] job summary not written ({exc})", flush=True)


def _run_playwright(
    argv: list[str],
    *,
    cwd: Path,
    env: dict,
    gateway,
    poll_secs: float = _GATEWAY_POLL_SECS,
    ceiling_secs: float = PLAYWRIGHT_RUN_CEILING_SECS,
    grace_secs: float = _STOP_GRACE_SECS,
) -> tuple[int | None, str | None]:
    """Run Playwright; return ``(exit code, why it was stopped)``.

    A run that ends on its own returns ``(code, None)``. A run is stopped when the
    gateway it drives exits (retrying against a dead gateway only replays timeouts)
    or when it outlives *ceiling_secs*; it then gets SIGINT so its reporters write,
    and after *grace_secs* its whole process group is killed. Playwright never
    outlives this call, whatever ends it.
    """
    from kiro_crew import platform_compat

    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, start_new_session=True)
    started = time.monotonic()
    try:
        while True:
            try:
                return proc.wait(timeout=poll_secs), None
            except subprocess.TimeoutExpired:
                pass
            elapsed = time.monotonic() - started
            if gateway.proc.poll() is not None:
                diagnostics = getattr(gateway, "diagnostics", None)
                reason = (
                    f"the gateway exited with {gateway.proc.returncode} after {elapsed:.0f}s; "
                    "stopped Playwright instead of letting its retries replay against it"
                    + (f"\n{diagnostics()}" if callable(diagnostics) else "")
                )
                break
            if elapsed > ceiling_secs:
                reason = f"Playwright ran {elapsed:.0f}s, past the {ceiling_secs:.0f}s bound"
                break
        if platform_compat.IS_POSIX:  # Windows has no group SIGINT: killed below
            with contextlib.suppress(OSError):
                platform_compat.kill_process_group(proc.pid, signal.SIGINT)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=grace_secs)
        return proc.returncode, reason
    finally:
        if proc.poll() is None:
            platform_compat.kill_popen_tree(proc)
            proc.wait(timeout=poll_secs * 15)


def _node_major(node_bin: str) -> int | None:
    """Return the major version of ``node_bin``, or None if it can't run."""
    try:
        out = subprocess.check_output(
            [node_bin, "--version"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.match(r"v(\d+)\.", out)
    return int(m.group(1)) if m else None


def _resolve_node18_dir() -> str | None:
    """Return a bin dir holding a real node>=18 binary, or None.

    Playwright 1.58 requires Node>=18. We cannot rely on the ambient ``node``:
    the website dir often pins an older Node via mise (its shim is cwd-sensitive
    and resolves to the pinned version when Playwright runs there), and the
    build env may not expose Node on PATH at all. So scan a prioritized list of
    *concrete* node binaries (never a mise shim, which is cwd-sensitive) and
    return the first dir whose node is >=18; prepending it to PATH makes the
    Playwright shebang resolve it regardless of mise.
    """
    candidates: list[str] = []
    # mise-managed concrete installs (local dev) — not the shim.
    candidates += sorted(
        glob.glob(os.path.expanduser("~/.local/share/mise/installs/node/*/bin/node")),
        reverse=True,
    )
    # Ambient node last; skip mise shims (cwd-sensitive, unreliable here).
    onpath = shutil.which("node")
    if onpath and "/shims/" not in onpath:
        candidates.append(onpath)
    for c in candidates:
        maj = _node_major(c)
        if maj is not None and maj >= 18:
            return str(Path(c).resolve().parent)
    return None


def _resolve_website_dir() -> Path | None:
    """Locate the in-tree ``website`` root (with ``playwright/`` + ``node_modules``).

    Mirrors ``kiro_crew.frontend`` dist resolution: the canonical frontend lives
    in-tree at ``<repo-root>/website``. ``test/`` sits at the repo root, so the
    website is a sibling of this file's parent directory.
    """
    repo_root = Path(__file__).resolve().parent.parent  # KiroCrew repo root
    in_tree = repo_root / _WEBSITE
    return in_tree if (in_tree / "playwright").is_dir() else None


@_requires_e2e
def test_dashboard_playwright_suite() -> None:
    """Boot a gateway and run the credential-less Playwright spec set against it."""

    def _unresolved(msg: str) -> NoReturn:
        # On the required PR gate (KIROCREW_E2E_REQUIRE, set by that step) an
        # environment-resolution miss is a HARD failure: pytest counts a skip as
        # a pass, so the gate would go green having run zero browser specs -- the
        # exact "dead suite, silent UI drift" rot this fold exists to catch. Keep
        # the graceful skip for ad-hoc local/dev runs (marker unset).
        if os.environ.get("KIROCREW_E2E_REQUIRE"):
            pytest.fail(msg)
        pytest.skip(msg)

    website = _resolve_website_dir()
    if website is None:
        _unresolved("website dir not resolvable (no playwright/ dir)")

    pw_bin = website / "node_modules" / ".bin" / "playwright"
    if not pw_bin.exists():
        _unresolved(f"Playwright CLI not found at {pw_bin}")

    node_dir = _resolve_node18_dir()
    if node_dir is None:
        _unresolved("No Node.js >=18 found; Playwright 1.58 requires it")

    # Point the gateway's ACP client at the packaged fake backend so the
    # agent-driven specs (chat, fork) run deterministic, credential-less turns
    # instead of needing real model access. acp/client.py reads
    # KIROCREW_KIRO_BIN and, when set, spawns it as the agent binary; the
    # harness gateway inherits os.environ at spawn time.
    from kiro_crew.testing import fake_acp_backend
    from kiro_crew.testing.harness import spawn_feature_gateway

    prev_kiro_bin = os.environ.get("KIROCREW_KIRO_BIN")
    os.environ["KIROCREW_KIRO_BIN"] = str(fake_acp_backend.__file__)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "playwright-results.json"
            with spawn_feature_gateway(fixture="minimal", approval="reads") as gw:
                from kiro_crew.config.loader import update_config_locked

                # A new-member API request already provisions private memory, so
                # it cannot represent an existing install's uninitialized alias.
                # Seed only this gateway's disposable config before the browser
                # starts. Each CI retry gets a fresh legacy identity; initialization
                # must never be undone just to reset a test.
                legacy_members = [f"memory-e2e-legacy-{attempt}" for attempt in range(3)]
                invalid_memory_bindings = [
                    {
                        "unavailable_member": f"memory-e2e-unavailable-{attempt}",
                        "unavailable_store": f"memory-e2e-missing-store-{attempt}",
                        "mismatched_member": f"memory-e2e-mismatched-{attempt}",
                        "mismatched_store": f"memory-e2e-mismatched-store-{attempt}",
                        "declared_owner": f"memory-e2e-other-owner-{attempt}",
                    }
                    for attempt in range(3)
                ]

                def _seed_legacy_members(data: dict) -> dict:
                    # Unsandboxed consent, for THIS disposable gateway only. The
                    # agent binary here is `fake_acp_backend.py`, a stdlib echo
                    # stub, so OS isolation guards nothing this suite asserts --
                    # and the CodeBuild container CI runs on refuses
                    # `unshare(CLONE_NEWUSER)` at the runtime policy level, which
                    # no sysctl can lift. Without this the browser specs get a
                    # sandbox-refusal card instead of a turn. A real sandboxed
                    # spawn completing real work stays covered by ci.yml's
                    # `e2e-private-namespace` lane, which runs the member sandbox
                    # on a hosted runner with a usable namespace, and by
                    # `e2e-boot-matrix`, which asserts both sandbox tiers.
                    data.setdefault("agent", {})["sandbox_allow_unsandboxed_exec"] = True
                    agents = data.setdefault("agents", {})
                    for name in legacy_members:
                        assert name not in agents
                        agents[name] = {"kiro_agent": "kirocrew", "memory_store": "default"}
                    # Intentional bad configuration, confined to gw.home. These
                    # declarations do not represent healthy peer stores and do
                    # not create or modify any memory directory or database.
                    stores = data.setdefault("memory_stores", {})
                    for broken in invalid_memory_bindings:
                        for kind in ("unavailable", "mismatched"):
                            name = broken[f"{kind}_member"]
                            assert name not in agents
                            agents[name] = {
                                "kiro_agent": "kirocrew",
                                "memory_store": broken[f"{kind}_store"],
                            }
                        store = broken["mismatched_store"]
                        assert store not in stores
                        assert broken["unavailable_store"] not in stores
                        stores[store] = {
                            "memory_version": 2,
                            "owner_member": broken["declared_owner"],
                        }
                    return data

                update_config_locked(gw.home / "config.json", mutate=_seed_legacy_members)
                env = dict(os.environ)
                env.update(
                    {
                        # Prepend a node>=18 bin dir so the playwright shebang
                        # resolves it ahead of any cwd-pinned mise shim.
                        "PATH": node_dir + os.pathsep + env.get("PATH", ""),
                        "PLAYWRIGHT_BASE_URL": f"http://localhost:{gw.port}",
                        "PLAYWRIGHT_TOKEN": gw.token,
                        # Fake ACP backend is wired, so the agent specs (chat/fork)
                        # can run headlessly -- opt them back in.
                        "PLAYWRIGHT_RUN_AGENT_SPECS": "1",
                        # Explicit ephemeral-harness marker: this gateway runs on an
                        # isolated tmp KIROCREW_HOME (spawn_feature_gateway --test-mode),
                        # so its slots are disposable.
                        "KIROCREW_E2E_EPHEMERAL": "1",
                        "KIROCREW_E2E_LEGACY_MEMBERS": json.dumps(legacy_members),
                        "KIROCREW_E2E_INVALID_MEMORY_BINDINGS": json.dumps(invalid_memory_bindings),
                        # CI mode: serial workers + retries:2 (a detector: a spec that
                        # passes only on a retry counts against MAX_FLAKY_SPECS) + html
                        # reporter, per playwright.config.ts.
                        "CI": "1",
                        # Machine-readable counts for the darkening floor below.
                        "PLAYWRIGHT_JSON_OUTPUT_NAME": str(report),
                    }
                )
                print(
                    f"[test:e2e:playwright] base={env['PLAYWRIGHT_BASE_URL']} "
                    f"node_dir={node_dir} fake_acp={fake_acp_backend.__file__} cwd={website}",
                    flush=True,
                )
                # html keeps the CI artifact the config asks for; json adds the
                # counts. A CLI --reporter replaces the config value, so name both.
                rc, stopped = _run_playwright(
                    [str(pw_bin), "test", "--reporter=html,json"],
                    cwd=website,
                    env=env,
                    gateway=gw,
                )
            salvaged = _salvaged_counts(report)
            if salvaged is not None:
                _write_step_summary(*salvaged)
            # A stopped run fails with why it stopped, plus whatever counts its
            # report salvaged. Otherwise assert the counts even on failure: a red run
            # plus a collapsed spec count points at darkening, not at the failure.
            if stopped:
                pytest.fail(f"{stopped}\n{salvaged[0] if salvaged else 'no readable report'}")
            _assert_suite_not_darkened(report)
            assert rc == 0, f"playwright test exited {rc}"
    finally:
        if prev_kiro_bin is None:
            os.environ.pop("KIROCREW_KIRO_BIN", None)
        else:
            os.environ["KIROCREW_KIRO_BIN"] = prev_kiro_bin


# --------------------------------------------------------------------------- #
# Floor-helper unit tests. Ungated on purpose: these need no gateway and no
# browser, and a silently broken floor would never fire when it matters.
# --------------------------------------------------------------------------- #


def _write_report(tmp_path: Path, **stats: int) -> Path:
    report = tmp_path / "results.json"
    report.write_text(json.dumps({"stats": stats}))
    return report


def test_floor_accepts_a_run_at_the_floor(tmp_path: Path) -> None:
    report = _write_report(tmp_path, expected=MIN_EXECUTED_SPECS, flaky=0, skipped=0)
    _assert_suite_not_darkened(report)  # must not raise


def test_floor_counts_flaky_as_executed(tmp_path: Path) -> None:
    """CI runs retries:2, so a flake absorbed by a retry still ran: the floor counts
    it, and only the flaky ceiling fails the run."""
    report = _write_report(
        tmp_path, expected=MIN_EXECUTED_SPECS - 1, flaky=1, skipped=0
    )
    with pytest.raises(AssertionError, match="passed only on a retry"):
        _assert_suite_not_darkened(report)


def test_floor_rejects_a_collapsed_spec_count(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path, expected=MIN_EXECUTED_SPECS - 1, flaky=0, skipped=0
    )
    with pytest.raises(AssertionError, match="specs executed, floor is"):
        _assert_suite_not_darkened(report)


def test_floor_rejects_any_skip(tmp_path: Path) -> None:
    report = _write_report(tmp_path, expected=MIN_EXECUTED_SPECS, flaky=0, skipped=1)
    with pytest.raises(AssertionError, match="skipped, ceiling is"):
        _assert_suite_not_darkened(report)


def test_floor_fails_when_the_report_is_missing(tmp_path: Path) -> None:
    """A missing report must fail loudly, not pass for lack of evidence."""
    # pytest.fail raises Failed, which derives from BaseException, so a plain
    # `pytest.raises(Exception)` would not catch it.
    with pytest.raises(
        pytest.fail.Exception, match="could not read Playwright JSON report"
    ):
        _assert_suite_not_darkened(tmp_path / "absent.json")


def _report_with(tmp_path: Path, tests: list[tuple[str, str]]) -> Path:
    """A JSON report whose specs carry the given ``(title, status)`` tests."""
    report = tmp_path / "results.json"
    specs = [
        {"title": title, "file": "a.spec.ts", "tests": [{"status": status}]}
        for title, status in tests
    ]
    flaky = sum(1 for _title, status in tests if status == "flaky")
    stats = {"expected": MIN_EXECUTED_SPECS, "flaky": flaky, "skipped": 0}
    suites = [{"title": "a.spec.ts", "suites": [{"title": "", "specs": specs}]}]
    report.write_text(json.dumps({"stats": stats, "suites": suites}))
    return report


def test_flaky_titles_are_read_from_nested_suites(tmp_path: Path) -> None:
    report = _report_with(tmp_path, [("opens", "flaky"), ("closes", "expected"), ("x", "flaky")])
    assert _flaky_titles(report) == ["a.spec.ts > opens", "a.spec.ts > x"]


def test_a_flaky_title_names_its_describe_blocks_and_keeps_duplicates(tmp_path: Path) -> None:
    spec = {"title": "opens", "file": "a.spec.ts", "tests": [{"status": "flaky"}]}
    suites = [
        {"title": "a.spec.ts", "suites": [{"title": "menu", "specs": [spec]}]},
        {"title": "a.spec.ts", "suites": [{"title": "menu", "specs": [spec]}]},
    ]
    report = tmp_path / "results.json"
    report.write_text(json.dumps({"stats": {}, "suites": suites}))
    assert _flaky_titles(report) == ["a.spec.ts > menu > opens"] * 2


def test_floor_rejects_more_flaky_specs_than_the_ceiling(tmp_path: Path) -> None:
    tests = [(f"spec {i}", "flaky") for i in range(MAX_FLAKY_SPECS + 1)]
    with pytest.raises(AssertionError, match="passed only on a retry"):
        _assert_suite_not_darkened(_report_with(tmp_path, tests))


def test_the_counts_and_flaky_titles_reach_the_step_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    _write_step_summary("[test:e2e:playwright] executed=3", ["a.spec.ts > opens"])
    text = summary.read_text(encoding="utf-8")
    assert "executed=3" in text and "- a.spec.ts > opens" in text


def test_an_unwritable_step_summary_does_not_fail_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path))  # a directory: open() raises
    _write_step_summary("[test:e2e:playwright] executed=3", ["a.spec.ts > opens"])


def test_the_floor_never_writes_the_job_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    _assert_suite_not_darkened(_write_report(tmp_path, expected=MIN_EXECUTED_SPECS))
    assert not summary.exists()


# Unit-test bounds for _run_playwright: a stop is noticed within a few polls, and a
# child that never gets going is given up on after _TEST_LOST_RUN_SECS.
_TEST_POLL_SECS = 0.05
_TEST_GRACE_SECS = 10.0
_TEST_LOST_RUN_SECS = 60.0


#: The child records its pid in a sibling file and renames it into place, so
#: ``child.pid`` only ever appears complete; a reader never sees it half written.
_CHILD_CODE = (
    "import os, sys, time\n"
    "path = sys.argv[1]\n"
    "with open(path + '.tmp', 'w') as handle:\n"
    "    handle.write(str(os.getpid()))\n"
    "os.replace(path + '.tmp', path)\n"
    "time.sleep(600)\n"
)


def _child(tmp_path: Path, code: str = _CHILD_CODE) -> list[str]:
    """A child that records its pid, then would sleep far past every bound here."""
    import sys

    return [sys.executable, "-c", code, str(tmp_path / "child.pid")]


def _recorded_pid(tmp_path: Path) -> int | None:
    """The child's pid once ``child.pid`` holds a whole positive number, else None."""
    try:
        text = (tmp_path / "child.pid").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() and int(text) > 0 else None


def test_a_run_is_stopped_when_its_gateway_exits(tmp_path: Path) -> None:
    from types import SimpleNamespace

    gateway = SimpleNamespace(
        proc=SimpleNamespace(poll=lambda: 1, returncode=1), diagnostics=lambda: "stderr tail"
    )
    _rc, reason = _run_playwright(
        _child(tmp_path),
        cwd=tmp_path,
        env=dict(os.environ),
        gateway=gateway,
        poll_secs=_TEST_POLL_SECS,
        grace_secs=_TEST_GRACE_SECS,
    )
    assert reason is not None and "the gateway exited with 1" in reason and "stderr tail" in reason


def test_a_run_is_stopped_past_its_bound(tmp_path: Path) -> None:
    from types import SimpleNamespace

    alive = SimpleNamespace(proc=SimpleNamespace(poll=lambda: None, returncode=None))
    _rc, reason = _run_playwright(
        _child(tmp_path),
        cwd=tmp_path,
        env=dict(os.environ),
        gateway=alive,
        poll_secs=_TEST_POLL_SECS,
        ceiling_secs=0.0,
        grace_secs=_TEST_GRACE_SECS,
    )
    assert reason is not None and "past the 0s bound" in reason


def test_playwright_is_reaped_when_the_poll_raises(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from kiro_crew.platform_compat import pid_exists

    started: list[int] = []

    def poll():
        pid = _recorded_pid(tmp_path)
        if pid is None:
            return None
        started.append(pid)
        raise RuntimeError("poll failed")

    gateway = SimpleNamespace(proc=SimpleNamespace(poll=poll, returncode=None))
    with pytest.raises(RuntimeError, match="poll failed"):
        _run_playwright(
            _child(tmp_path),
            cwd=tmp_path,
            env=dict(os.environ),
            gateway=gateway,
            poll_secs=_TEST_POLL_SECS,
            ceiling_secs=_TEST_LOST_RUN_SECS,
        )
    assert started and not pid_exists(started[0])


def test_a_run_that_finishes_reports_its_exit_code(tmp_path: Path) -> None:
    import sys
    from types import SimpleNamespace

    alive = SimpleNamespace(proc=SimpleNamespace(poll=lambda: None, returncode=None))
    argv = [sys.executable, "-c", "raise SystemExit(3)"]
    assert _run_playwright(
        argv,
        cwd=tmp_path,
        env=dict(os.environ),
        gateway=alive,
        poll_secs=_TEST_POLL_SECS,
        ceiling_secs=_TEST_LOST_RUN_SECS,
    ) == (3, None)


def test_a_partial_or_missing_report_salvages_nothing_and_never_raises(tmp_path: Path) -> None:
    report = tmp_path / "results.json"
    assert _salvaged_counts(report) is None
    report.write_text('{"stats": {"expected": 3, "fla')
    assert _salvaged_counts(report) is None
    report.write_text('["not", "an", "object"]')
    assert _salvaged_counts(report) is None
    report.write_text(json.dumps({"stats": {"expected": 3, "skipped": 0}}))
    assert _salvaged_counts(report) == (
        "[test:e2e:playwright] executed=3 skipped=0 stats={'expected': 3, 'skipped': 0}",
        [],
    )
