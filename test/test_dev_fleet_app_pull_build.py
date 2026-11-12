"""Dev Fleet Pull + Build: the npm preflight, the node_modules stash and repair seams.

Covers the preflight between fetch and merge, the stash recovery, the failure cause,
the step-diagnosis stamp gate, sync-ref pruning and the preflight snapshot.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _sweep_staged_sync_tempdirs  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture
from dev_fleet_app_helpers import _LAST_CLEANUP_PATHS, _SYNC_REPO, _run_sync, _sync_step_argvs

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import npm_preflight
from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.apps.builtins.dev_fleet import worktree_ops as worktree_ops_mod

# `npm ci` deletes node_modules before installing, so a registry refusal mid-reify
# would leave the checkout with new source and no frontend dependencies. These pin
# the properties that make that failure a no-op.


@pytest.mark.asyncio
async def test_sync_preflights_between_fetch_and_merge(monkeypatch):
    """The probe must sit AFTER fetch and BEFORE merge.

    The incoming lockfile is knowable only once fetch lands, and fetch moves
    nothing but remote refs, so that is the one moment a refusal costs nothing.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, steps = await _run_sync(mod, [])
    labels = [s["label"] for s in steps]

    assert mod._PREFLIGHT_LABEL in labels, labels
    # The merge is labelled apart from the fetch so the stepper does not read
    # Pull -> Preflight -> Pull, like a restarted run.
    assert labels[0] == "Pull", labels
    assert labels.index(mod._PREFLIGHT_LABEL) == 1, labels
    assert labels[2] == "Merge", labels
    assert labels.count("Pull") == 1, labels
    assert labels.index("pip install") > labels.index(mod._PREFLIGHT_LABEL), labels


@pytest.mark.asyncio
async def test_sync_preflight_probes_the_incoming_ref_not_the_working_tree(monkeypatch):
    """It must read the lockfile from the FETCHED ref, not the working tree."""
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    argvs = await _sync_step_argvs(monkeypatch)
    probe = [a for a in argvs if any("npm_preflight" in str(x) for x in a)]
    assert probe, f"no preflight step in {argvs}"
    argv = probe[0]
    assert argv[0] == sys.executable, argv
    assert "--ref" in argv
    ref = argv[argv.index("--ref") + 1]
    # The ref the fetch step pinned, not a path and not a mutable remote-tracking
    # name the refresher could move to a revision the merge may not install.
    assert ref == mod._sync_base_ref(), ref
    assert not ref.endswith(f"/{mod.BASE_BRANCH}"), ref
    assert not ref.startswith("/") and not ref.startswith("."), ref


@pytest.mark.asyncio
async def test_sync_skips_the_preflight_on_an_edition_checkout(monkeypatch):
    """The edition path runs no npm, so a probe could only produce a false refusal."""
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: True)
    _, cmd, steps = await _run_sync(mod, [])
    assert mod._PREFLIGHT_LABEL not in [s["label"] for s in steps]


