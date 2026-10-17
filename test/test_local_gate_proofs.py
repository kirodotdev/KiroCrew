"""Proof reuse in the change-scoped local gate (``scripts/local-gate.py``).

A gate command that already passed on this exact tree is not run twice. The rules that
make that safe are pinned here, one mutation per rule:

* a proof is keyed by tree, argv, cwd and the i18n base ref: change any one and the
  command runs;
* only a pass is recorded, and only when the tree did not move while the command ran;
* a proof that is unreadable, mismatched, over the age bound or from the future is no
  proof;
* a proof write is atomic, and a write that fails never fails the gate;
* ``--no-reuse`` always runs, and a failing run drops the pass it contradicts;
* the tree id is git's own tree for HEAD plus every edit, built without touching the
  checkout's index.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "local-gate.py"
_CMD = ["/venv/bin/python", "-m", "pytest", "-q", "--", "test/test_a.py"]
_LABEL = "backend (related)"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("local_gate_under_proof_test", _SCRIPT)
    assert spec and spec.loader, "could not build an import spec for the gate"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Proofs: what counts as a proof
# ---------------------------------------------------------------------------


_ENV = {"I18N_BASE_REF": "base-a", "PYTEST_XDIST_AUTO_NUM_WORKERS": "2", "PATH": "/bin"}
_TOOLS = {"python": "3.12.1", "packages": "p1"}


def _store(gate, root, *, tree="tree-a", env=None, now=1000.0):
    return gate.Proofs(root, tree, dict(_ENV if env is None else env), clock=lambda: now)


@pytest.fixture
def pinned(gate, monkeypatch):
    """record() re-reads the tree and every lookup names the tools; pin both to fixed answers."""
    monkeypatch.setattr(gate, "tree_id", lambda: "tree-a")
    monkeypatch.setattr(gate, "tool_versions", lambda cmd, cwd, env: dict(_TOOLS))


def test_a_pass_is_reusable_for_the_same_identity(gate, pinned, tmp_path) -> None:
    _store(gate, tmp_path).record(_CMD, _REPO_ROOT)
    assert _store(gate, tmp_path, now=1100.0).age(_CMD, _REPO_ROOT) == 100.0


@pytest.mark.parametrize(
    "changed",
    [
        {"tree": "tree-b"},
        {"cmd": [*_CMD, "test/test_b.py"]},
        {"cmd": ["/other/python", *_CMD[1:]]},
        {"cwd": _REPO_ROOT / "website"},
    ],
    ids=["tree", "targets", "interpreter", "cwd"],
)
def test_changing_any_part_of_the_identity_runs_the_command(
    gate, pinned, tmp_path, changed
) -> None:
    _store(gate, tmp_path).record(_CMD, _REPO_ROOT)
    other = _store(gate, tmp_path, tree=changed.get("tree", "tree-a"))
    assert other.age(changed.get("cmd", _CMD), changed.get("cwd", _REPO_ROOT)) is None


@pytest.mark.parametrize(
    "other",
    [
        {"cmd": [*_CMD, "test/test_b.py"], "cwd": _REPO_ROOT},
        {"cmd": _CMD, "cwd": _REPO_ROOT / "website"},
    ],
    ids=["command", "cwd"],
)
def test_a_proof_filed_under_another_key_names_what_it_was_for(
    gate, pinned, tmp_path, other
) -> None:
    store = _store(gate, tmp_path)
    store.record(_CMD, _REPO_ROOT)
    shutil.copyfile(store._path(_CMD, _REPO_ROOT), store._path(other["cmd"], other["cwd"]))
    assert store.age(other["cmd"], other["cwd"]) is None


def test_a_proof_is_reusable_up_to_the_age_bound_and_not_past_it(gate, pinned, tmp_path) -> None:
    _store(gate, tmp_path).record(_CMD, _REPO_ROOT)
    bound = gate._PROOF_MAX_AGE
    assert _store(gate, tmp_path, now=1000.0).age(_CMD, _REPO_ROOT) == 0.0
    assert _store(gate, tmp_path, now=1000.0 + bound).age(_CMD, _REPO_ROOT) == bound
    assert _store(gate, tmp_path, now=1000.0 + bound + 0.5).age(_CMD, _REPO_ROOT) is None


def test_a_proof_dated_in_the_future_is_no_proof(gate, pinned, tmp_path) -> None:
    _store(gate, tmp_path).record(_CMD, _REPO_ROOT)
    assert _store(gate, tmp_path, now=999.0).age(_CMD, _REPO_ROOT) is None


_PROOF_TEMPLATES = [
    "",
    "{",
    "[]",
    "null",
    '"proof"',
    '{"at": 1000.0}',
    '{"identity": %(identity)s}',
    '{"identity": 7, "at": 1000.0}',
    '{"identity": %(identity)s, "at": "1000"}',
    '{"identity": %(identity)s, "at": null}',
    '{"identity": %(identity)s, "at": [1000]}',
    '{"identity": %(identity)s, "at": NaN}',
]


def _write_proof(store, template: str) -> None:
    path = store._path(_CMD, _REPO_ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = json.dumps(store._identity(_CMD, _REPO_ROOT))
    path.write_text(template % {"identity": identity}, encoding="utf-8")


def test_a_hand_written_proof_in_the_recorded_layout_is_accepted(gate, pinned, tmp_path) -> None:
    """Negative control for the malformed cases: the layout they break is the real one."""
    store = _store(gate, tmp_path)
    _write_proof(store, '{"identity": %(identity)s, "at": 1000.0}')
    assert store.age(_CMD, _REPO_ROOT) == 0.0


@pytest.mark.parametrize("template", _PROOF_TEMPLATES)
def test_a_malformed_proof_is_no_proof(gate, pinned, tmp_path, template: str) -> None:
    store = _store(gate, tmp_path)
    _write_proof(store, template)
    assert store.age(_CMD, _REPO_ROOT) is None


def test_a_write_cut_short_leaves_no_proof(gate, pinned, tmp_path) -> None:
    store = _store(gate, tmp_path)
    path = store._path(_CMD, _REPO_ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".4242.tmp").write_text('{"identity": ', encoding="utf-8")
    assert store.age(_CMD, _REPO_ROOT) is None


def test_record_keeps_nothing_when_the_tree_moved_while_the_command_ran(
    gate, pinned, monkeypatch, tmp_path
) -> None:
    store = _store(gate, tmp_path)
    monkeypatch.setattr(gate, "tree_id", lambda: "tree-b")
    store.record(_CMD, _REPO_ROOT)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["torn-write", "refused-rename"])
def test_a_failed_write_keeps_the_earlier_proof_and_leaves_no_temp_file(
    gate, pinned, monkeypatch, tmp_path, failure: str
) -> None:
    store = _store(gate, tmp_path)
    store.record(_CMD, _REPO_ROOT)
    proof = store._path(_CMD, _REPO_ROOT)
    before = proof.read_bytes()
    if failure == "torn-write":
        real_write = Path.write_text

        def torn(self, data, *args, **kwargs):
            real_write(self, data[: len(data) // 2], *args, **kwargs)
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", torn)
    else:

        def refuse(src, dst):
            raise OSError("read-only")

        monkeypatch.setattr(gate.os, "replace", refuse)
    _store(gate, tmp_path, now=2000.0).record(_CMD, _REPO_ROOT)
    assert proof.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [proof.name]


def test_forget_drops_the_pass_and_tolerates_a_missing_one(gate, pinned, tmp_path) -> None:
    store = _store(gate, tmp_path)
    store.record(_CMD, _REPO_ROOT)
    store.forget(_CMD, _REPO_ROOT)
    assert store.age(_CMD, _REPO_ROOT) is None
    store.forget(_CMD, _REPO_ROOT)


# ---------------------------------------------------------------------------
# main(): the run loop
# ---------------------------------------------------------------------------


@pytest.fixture
def rig(gate, monkeypatch, tmp_path):
    """gate.main() over one fake command, with the tree, environment and tools under test control."""
    state = types.SimpleNamespace(
        tree="tree-a", env=dict(_ENV), tools=dict(_TOOLS), rc=0, runs=0, during_run=None, order=[]
    )
    git_dir = tmp_path / "gitdir"
    state.proofs_dir = git_dir / "local-gate-proofs"
    plan = gate.Plan("test plan")
    plan.add(_LABEL, list(_CMD), _REPO_ROOT)

    def fake_run(cmd, cwd=None, env=None, **_kwargs):
        state.runs += 1
        state.order.append("run")
        if state.during_run is not None:
            state.during_run()
        return subprocess.CompletedProcess(cmd, state.rc)

    def fake_tools(cmd, cwd, env):
        state.order.append("tools")
        return state.tools

    monkeypatch.setattr(gate, "build_plan", lambda _args: plan)
    monkeypatch.setattr(gate, "gate_env", lambda _base: dict(state.env))
    monkeypatch.setattr(gate, "tree_id", lambda: state.tree)
    monkeypatch.setattr(gate, "tool_versions", fake_tools)
    monkeypatch.setattr(gate, "_git", lambda *args, **kwargs: str(git_dir))
    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    return state


def test_proofs_are_filed_under_the_git_dir_of_the_checkout(gate, rig) -> None:
    store = gate.open_proofs({"I18N_BASE_REF": "base-x"})
    assert (store.root, store.tree, store.env) == (
        rig.proofs_dir,
        "tree-a",
        {"I18N_BASE_REF": "base-x"},
    )


def test_the_second_unchanged_run_reports_proof_reuse_and_does_not_run(gate, rig, capsys) -> None:
    assert gate.main([]) == 0
    assert rig.runs == 1
    capsys.readouterr()
    assert gate.main([]) == 0
    assert rig.runs == 1
    err = capsys.readouterr().err
    assert f"[{_LABEL}] reused, not run" in err
    assert "--no-reuse" in err
    assert "running [" not in err


@pytest.mark.parametrize("field", ["tree", "env", "tools"])
def test_a_changed_tree_environment_or_tool_runs_the_command_again(gate, rig, field) -> None:
    assert gate.main([]) == 0
    setattr(
        rig,
        field,
        {
            "tree": "tree-b",
            "env": {**_ENV, "I18N_BASE_REF": "base-b"},
            "tools": {**_TOOLS, "python": "3.13.0"},
        }[field],
    )
    assert gate.main([]) == 0
    assert rig.runs == 2
    assert gate.main([]) == 0
    assert rig.runs == 2


def test_a_failing_run_is_not_recorded(gate, rig) -> None:
    rig.rc = 1
    assert gate.main([]) == 1
    assert not rig.proofs_dir.exists() or list(rig.proofs_dir.iterdir()) == []
    rig.rc = 0
    assert gate.main([]) == 0
    assert rig.runs == 2
    assert gate.main([]) == 0
    assert rig.runs == 2


def test_a_failure_under_no_reuse_drops_the_pass_it_contradicts(gate, rig) -> None:
    assert gate.main([]) == 0
    rig.rc = 1
    assert gate.main(["--no-reuse"]) == 1
    rig.rc = 0
    assert gate.main([]) == 0
    assert rig.runs == 3


def test_no_reuse_runs_every_command_and_still_records(gate, rig) -> None:
    assert gate.main(["--no-reuse"]) == 0
    assert gate.main(["--no-reuse"]) == 0
    assert rig.runs == 2
    assert gate.main([]) == 0
    assert rig.runs == 2


def test_an_unidentifiable_tree_reuses_nothing_and_writes_nothing(gate, rig, monkeypatch) -> None:
    monkeypatch.setattr(gate, "tree_id", lambda: None)
    assert gate.main([]) == 0
    assert gate.main([]) == 0
    assert rig.runs == 2
    assert not rig.proofs_dir.exists()


def test_a_tree_edited_while_the_command_ran_is_not_recorded(gate, rig) -> None:
    def edit_tree() -> None:
        rig.tree = "tree-b"

    rig.during_run = edit_tree
    assert gate.main([]) == 0
    rig.during_run = None
    rig.tree = "tree-a"
    assert gate.main([]) == 0
    assert rig.runs == 2


def test_a_proof_older_than_the_bound_runs_again(gate, rig) -> None:
    gate.Proofs(rig.proofs_dir, "tree-a", dict(rig.env), clock=lambda: 0.0).record(_CMD, _REPO_ROOT)
    assert gate.main([]) == 0
    assert rig.runs == 1


def test_a_proof_that_cannot_be_written_does_not_fail_the_gate(gate, rig, monkeypatch) -> None:
    def refuse(src, dst):
        raise OSError("read-only")

    monkeypatch.setattr(gate.os, "replace", refuse)
    assert gate.main([]) == 0
    assert gate.main([]) == 0
    assert rig.runs == 2


# ---------------------------------------------------------------------------
# tree_id(): against real git
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=gate", "-c", "user.email=gate@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


@pytest.fixture
def repo(gate, monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    monkeypatch.setattr(gate, "_REPO_ROOT", root)
    return root


def test_tree_id_is_stable_and_leaves_the_checkout_alone(gate, repo) -> None:
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    (repo / "b.txt").write_text("new\n", encoding="utf-8")
    status_before = _git(repo, "status", "--porcelain")
    index_before = (repo / ".git" / "index").read_bytes()
    assert status_before.splitlines() == [" M a.txt", "?? b.txt"]
    first, second = gate.tree_id(), gate.tree_id()
    assert first and first == second
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert _git(repo, "status", "--porcelain") == status_before


@pytest.mark.parametrize("edit", ["unstaged", "staged", "untracked", "deleted"])
def test_tree_id_moves_with_every_kind_of_edit(gate, repo, edit: str) -> None:
    before = gate.tree_id()
    if edit in ("unstaged", "staged"):
        (repo / "a.txt").write_text("two\n", encoding="utf-8")
        if edit == "staged":
            _git(repo, "add", "a.txt")
    elif edit == "untracked":
        (repo / "b.txt").write_text("new\n", encoding="utf-8")
    else:
        (repo / "a.txt").unlink()
    assert gate.tree_id() not in (None, before)


def test_tree_id_names_what_git_would_commit(gate, repo) -> None:
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    (repo / "b.txt").write_text("new\n", encoding="utf-8")
    unstaged = gate.tree_id()
    _git(repo, "add", "-A")
    assert gate.tree_id() == unstaged == _git(repo, "write-tree").strip()


def test_tree_id_ignores_files_gitignore_hides(gate, repo) -> None:
    before = gate.tree_id()
    (repo / "ignored.txt").write_text("scratch\n", encoding="utf-8")
    assert gate.tree_id() == before


def test_tree_id_in_a_linked_worktree_names_the_same_content(
    gate, repo, monkeypatch, tmp_path
) -> None:
    expected = gate.tree_id()
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "--detach", str(linked))
    monkeypatch.setattr(gate, "_REPO_ROOT", linked)
    assert gate.tree_id() == expected
    assert gate.open_proofs({}).root.parent.name == "linked"


def test_tree_id_is_none_outside_a_repository(gate, monkeypatch, tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(gate, "_REPO_ROOT", plain)
    assert gate.tree_id() is None


# ---------------------------------------------------------------------------
# End to end: real git, a real child process, the real proof directory
# ---------------------------------------------------------------------------


def test_a_second_run_over_an_unchanged_repository_starts_the_process_once(
    gate, repo, monkeypatch, tmp_path, capsys
) -> None:
    """Uses only names the gate had before proofs, so on a gate without them it fails on
    the process having started twice, not on a missing attribute."""
    marker = tmp_path / "ran.txt"
    counter = tmp_path / "count_run.py"
    counter.write_text(
        "import sys\nwith open(sys.argv[1], 'a') as handle:\n    handle.write('x')\n",
        encoding="utf-8",
    )
    plan = gate.Plan("end to end")
    plan.add(_LABEL, [sys.executable, str(counter), str(marker)], repo)
    monkeypatch.setattr(gate, "build_plan", lambda _args: plan)
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "1")
    assert gate.main([]) == 0
    assert gate.main([]) == 0
    assert marker.read_text(encoding="utf-8") == "x"
    assert f"[{_LABEL}] reused, not run" in capsys.readouterr().err
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "2")
    assert gate.main([]) == 0
    assert marker.read_text(encoding="utf-8") == "xx"
    assert gate.main(["--no-reuse"]) == 0
    assert marker.read_text(encoding="utf-8") == "xxx"


# ---------------------------------------------------------------------------
# Identity: the whole command environment, and the tools behind the argv
# ---------------------------------------------------------------------------


@pytest.fixture
def tree_only(gate, monkeypatch):
    """Pin the tree and leave the tools real, for tests that change what names them."""
    monkeypatch.setattr(gate, "tree_id", lambda: "tree-a")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PYTEST_XDIST_AUTO_NUM_WORKERS", "12"),
        ("I18N_BASE_REF", "base-b"),
        ("PATH", "/usr/bin:/bin"),
        ("KIROCREW_ANY_NEW_VARIABLE", "1"),
        ("PYTEST_XDIST_AUTO_NUM_WORKERS", None),
    ],
    ids=["worker-cap-2-to-12", "base-ref", "path", "added", "removed"],
)
def test_changing_any_one_environment_variable_runs_the_command(
    gate, pinned, tmp_path, name, value
) -> None:
    _store(gate, tmp_path).record(_CMD, _REPO_ROOT)
    env = {k: v for k, v in _ENV.items() if k != name}
    if value is not None:
        env[name] = value
    assert _store(gate, tmp_path, env=env).age(_CMD, _REPO_ROOT) is None


def test_the_same_environment_in_another_order_is_still_reused(gate, pinned, tmp_path) -> None:
    _store(gate, tmp_path).record(_CMD, _REPO_ROOT)
    reordered = dict(reversed(list(_ENV.items())))
    assert reordered != {} and list(reordered) != list(_ENV)
    assert _store(gate, tmp_path, env=reordered).age(_CMD, _REPO_ROOT) == 0.0


def test_the_proof_file_holds_a_digest_of_the_environment_never_its_values(
    gate, pinned, tmp_path
) -> None:
    env = {**_ENV, "SOME_SECRET_TOKEN": "hunter2-never-written"}
    store = _store(gate, tmp_path, env=env)
    store.record(_CMD, _REPO_ROOT)
    text = store._path(_CMD, _REPO_ROOT).read_text(encoding="utf-8")
    assert "hunter2-never-written" not in text and "SOME_SECRET_TOKEN" not in text
    assert store.age(_CMD, _REPO_ROOT) == 0.0


def test_a_new_interpreter_version_runs_the_command(gate, tree_only, monkeypatch, tmp_path) -> None:
    _store(gate, tmp_path).record(_CMD, _REPO_ROOT)
    assert _store(gate, tmp_path).age(_CMD, _REPO_ROOT) == 0.0
    monkeypatch.setattr(sys, "version", "3.99.0 (a build the proof was not made under)")
    assert _store(gate, tmp_path).age(_CMD, _REPO_ROOT) is None


def _install(site: Path, version: str) -> None:
    """Make ``gatefake`` the one installed copy at ``version``, as an upgrade leaves it."""
    for old in site.glob("gatefake-*.dist-info"):
        shutil.rmtree(old)
    dist = site / f"gatefake-{version}.dist-info"
    dist.mkdir(parents=True)
    meta = f"Metadata-Version: 2.1\nName: gatefake\nVersion: {version}\n"
    (dist / "METADATA").write_text(meta, encoding="utf-8")
    stamp = int(float(version) * 1000)  # importlib.metadata caches a directory by its mtime
    os.utime(site, (stamp, stamp))
    importlib.invalidate_caches()


def test_installing_upgrading_or_removing_a_package_runs_the_command(
    gate, tree_only, monkeypatch, tmp_path
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.syspath_prepend(str(site))
    _install(site, "1.0")
    _store(gate, tmp_path).record(_CMD, _REPO_ROOT)
    assert _store(gate, tmp_path).age(_CMD, _REPO_ROOT) == 0.0
    _install(site, "2.0")
    assert _store(gate, tmp_path).age(_CMD, _REPO_ROOT) is None
    _store(gate, tmp_path).record(_CMD, _REPO_ROOT)
    assert _store(gate, tmp_path).age(_CMD, _REPO_ROOT) == 0.0
    shutil.rmtree(site / "gatefake-2.0.dist-info")
    importlib.invalidate_caches()
    assert _store(gate, tmp_path).age(_CMD, _REPO_ROOT) is None


def test_an_error_while_naming_the_tools_is_no_identity_and_not_a_crash(gate, monkeypatch) -> None:
    def explode():
        raise RuntimeError("metadata exploded")

    monkeypatch.setattr(gate.importlib.metadata, "distributions", explode)
    assert gate.tool_versions(_CMD, _REPO_ROOT, dict(_ENV)) is None


@pytest.fixture
def node_world(gate, tree_only, monkeypatch, tmp_path):
    """A frontend command whose launcher, node and node_modules the test controls."""
    cwd = tmp_path / "website"
    (cwd / "node_modules").mkdir(parents=True)
    lock = cwd / "node_modules" / ".package-lock.json"
    lock.write_text('{"packages": {"node_modules/vitest": {"version": "2.1.1"}}}', encoding="utf-8")
    world = types.SimpleNamespace(
        cmd=[str(tmp_path / "shims" / "npx"), "vitest", "run", "src/a.test.ts"],
        cwd=cwd,
        lock=lock,
        versions={"npx": "11.19.0", "node": "v24.20.0"},
        node="/shims/node",
        calls=[],
        which_paths=[],
    )

    def fake_output(argv, run_cwd, env=None):
        world.calls.append((Path(argv[0]).stem, argv[1:], run_cwd, env))
        return world.versions.get(Path(argv[0]).stem)

    def fake_which(name, path=None):
        world.which_paths.append((name, path))
        return world.node if name == "node" else None

    monkeypatch.setattr(gate, "_output", fake_output)
    monkeypatch.setattr(gate.shutil, "which", fake_which)
    return world


def test_a_frontend_pass_is_reused_while_node_and_its_packages_are_unchanged(
    gate, node_world, tmp_path
) -> None:
    _store(gate, tmp_path).record(node_world.cmd, node_world.cwd)
    assert _store(gate, tmp_path).age(node_world.cmd, node_world.cwd) == 0.0


@pytest.mark.parametrize("change", ["launcher", "node", "installed-packages"])
def test_changing_the_launcher_node_or_installed_node_packages_runs_the_command(
    gate, node_world, tmp_path, change
) -> None:
    _store(gate, tmp_path).record(node_world.cmd, node_world.cwd)
    if change == "launcher":
        node_world.versions["npx"] = "11.20.0"
    elif change == "node":
        node_world.versions["node"] = "v22.1.0"
    else:
        node_world.lock.write_text('{"vitest": "2.2.0"}', encoding="utf-8")
    assert _store(gate, tmp_path).age(node_world.cmd, node_world.cwd) is None


@pytest.mark.parametrize(
    "gap", ["no-launcher-version", "no-node-on-path", "no-node-version", "no-lockfile"]
)
def test_a_frontend_command_whose_tools_cannot_be_named_runs_and_keeps_nothing(
    gate, node_world, tmp_path, gap
) -> None:
    if gap == "no-launcher-version":
        node_world.versions.pop("npx")
    elif gap == "no-node-on-path":
        node_world.node = None
    elif gap == "no-node-version":
        node_world.versions.pop("node")
    else:
        node_world.lock.unlink()
    store = _store(gate, tmp_path)
    store.record(node_world.cmd, node_world.cwd)
    assert list(tmp_path.glob("*.json")) == []
    assert store.age(node_world.cmd, node_world.cwd) is None


def test_the_node_probes_run_where_and_as_the_command_will(gate, node_world, tmp_path) -> None:
    env = {**_ENV, "PATH": "/shims:/bin"}
    _store(gate, tmp_path, env=env).age(node_world.cmd, node_world.cwd)
    assert node_world.which_paths == [("node", "/shims:/bin")]
    assert node_world.calls == [
        ("npx", ["--version"], node_world.cwd, env),
        ("node", ["--version"], node_world.cwd, env),
    ]


def test_a_python_command_never_probes_node(gate, node_world, tmp_path) -> None:
    _store(gate, tmp_path).age(_CMD, _REPO_ROOT)
    assert node_world.calls == [] and node_world.which_paths == []


def test_a_pass_recorded_under_two_workers_is_not_reused_under_twelve(gate, rig) -> None:
    assert gate.main([]) == 0
    rig.env = {**_ENV, "PYTEST_XDIST_AUTO_NUM_WORKERS": "12"}
    assert gate.main([]) == 0
    assert rig.runs == 2
    assert gate.main([]) == 0
    assert rig.runs == 2


def test_the_tools_are_named_before_the_command_runs_even_under_no_reuse(gate, rig) -> None:
    assert gate.main(["--no-reuse"]) == 0
    assert rig.order == ["tools", "run"]


def test_a_pass_is_filed_under_the_tools_read_before_the_run(gate, rig) -> None:
    def upgrade_while_running() -> None:
        rig.tools = {**_TOOLS, "packages": "p2"}

    rig.during_run = upgrade_while_running
    assert gate.main([]) == 0
    rig.during_run = None
    assert gate.main([]) == 0
    assert rig.runs == 2


def test_unnamed_tools_run_the_command_every_time_and_keep_nothing(gate, rig) -> None:
    rig.tools = None
    assert gate.main([]) == 0
    assert gate.main([]) == 0
    assert rig.runs == 2
    assert not rig.proofs_dir.exists()
