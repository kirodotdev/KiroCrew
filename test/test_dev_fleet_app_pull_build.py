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

# --- Pull+Build: preflight, node_modules transaction, operator repair seam ---
#
# `npm ci` deletes node_modules before installing, so a registry that refuses one
# package can turn a sync into damage: the tree was emptied, the run aborted
# mid-reify, and the checkout was left with new source, a new lockfile and no
# frontend dependencies. These pin the three properties that make that failure a
# no-op instead.


@pytest.mark.asyncio
async def test_sync_preflights_between_fetch_and_merge(monkeypatch):
    """The probe must sit AFTER fetch and BEFORE merge.

    That position is the whole mechanism, not a detail: the incoming lockfile is
    only knowable once fetch has landed, and fetch moves nothing but remote refs
    — so it is the one moment where a refusal costs nothing. After the merge the
    refusal would already be too late; before the fetch there is nothing to read.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, steps = await _run_sync(mod, [])
    labels = [s["label"] for s in steps]

    assert mod._PREFLIGHT_LABEL in labels, labels
    # The probe sits between the fetch and the merge, which is the whole point:
    # the lockfile is knowable once fetch lands, and refusing before merge costs
    # nothing. The merge is labelled distinctly from the fetch so the rendered
    # stepper does not read Pull -> Preflight -> Pull, like a restarted run.
    assert labels[0] == "Pull", labels
    assert labels.index(mod._PREFLIGHT_LABEL) == 1, labels
    assert labels[2] == "Merge", labels
    assert labels.count("Pull") == 1, labels
    assert labels.index("pip install") > labels.index(mod._PREFLIGHT_LABEL), labels


@pytest.mark.asyncio
async def test_sync_preflight_probes_the_incoming_ref_not_the_working_tree(monkeypatch):
    """It must read the lockfile from the FETCHED ref.

    Reading the working tree would answer the question about the revision we
    already have, which is never the one that is about to be installed — and it
    could only be done after the merge, i.e. after the point where refusing is
    still free.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    argvs = await _sync_step_argvs(monkeypatch)
    probe = [a for a in argvs if any("npm_preflight" in str(x) for x in a)]
    assert probe, f"no preflight step in {argvs}"
    argv = probe[0]
    assert argv[0] == sys.executable, argv
    assert "--ref" in argv
    ref = argv[argv.index("--ref") + 1]
    # The ref the fetch step pinned, not a path and not a mutable
    # remote-tracking name: reading the working tree would answer the question
    # about the revision we already have, and a name the refresher can move
    # would answer it about a revision the merge may not install.
    assert ref == mod._sync_base_ref(), ref
    assert not ref.endswith(f"/{mod.BASE_BRANCH}"), ref
    assert not ref.startswith("/") and not ref.startswith("."), ref


@pytest.mark.asyncio
async def test_sync_skips_the_preflight_on_an_edition_checkout(monkeypatch):
    """No frontend half means nothing to preflight.

    The edition path deliberately runs no npm at all, so a probe there would be
    a network round trip that can only produce a false refusal.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: True)
    _, cmd, steps = await _run_sync(mod, [])
    assert mod._PREFLIGHT_LABEL not in [s["label"] for s in steps]


@pytest.mark.asyncio
async def test_npm_ci_step_carries_a_node_modules_stash(monkeypatch):
    """The transaction is attached to the npm ci step, and ONLY to it.

    It cannot be a later "restore" step: the runner is fail-fast, so anything
    after a failed step never runs — which is exactly the case that needs the
    restore.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, steps = await _run_sync(mod, [])
    stashed = {s["label"]: s["stash"] for s in steps if s.get("stash")}
    assert list(stashed) == ["npm ci"], stashed
    assert stashed["npm ci"] == str(Path(_SYNC_REPO) / "website" / "node_modules")
    # The transaction's BEHAVIOUR — restore on failure only, confirmed
    # deletions, symlink unlinking, lexists gates — is proven by EXECUTION in
    # test_dev_fleet_sync_runner.py against real directory trees; the inline
    # source-text assertions this test once carried moved there with it. What
    # stays here is the composition contract: the stash rides the npm ci step.