@pytest.mark.asyncio
async def test_npm_ci_step_carries_a_node_modules_stash(monkeypatch):
    """The transaction is attached to the npm ci step, and ONLY to it.

    It cannot be a later "restore" step: the runner is fail-fast, so nothing runs
    after the failed step that needs the restore.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, steps = await _run_sync(mod, [])
    stashed = {s["label"]: s["stash"] for s in steps if s.get("stash")}
    assert list(stashed) == ["npm ci"], stashed
    assert stashed["npm ci"] == str(Path(_SYNC_REPO) / "website" / "node_modules")
    # The transaction's behaviour is driven against real trees in
    # test_dev_fleet_sync_runner.py; this pins only that the stash rides npm ci.


@pytest.mark.asyncio
async def test_sync_never_runs_an_operator_supplied_command(monkeypatch):
    """No configurable command executes on the sync path. This is a RATCHET.

    An operator-declared credential-repair command runs a FILE an agent can
    rewrite, against the operator's credentials; no argv[0] check can make the
    declared command the code that runs, so it does not belong on an unattended
    path. Adding one needs its own threat model.
    """
    monkeypatch.setenv("KIROCREW_DEVFLEET_NPM_AUTH_REPAIR", "/bin/sh -c true")
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, steps = await _run_sync(mod, [])

    assert not any("repair" in s["label"] for s in steps), steps
    assert not hasattr(mod, "_npm_auth_repair_argv")
    cmd_flat = " ".join(map(str, cmd))
    for token in ("KIROCREW_DEVFLEET_NPM_AUTH_REPAIR", "npm_auth_repair"):
        assert token not in cmd_flat, cmd
    # Checked by absence, not an argv[0] allowlist: toolchain paths differ by
    # host (/usr/bin/npm vs /opt/homebrew/bin/npm).
    assert "/bin/sh" not in cmd_flat
    for s in steps:
        assert "/bin/sh" not in " ".join(map(str, s["argv"])), s["argv"]
        assert not any("repair" in str(v) for v in s.get("env", {}).values()), s


@pytest.mark.asyncio
async def test_a_stashed_tree_is_recovered_before_any_step_runs(monkeypatch):
    """Recovery must not sit behind the steps that precede the transaction.

    A run killed after the move-aside leaves the tree absent and the backup
    unclaimed; if recovery waited for earlier steps, a failing preflight would
    leave it missing. Reconciliation therefore happens before the step loop.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    from kiro_crew.apps.builtins.dev_fleet import sync_runner

    _, cmd, steps = await _run_sync(mod, [])
    assert cmd is not None
    # Behaviour is driven in test_dev_fleet_sync_runner.py (TestReconcileLeftovers,
    # TestNodeModulesTransaction); here, only the ordering in main().
    src = Path(sync_runner.__file__).read_text(encoding="utf-8")
    main_body = src.split("def main(", 1)[1]
    assert main_body.index("reconcile_leftovers(") < main_body.index(
        "run_steps("
    ), "the stashed tree must be reclaimed before any step runs"
    enter = src.split("def __enter__", 1)[1].split("def __exit__", 1)[0]
    assert "os.rename(self.stash, self.backup)" in enter
    assert "os.rename(self.backup, self.stash)" not in enter


@pytest.mark.asyncio
async def test_the_failure_cause_is_never_taken_from_child_output(monkeypatch):
    """A build script must not be able to forge the authoritative diagnosis.

    stdout carries worktree-controlled output, so an in-band marker could be
    printed by an npm lifecycle script; the cause comes from the exit code.
    """
    from kiro_crew.apps.builtins.dev_fleet import sync_runner

    _, cmd, _ = await _run_sync(mod, [])
    assert cmd is not None
    # No promotable marker is emitted by the runner at all.
    src = Path(sync_runner.__file__).read_text(encoding="utf-8")
    assert "::cause::" not in src
    # And the worker has no branch that lifts text out of a line.
    import inspect

    body = inspect.getsource(mod._start_run)
    assert "::cause::" not in body
    assert (
        "npm_preflight.explain_exit(rc)" in body
    ), "the cause must be derived from the exit code, gateway-side"


@pytest.mark.asyncio
async def test_runner_refuses_when_a_tree_and_a_backup_both_exist(monkeypatch):
    """Both paths present is AMBIGUOUS, so the runner touches neither.

    A kill during npm ci and a stale backup after a good sync look identical on
    disk, so any pick destroys the good copy in one case.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, _ = await _run_sync(mod, [])
    assert cmd is not None

    # The refusal itself is driven in test_dev_fleet_sync_runner.py
    # (TestReconcileLeftovers). Here: the code is handed in on argv, and the
    # gateway maps it to a sentence naming the next step.
    assert "--exit-tree-ambiguous" in cmd
    passed = cmd[cmd.index("--exit-tree-ambiguous") + 1]
    assert passed == str(npm_preflight.EXIT_TREE_AMBIGUOUS)
    text = npm_preflight.explain_exit(npm_preflight.EXIT_TREE_AMBIGUOUS)
    assert "press Pull + Build again" in text


@pytest.mark.asyncio
async def test_only_the_preflight_step_may_assert_a_diagnosis(monkeypatch):
    """A reserved exit code is trusted from ONE step and remapped from the rest.

    Every step but the preflight runs worktree-controlled code that can exit any
    number, so a forged 41 would claim a registry-credential failure.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, _ = await _run_sync(mod, [])
    assert cmd is not None

    # The gate is driven in test_dev_fleet_sync_runner.py (TestDemoteReserved,
    # TestRunSteps). Here: the gateway passes the reserved set and trusted label
    # IN, so the runner imports nothing from kiro_crew and cannot drift.
    assert "--reserved" in cmd
    reserved = sorted(npm_preflight.RESERVED_EXIT_CODES)
    passed = cmd[cmd.index("--reserved") + 1]
    assert passed == ",".join(str(c) for c in reserved), passed
    assert "--preflight-label" in cmd
    assert cmd[cmd.index("--preflight-label") + 1] == mod._PREFLIGHT_LABEL
    # Every explained code must be guarded. EXIT_FRONTEND_SKIP is guarded too,
    # but it is a success verdict, so it carries no explanation.
    for code in reserved:
        if code == npm_preflight.EXIT_FRONTEND_SKIP:
            assert not npm_preflight.explain_exit(code), "the skip verdict is not a diagnosis"
            continue
        assert npm_preflight.explain_exit(code), code
    # Guarded so a worktree-run step cannot forge it to suppress the build.
    assert npm_preflight.EXIT_FRONTEND_SKIP in npm_preflight.RESERVED_EXIT_CODES
    assert "--exit-frontend-skip" in cmd
    assert cmd[cmd.index("--exit-frontend-skip") + 1] == str(npm_preflight.EXIT_FRONTEND_SKIP)
    assert "--frontend-labels" in cmd
    assert cmd[cmd.index("--frontend-labels") + 1] == "npm ci,npm build + stage"


def test_the_trusted_label_matches_the_step_that_carries_it():
    """The trust check keys on a label; a renamed step would silently drop the
    probe's own diagnosis."""
    import inspect

    src = inspect.getsource(mod._sync_start_locked)
    assert "_PREFLIGHT_LABEL," in src, (
        "the preflight step must be labelled from the constant the runner's "
        "trust check uses, not from a repeated literal"
    )