@pytest.mark.asyncio
async def test_sync_never_runs_an_operator_supplied_command(monkeypatch):
    """No configurable command executes on the sync path. This is a RATCHET.

    An operator-declared "repair the registry credential" hook was written, then
    removed: its whole purpose is to run a program that touches the operator's
    credential material, and the operator declares the command while an agent
    can rewrite the FILE it names -- or a script among its arguments. Withholding
    git's credential helpers did not close it either, because HOME is itself the
    channel those credentials arrive through. No validation of argv[0] can make
    "the operator chose this command" mean "this is the code that will run", so
    the seam does not belong on a path that runs unattended.

    Restoring it needs its own change with its own threat model, not a revert.
    """
    monkeypatch.setenv("KIROCREW_DEVFLEET_NPM_AUTH_REPAIR", "/bin/sh -c true")
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, steps = await _run_sync(mod, [])

    assert not any("repair" in s["label"] for s in steps), steps
    assert not hasattr(mod, "_npm_auth_repair_argv")
    cmd_flat = " ".join(map(str, cmd))
    for token in ("KIROCREW_DEVFLEET_NPM_AUTH_REPAIR", "npm_auth_repair"):
        assert token not in cmd_flat, cmd
    # The declared command must appear NOWHERE in the composed steps. Asserted
    # this way rather than against a list of expected argv[0]s: the toolchain
    # binaries are resolved from the host (npm is /usr/bin/npm on one platform
    # and /opt/homebrew/bin/npm on another), so a path allowlist tests the host
    # rather than the property.
    assert "/bin/sh" not in cmd_flat
    for s in steps:
        assert "/bin/sh" not in " ".join(map(str, s["argv"])), s["argv"]
        assert not any("repair" in str(v) for v in s.get("env", {}).values()), s


@pytest.mark.asyncio
async def test_a_stashed_tree_is_recovered_before_any_step_runs(monkeypatch):
    """Recovery must not sit behind the steps that precede the transaction.

    A run killed just after the move-aside leaves the tree absent and its backup
    unclaimed. With adoption on the `npm ci` step, the next run's recovery was
    gated on every earlier step succeeding -- so a preflight that still failed
    left the tree missing with an intact copy sitting right beside it. Both
    halves of leftover-state reconciliation therefore happen before the loop.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    from kiro_crew.apps.builtins.dev_fleet import sync_runner

    _, cmd, steps = await _run_sync(mod, [])
    assert cmd is not None
    # The reconcile/adopt decision and the move-aside now live in the runner
    # module. That both halves happen -- and the backup-only recovery lands
    # BEFORE any step runs, and the pre-run section only moves the tree aside --
    # is driven against real trees in test_dev_fleet_sync_runner.py
    # (TestReconcileLeftovers, TestNodeModulesTransaction). Here we pin the
    # structural ordering in main(): reconcile_leftovers is called before
    # run_steps.
    src = Path(sync_runner.__file__).read_text(encoding="utf-8")
    main_body = src.split("def main(", 1)[1]
    assert main_body.index("reconcile_leftovers(") < main_body.index(
        "run_steps("
    ), "the stashed tree must be reclaimed before any step runs"
    # The move-aside (enter) and the restore (exit) are separate phases of the
    # transaction, not a redundant second adoption in the pre-run section.
    enter = src.split("def __enter__", 1)[1].split("def __exit__", 1)[0]
    assert "os.rename(self.stash, self.backup)" in enter
    assert "os.rename(self.backup, self.stash)" not in enter


@pytest.mark.asyncio
async def test_the_failure_cause_is_never_taken_from_child_output(monkeypatch):
    """A build script must not be able to forge the authoritative diagnosis.

    The run's stdout carries worktree-controlled output, so any in-band marker
    the gateway promoted could be printed by an npm lifecycle script that then
    fails -- and the dashboard would present the forgery as the cause, remedy
    included. Redaction does not help: it strips credentials, not instructions.
    So the diagnosis is derived from the EXIT CODE, which a step's own child
    cannot choose, and nothing is parsed out of the stream.
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

    Killed during npm ci leaves a partial tree plus the good backup; a backup
    outliving a successful sync leaves the good tree plus a stale one. Nothing on
    disk distinguishes them, so either rule destroys the good copy in one case.
    Stopping is the only branch that cannot lose data, and it exits with a code
    the gateway maps to a sentence naming the next step.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, _ = await _run_sync(mod, [])
    assert cmd is not None

    # The both-exist refusal (exit the ambiguous-tree code, touch neither, name
    # both paths) is driven against real trees in test_dev_fleet_sync_runner.py
    # (TestReconcileLeftovers), and that it lands as reconciliation before any
    # step is pinned there and by the main() ordering test. The code itself has
    # ONE spelling now (npm_preflight.EXIT_TREE_AMBIGUOUS, passed to the runner
    # via --exit-tree-ambiguous); here we pin the pieces that stay
    # gateway-side: the code is handed in on argv, and the gateway maps it to a
    # sentence naming the next step.
    assert "--exit-tree-ambiguous" in cmd
    passed = cmd[cmd.index("--exit-tree-ambiguous") + 1]
    assert passed == str(npm_preflight.EXIT_TREE_AMBIGUOUS)
    # The mapped sentence names what to do, not merely what happened.
    text = npm_preflight.explain_exit(npm_preflight.EXIT_TREE_AMBIGUOUS)
    assert "press Pull + Build again" in text


@pytest.mark.asyncio
async def test_only_the_preflight_step_may_assert_a_diagnosis(monkeypatch):
    """A reserved exit code is trusted from ONE step and remapped from the rest.

    Moving the diagnosis off stdout onto exit codes did not by itself make it
    unforgeable: every step except the preflight runs worktree-controlled code
    (an npm lifecycle script, a vite config) and can exit any number it likes.
    A forged 41 would have the dashboard assert a registry-credential failure --
    remedy included -- for what was actually a build error. So the runner trusts
    a reserved code only from the step whose binary is ours, and keeps the true
    code in the log rather than believing it.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, _ = await _run_sync(mod, [])
    assert cmd is not None

    # The gate itself (a reserved code is trusted from the preflight label and
    # demoted from every other step) is driven by execution in
    # test_dev_fleet_sync_runner.py (TestDemoteReserved, TestRunSteps). Here we
    # pin that the gateway passes the reserved set and the trusted label IN, so
    # the runner needs to import nothing from kiro_crew and the two cannot
    # drift from the modules that own them.
    assert "--reserved" in cmd
    reserved = sorted(npm_preflight.RESERVED_EXIT_CODES)
    passed = cmd[cmd.index("--reserved") + 1]
    assert passed == ",".join(str(c) for c in reserved), passed
    assert "--preflight-label" in cmd
    assert cmd[cmd.index("--preflight-label") + 1] == mod._PREFLIGHT_LABEL
    # Every code the gateway EXPLAINS must be in the guarded set, or a diagnosis
    # it explains could arrive forged. The converse does not hold:
    # EXIT_FRONTEND_SKIP is guarded so an untrusted step cannot forge it, but it
    # is a SUCCESS verdict, not a diagnosis, so it carries no explanation.
    for code in reserved:
        if code == npm_preflight.EXIT_FRONTEND_SKIP:
            assert not npm_preflight.explain_exit(code), "the skip verdict is not a diagnosis"
            continue
        assert npm_preflight.explain_exit(code), code
    # The frontend-skip verdict must be guarded (reserved) so a worktree-run step
    # cannot forge it to suppress the build, and the gateway must tell the runner
    # its value and which labels it suppresses.
    assert npm_preflight.EXIT_FRONTEND_SKIP in npm_preflight.RESERVED_EXIT_CODES
    assert "--exit-frontend-skip" in cmd
    assert cmd[cmd.index("--exit-frontend-skip") + 1] == str(npm_preflight.EXIT_FRONTEND_SKIP)
    assert "--frontend-labels" in cmd
    assert cmd[cmd.index("--frontend-labels") + 1] == "npm ci,npm build + stage"


def test_the_trusted_label_matches_the_step_that_carries_it():
    """The trust check keys on a label, so the label must be the real one.

    If the step were renamed without updating the constant, every reserved code
    would be remapped -- the probe's own diagnosis would silently stop reaching
    the dashboard, and nothing would fail.
    """
    import inspect

    src = inspect.getsource(mod._sync_start_locked)
    assert "_PREFLIGHT_LABEL," in src, (
        "the preflight step must be labelled from the constant the runner's "
        "trust check uses, not from a repeated literal"
    )


@pytest.mark.asyncio
async def test_probe_and_merge_consume_one_immutable_commit(monkeypatch):
    """Fetch, probe and merge must share a commit no background fetch can move.

    ``<remote>/<base branch>`` is a mutable name and the status refresher
    re-fetches it every _NET_REFRESH_S seconds in this same process. With a real
    install sitting between the probe and the merge, resolving that name twice
    lets them land on different commits -- the probe would certify a revision
    that is not the one installed, which is worse than not probing at all,
    because the promise is what makes the merge look safe.
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
    # ...the ref is PER PROCESS, because _SYNC_LOCK only makes syncs
    # single-flight inside one gateway: two gateways on one checkout would
    # otherwise share this name and the second one's fetch would move it
    # between the first one's probe and merge, reopening the window.
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

    The preflight installs a scratch copy of ``_PROBE_FILES`` and refuses the
    sync when that install fails. The gate is fail-closed and has no bypass, so
    a resolution input the mirror does NOT carry makes the scratch install fail
    while the real one would have succeeded -- which hard-blocks every
    Pull + Build until somebody edits npm_preflight.

    ``workspaces`` and ``file:``/``link:`` specifiers are that class: both make
    npm read paths that exist in the checkout and not in the scratch directory.
    Neither is present today, so this pins the assumption rather than reporting a
    defect: whoever introduces one gets this failure at that moment, instead of
    an unexplained refusal of every update afterwards. Fixing it then means
    teaching the probe to skip (restoring today's unguarded behaviour for that
    case) or to copy what the new input needs -- not deleting this test.
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

    ``_start_run`` is kind-agnostic and also serves ``provision <name>``, which
    executes an agent-authored branch with no reserved-code remapping. Deriving
    the cause for every kind would hand a provision run's exit 41 back as "the
    package registry rejected our credentials" plus a remedy -- the same forged
    authoritative diagnosis the runner-side demotion exists to refuse, arriving
    by the other door.

    Driven end to end rather than read out of the source: an earlier version of
    this test asserted on ``inspect.getsource`` and passed while the gate was
    comparing a variable that the ``::step::`` handler had already rebound, so
    the sync path silently stamped nothing and the provision path raised
    NameError. Only running it catches that.
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

    # The sync run is the one kind allowed to assert a cause -- and it must
    # actually get one, which the shadowed comparison silently prevented.
    assert seen["sync"].get("cause"), (
        "the sync run was not stamped; the gate is reading something other than " "the run kind"
    )
    assert "registry" in seen["sync"]["cause"].lower()
    assert seen["sync"]["exit_code"] == npm_preflight.EXIT_AUTH

    # The provision run must be stamped with nothing AND must still complete
    # normally: an unbound name here turned a finished run into exit -1.
    assert not seen["provision wt-x"].get("cause"), seen["provision wt-x"]
    assert seen["provision wt-x"]["exit_code"] == npm_preflight.EXIT_AUTH, (
        "the provision run's exit code was rewritten -- the completion path "
        "raised before recording it"
    )