@pytest.mark.asyncio
async def test_probe_and_merge_consume_one_immutable_commit(monkeypatch):
    """Fetch, probe and merge must share a commit no background fetch can move.

    The status refresher re-fetches ``<remote>/<base branch>`` every
    _NET_REFRESH_S seconds, so resolving that name twice could certify one
    revision and install another.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, steps = await _run_sync(mod, [])

    by_label = {}
    for st in steps:
        by_label.setdefault(st["label"], []).append(st["argv"])

    fetch = next(a for a in by_label["Pull"] if "fetch" in a)
    merge = next(a for a in by_label["Merge"] if "merge" in a)
    probe = by_label[mod._PREFLIGHT_LABEL][0]

    # The fetch pins the tip it brought, forcing, because the ref is ours.
    pinned = mod._sync_base_ref()
    assert f"+refs/heads/{mod.BASE_BRANCH}:{pinned}" in fetch
    # Both consumers name that pinned ref...
    assert merge[-1] == pinned
    assert probe[probe.index("--ref") + 1] == pinned
    # ...the ref is PER PROCESS: _SYNC_LOCK is single-flight only inside one
    # gateway, and a second gateway's fetch must not move this one's pin.
    assert str(os.getpid()) in pinned, pinned
    assert pinned.startswith("refs/kirocrew/sync-base-"), pinned
    # ...and neither still names the mutable one, which is the actual defect.
    mutable = [a for a in merge + probe if a.endswith("/" + mod.BASE_BRANCH)]
    assert not mutable, (
        f"{mutable} is a mutable remote-tracking name; the refresher can move "
        "it between the probe and the merge"
    )
    # The pin must be written before anything reads it, or a ref left by an
    # earlier run could be probed and merged.
    labels = [st["label"] for st in steps]
    assert labels.index("Pull") < labels.index(mod._PREFLIGHT_LABEL)


def test_the_frontend_declares_no_resolution_input_the_probe_cannot_mirror():
    """A tripwire on the probe's three-file mirror, not on the frontend.

    The preflight installs a scratch copy of ``_PROBE_FILES`` and fails closed,
    so ``workspaces`` or ``file:``/``link:`` specifiers (paths absent from the
    scratch dir) would block every Pull + Build. Fix npm_preflight, not this test.
    """
    root = Path(__file__).resolve().parents[1]
    pkg_path = root / "website" / "package.json"
    if not pkg_path.is_file():  # editions ship without the frontend half
        pytest.skip("no frontend half in this checkout")
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))

    assert "workspaces" not in pkg, (
        "website/package.json now declares workspaces, which npm resolves from "
        "sibling directories the preflight's scratch copy does not have. The "
        "probe would refuse every sync. See _PROBE_FILES in npm_preflight."
    )

    local = {}
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (pkg.get(field) or {}).items():
            if isinstance(spec, str) and spec.startswith(("file:", "link:")):
                local[f"{field}.{name}"] = spec
    assert not local, (
        f"local-path dependencies {local} resolve against the checkout, which "
        "the preflight's scratch copy is not. The probe would refuse every "
        "sync. See _PROBE_FILES in npm_preflight."
    )

    # The mirror must also still carry the inputs it claims to: a copy that
    # dropped .npmrc would answer a different question than the install.
    for name in ("package-lock.json", "package.json", ".npmrc"):
        assert name in npm_preflight._PROBE_FILES, name


@pytest.mark.asyncio
async def test_only_the_sync_kind_can_be_stamped_with_a_diagnosis(monkeypatch):
    """The stamp must be gated on the kind whose script enforces the reservation.

    ``_start_run`` also serves ``provision <name>``, which runs an agent-authored
    branch with no remapping, so its exit 41 must not become a registry diagnosis.
    Driven end to end: a source-text check misses a gate variable that the
    ``::step::`` handler rebinds.
    """
    seen = {}

    for kind, emits_steps in (("sync", True), ("provision wt-x", False)):
        lines = [b"::step::0::4::Pull\n"] if emits_steps else []
        lines.append(b"npm error code E401\n")

        class FakeProc:
            pid = 4242
            returncode = None

            def __init__(self):
                self.stdout = self
                self._lines = list(lines)

            async def readline(self):
                return self._lines.pop(0) if self._lines else b""

            async def wait(self):
                self.returncode = npm_preflight.EXIT_AUTH
                return npm_preflight.EXIT_AUTH

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(runtime_mod.asyncio, "create_subprocess_exec", fake_exec)
        rid = await mod._start_run(kind, ["true"], env={})
        for _ in range(80):
            # flake-ok: pre-existing bounded 50 ms poll of the run status, moved verbatim; on expiry the test reads the record as it stands and its assertions fail
            await runtime_mod.asyncio.sleep(0.05)
            async with mod._RUNS_LOCK:
                if mod._RUNS.get(rid, {}).get("status") not in (None, "running"):
                    break
        async with mod._RUNS_LOCK:
            seen[kind] = dict(mod._RUNS[rid])

    assert seen["sync"].get("cause"), (
        "the sync run was not stamped; the gate is reading something other than " "the run kind"
    )
    assert "registry" in seen["sync"]["cause"].lower()
    assert seen["sync"]["exit_code"] == npm_preflight.EXIT_AUTH

    # The provision run gets no cause AND still records its exit code (an
    # unbound gate name would turn it into exit -1).
    assert not seen["provision wt-x"].get("cause"), seen["provision wt-x"]
    assert seen["provision wt-x"]["exit_code"] == npm_preflight.EXIT_AUTH, (
        "the provision run's exit code was rewritten -- the completion path "
        "raised before recording it"
    )


def test_the_stamp_gate_reads_a_name_the_step_handler_cannot_rebind():
    """The gate must not read a variable the output loop assigns to.

    ``label`` is both the parameter and the ``::step::`` handler's target, so the
    gate's variable must be bound once; checked on the parse tree, since a
    substring count of ``run_kind =`` also matches ``run_kind ==``.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod._start_run)))

    # The name the gate actually compares against.
    gates = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Compare)
        and isinstance(n.left, ast.Name)
        and any(isinstance(c, ast.Name) and c.id == "_SYNC_RUN_LABEL" for c in n.comparators)
    ]
    assert len(gates) == 1, f"expected one kind gate, found {len(gates)}"
    guarded = gates[0].left.id

    targets = [
        t.id
        for n in ast.walk(tree)
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.For, ast.AsyncFor))
        for t in ast.walk(n.target if hasattr(n, "target") else n.targets[0])
        if isinstance(t, ast.Name)
    ]
    assert targets.count(guarded) == 1, (
        f"{guarded!r} is assigned {targets.count(guarded)} times in "
        "_start_run; the gate's variable must be bound once, before any output "
        "is read, or a later handler can rebind it out from under the gate"
    )
    assert guarded not in {a.arg for a in tree.body[0].args.args}, (
        f"{guarded!r} is the parameter itself -- use a local captured at entry, "
        "so a handler that rebinds the parameter cannot reach the gate"
    )


def test_the_stamp_gate_and_the_sync_label_are_one_constant():
    """A literal in either place would let the two drift apart silently.

    Drift one way re-opens the forgery; the other way suppresses the diagnosis.
    """
    assert mod._SYNC_RUN_LABEL == "sync"
    src = Path(runtime_mod.__file__).read_text(encoding="utf-8")
    assert (
        '_start_run("sync"' not in src
    ), "the sync is started with a literal label; use _SYNC_RUN_LABEL"


@pytest.mark.asyncio
async def test_prune_clears_dead_sync_refs_and_spares_live_ones(tmp_path, monkeypatch):
    """The PID suffix is only affordable if the refs it strands get collected.

    A gateway restart leaves its pinned ref in the operator's checkout. The prune
    removes those, but must spare a live PID's ref: it may be a second gateway's pin.
    """
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "p",
        "GIT_AUTHOR_EMAIL": "p@e",
        "GIT_COMMITTER_NAME": "p",
        "GIT_COMMITTER_EMAIL": "p@e",
    }

    def git(*a):
        return sp.run(
            ["git", *a],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-q", "-m", "one")
    head = git("rev-parse", "HEAD")

    # Refs for: this process, a gone PID, a live foreign PID (pid 1: always alive,
    # never us, and signalling it raises PermissionError), and a foreign name.
    # Only the live foreign PID's survival depends on the liveness check.
    mine = mod._sync_base_ref()
    dead_pid = 999_999_999  # above any real pid_max
    dead = f"refs/kirocrew/sync-base-{dead_pid}"
    live_other = "refs/kirocrew/sync-base-1"
    foreign = "refs/kirocrew/something-else"
    for ref in (mine, dead, live_other, foreign):
        git("update-ref", ref, head)

    async def fake_git(git_dir, *args, **kw):
        return sp.run(
            ["git", *args],
            cwd=git_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        ).stdout

    monkeypatch.setattr(repository_mod, "_git", fake_git)

    await mod._prune_dead_sync_base_refs(str(repo))

    remaining = set(git("for-each-ref", "--format=%(refname)", "refs/kirocrew/").splitlines())
    assert mine in remaining, (
        "the prune deleted THIS process's live pin; a sync would then fetch into "
        "a ref another gateway could move"
    )
    assert live_other in remaining, (
        "the prune deleted a ref belonging to a process that is STILL ALIVE -- "
        "that may be a second gateway's pin, and removing it reopens exactly the "
        "window the PID suffix exists to close"
    )
    assert dead not in remaining, f"stale ref {dead} survived the prune"
    assert foreign in remaining, "the prune deleted a ref outside its own naming scheme"


@pytest.mark.asyncio
async def test_prune_runs_before_the_fetch_step_is_built(monkeypatch):
    """The prune runs before the steps are built, so it cannot race our fetch."""
    calls: list[str] = []

    async def spy(repo):
        calls.append(repo)

    monkeypatch.setattr(worktree_ops_mod, "_prune_dead_sync_base_refs", spy)
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, steps = await _run_sync(mod, [])

    assert calls, "the sync never pruned stale pinned refs"

    fetch = [st for st in steps if "fetch" in st["argv"]]
    assert fetch, "no fetch step"
    # The pin the fetch writes is this process's, which the prune never removes.
    assert mod._sync_base_ref() in " ".join(fetch[0]["argv"])


@pytest.mark.asyncio
async def test_preflight_runs_a_snapshot_not_the_editable_source(monkeypatch):
    """The probe must execute a private COPY by path, never import the checkout.

    ``-c`` puts the checkout first on ``sys.path`` (``-I`` fixes that), and the
    editable install resolves ``import kiro_crew...`` into the checkout even
    under ``-I``. This step is trusted because its binary is ours, so it runs a
    snapshot by path; npm_preflight imports only stdlib.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    argvs = await _sync_step_argvs(monkeypatch)
    probe = [
        a
        for a in argvs
        if any("npm_preflight" in str(x) for x in a) and str(a[0]) == sys.executable
    ]
    assert probe, f"no preflight step in {argvs}"
    argv = probe[0]

    joined = " ".join(str(x) for x in argv)
    assert "-c" not in argv, argv
    assert "import" not in joined, (
        "the probe still imports the module; an editable install resolves that "
        "into the checkout being synced"
    )
    # A script path, and NOT one inside the checkout.
    script = [str(x) for x in argv if str(x).endswith("npm_preflight.py")]
    assert len(script) == 1, argv
    assert (
        "/src/kiro_crew/" not in script[0]
    ), f"{script[0]} is the editable source itself, not a snapshot"
    # Isolated, with the flags ahead of the script path -- after it they would be
    # argv for the program and the protection would vanish silently.
    assert argv.index("-I") < argv.index(script[0]), argv
    assert argv.index("-X") < argv.index(script[0]), argv


@pytest.mark.asyncio
async def test_the_preflight_snapshot_is_registered_for_cleanup(monkeypatch):
    """The snapshot must not leak one temp directory per Pull + Build.

    Cleanup falls back to rmdir, which needs an empty directory, so the FILE must
    be registered ahead of its directory.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    await _run_sync(mod, [])

    paths = list(_LAST_CLEANUP_PATHS)
    snaps = [p for p in paths if "npm-preflight" in p]
    assert snaps, f"the preflight snapshot is not registered for cleanup: {paths}"
    files = [p for p in snaps if p.endswith(".py")]
    dirs = [p for p in snaps if not p.endswith(".py")]
    assert files and dirs, f"expected both the file and its directory: {snaps}"
    assert paths.index(files[0]) < paths.index(dirs[0]), (
        "the directory is registered before its file, so rmdir will fail on a "
        "non-empty directory and the snapshot will leak"
    )


@pytest.mark.parametrize("failing", ["mkdtemp", "write"])
@pytest.mark.asyncio
async def test_a_full_tmpdir_refuses_the_sync_instead_of_raising(monkeypatch, tmp_path, failing):
    """Staging the snapshot can fail; that must refuse, and leave nothing behind.

    An escaping OSError would be an HTTP 500, and proceeding unprobed is the
    destructive path. In the write case mkdtemp already succeeded and nothing is
    registered for cleanup, so the refusal must remove the directory itself.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    boom = OSError(28, "No space left on device")
    # Direct the REAL mkdtemp under this test's tmp_path, so even a regression
    # that reintroduces the leak cannot litter the host running the suite.
    real_mkdtemp = worktree_ops_mod.tempfile.mkdtemp

    if failing == "mkdtemp":
        monkeypatch.setattr(
            worktree_ops_mod.tempfile,
            "mkdtemp",
            lambda *a, **kw: (_ for _ in ()).throw(boom),
        )
    else:
        monkeypatch.setattr(
            worktree_ops_mod.tempfile,
            "mkdtemp",
            lambda *a, **kw: real_mkdtemp(*a, **{**kw, "dir": str(tmp_path)}),
        )
        real_write = Path.write_bytes

        def fail_write(self, data):
            if self.name == "npm_preflight.py":
                raise boom
            return real_write(self, data)

        monkeypatch.setattr(Path, "write_bytes", fail_write)

    result, cmd, steps = await _run_sync(mod, [])

    assert result.get("ok") is False, result
    assert "preflight" in result["error"].lower(), result
    assert "No space left on device" in result["error"], result
    assert cmd is None, "the sync started a run despite failing to stage"
    leaked = list(tmp_path.glob("kirocrew-npm-preflight-*"))
    assert not leaked, f"the refusal left {leaked} behind; every failed sync would leak one"


@pytest.mark.asyncio
async def test_the_snapshot_is_written_from_bytes_captured_at_import(monkeypatch):
    """The probe's snapshot must be of the code THIS gateway is running.

    Copying the file at sync time would let a source rewritten after gateway
    start run as the one step trusted to assert a failure cause.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    written: dict = {}
    real_write = Path.write_bytes

    def spy(self, data):
        if self.name == "npm_preflight.py":
            written["data"] = data
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", spy)
    # Whatever is on disk now is irrelevant: the module was imported long ago.
    monkeypatch.setattr(runtime_mod, "_PREFLIGHT_SOURCE", b"# captured at import\nmarker = 1\n")
    await _run_sync(mod, [])

    assert "data" in written, "the snapshot was never written"
    assert (
        written["data"] == b"# captured at import\nmarker = 1\n"
    ), "the snapshot was re-read from disk instead of using the captured bytes"


@pytest.mark.asyncio
async def test_sync_refuses_when_the_probe_source_could_not_be_captured(monkeypatch):
    """An unreadable probe source refuses rather than falling back to a re-read.

    A frozen or zipimported install has no readable ``__file__``; a re-read at
    sync time would reopen the window the import-time capture closes.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    monkeypatch.setattr(runtime_mod, "_PREFLIGHT_SOURCE", None)

    result, cmd, steps = await _run_sync(mod, [])

    assert result.get("ok") is False, result
    assert "preflight" in result["error"].lower(), result
    assert cmd is None, "the sync started a run with no trustworthy probe"


@pytest.mark.asyncio
async def test_every_stash_presence_gate_uses_lexists(monkeypatch):
    """Presence gates must not follow symlinks.

    With ``isdir`` a DANGLING node_modules reads as absent and the backup-only
    ``os.rename`` fails ENOTDIR on every sync; a symlinked tree is never stashed.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    from kiro_crew.apps.builtins.dev_fleet import sync_runner

    _, cmd, _ = await _run_sync(mod, [])
    assert cmd is not None

    src = Path(sync_runner.__file__).read_text(encoding="utf-8")
    assert "have_tree = os.path.lexists(stash)" in src
    assert "have_backup = os.path.lexists(backup)" in src
    assert "if self.stash and self.backup and os.path.lexists(self.stash):" in src
    # No presence gate on either path may follow a link.
    for bad in (
        "os.path.isdir(stash)",
        "os.path.isdir(backup)",
        "os.path.exists(stash)",
        "os.path.exists(backup)",
    ):
        assert bad not in src, f"{bad} follows symlinks; use lexists"