def test_the_stamp_gate_reads_a_name_the_step_handler_cannot_rebind():
    """The gate must not read a variable the output loop assigns to.

    This is the defect that shipped once: ``label`` is the function parameter
    AND the ``::step::`` handler's target, so at completion it held the last
    step's label or was unbound. Whatever the gate compares has to be assigned
    exactly once, which is checked here on the parse tree rather than by counting
    substrings -- the first version of this test counted ``run_kind =`` and
    matched ``run_kind ==`` too.
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

    Drift in one direction re-opens the forgery; in the other it suppresses the
    diagnosis this PR exists to deliver, and neither shows up as a failure.
    """
    assert mod._SYNC_RUN_LABEL == "sync"
    src = Path(runtime_mod.__file__).read_text(encoding="utf-8")
    assert (
        '_start_run("sync"' not in src
    ), "the sync is started with a literal label; use _SYNC_RUN_LABEL"


@pytest.mark.asyncio
async def test_prune_clears_dead_sync_refs_and_spares_live_ones(tmp_path, monkeypatch):
    """The PID suffix is only affordable if the refs it strands get collected.

    Nothing deletes the pinned ref on the way out, so an ordinary gateway
    restart leaves one behind in the OPERATOR's checkout -- unbounded except by
    pid_max and visible in ``git for-each-ref`` forever. The prune removes those,
    and must LEAVE ALONE any ref whose PID is still alive: that one may be a
    second gateway's live pin, and deleting it would reopen the very window the
    suffix closes.
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
    (repo / "f.txt").write_text("one\n")
    git("add", "f.txt")
    git("commit", "-q", "-m", "one")
    head = git("rev-parse", "HEAD")

    # A ref for THIS process (skipped by name), one for a PID that is certainly
    # gone, one for a DIFFERENT but LIVE process -- pid 1 always exists and is
    # never us, and signalling it raises PermissionError rather than succeeding,
    # so it exercises the alive-but-not-ours branch too -- and one that is not
    # ours to reason about. The live foreign PID is the case that matters: it
    # stands in for a second gateway's pin, and it is the only ref whose survival
    # depends on the liveness check rather than on the name check.
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
    """Ordering: the prune must not be able to race the fetch that writes our pin.

    It only ever deletes refs for PIDs that are gone, so it cannot touch our own
    -- but running it before the steps are built keeps that guarantee structural
    rather than incidental.
    """
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

    Two distinct holes close here. ``-c`` puts the cwd -- the checkout -- first on
    ``sys.path``, so an untracked ``kiro_crew/`` package at its root would win;
    ``-I`` fixes that. But the install is EDITABLE, so ``import kiro_crew...``
    resolves into the checkout's own ``src/`` tree even under ``-I``, and this
    step is trusted to assert a failure cause precisely BECAUSE its binary is
    ours. Importing the tree being synced put the boundary's own key under the
    mat. Running a snapshot by path consults neither.

    Verified end to end in $KIROCREW_SCRATCH/shadow_probe.py: a planted shadow
    wins without -I, the import still lands in the editable tree WITH -I, and a
    by-path snapshot runs standalone because npm_preflight imports only stdlib.
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

    # No import of the package at all -- that is the point.
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

    The run's cleanup unlinks each registered path and falls back to rmdir, which
    only succeeds on an empty directory -- so the FILE has to be registered ahead
    of its directory. The dependency-only path already leaks its own snapshot dir
    by registering neither; this asserts we do not repeat that.
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

    ``mkdtemp`` and the snapshot write both raise OSError on a full or unwritable
    TMPDIR. The sync answers a UI action, so an escaping OSError would surface as
    an unhandled HTTP 500 with no remedy. Refusing is also the SAFE outcome: with
    no probe there is nothing to trust, and proceeding unprobed is precisely the
    destructive path this change exists to prevent.

    The write case matters twice over: mkdtemp has already SUCCEEDED by then, and
    the refusal returns before anything is registered for the run's cleanup, so a
    directory would survive every failed sync -- in the product, not merely in
    this test. Both sites are driven for real; an assertion on the source would
    not notice a second unguarded call appearing beside the first.
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
    # A refusal means no run was started at all, so no steps ran.
    assert cmd is None, "the sync started a run despite failing to stage"
    # And the partial snapshot is gone: nothing registered it for cleanup, so the
    # refusal path has to remove it itself.
    leaked = list(tmp_path.glob("kirocrew-npm-preflight-*"))
    assert not leaked, f"the refusal left {leaked} behind; every failed sync would leak one"


@pytest.mark.asyncio
async def test_the_snapshot_is_written_from_bytes_captured_at_import(monkeypatch):
    """The probe's snapshot must be of the code THIS gateway is running.

    Copying the module file at sync time left a window from gateway start until
    the button press in which the source could be rewritten -- and the copy is
    then executed as the one step trusted to assert a failure cause. Capturing at
    import closes it: the bytes are the ones the running process imported.

    Driven by rewriting the file on disk AFTER import and asserting the snapshot
    does not contain the change.
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

    A frozen or zipimported install has no readable ``__file__``. Falling back to
    copying the file at sync time would reintroduce exactly the window the
    import-time capture removes, so the sync refuses instead -- the same safe
    direction the full-TMPDIR path takes.
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

    ``isdir`` follows a link, so a DANGLING node_modules read as absent: the
    reconciliation then took the backup-only branch and called
    ``os.rename(<dir>, <dangling link>)``, which fails ENOTDIR and crashes the
    runner on every sync with the tree never recovered. The move-aside gate has
    the mirror bug -- with ``isdir`` a symlinked tree is never stashed, so the
    step runs with no backup at all, on exactly the layouts that most need one.

    Verified end to end in $KIROCREW_SCRATCH/runner_probe.py (scenarios 8 and 9);
    this pins the shape so the gates cannot silently revert.
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
