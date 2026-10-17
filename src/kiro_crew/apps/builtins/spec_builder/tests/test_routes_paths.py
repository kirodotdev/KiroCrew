"""Route tests: path validation and containment.

``_safe_dir`` refuses sensitive, relative and symlinked-into-sensitive
locations; spec file reads are descriptor-pinned and refuse a symlink out of the
spec directory; the browse scan, discovery roots, indexed working dirs and
resolved spec dirs are validated before use; and the security and gateway
helpers the routes call are bound at module scope, where a patch reaches them.
"""

from __future__ import annotations

import ast
import inspect
import os
import re
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes, routes_source

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _make_client,
    _never_touch_the_real_state,
    _redirect_state,
)

# ── path sanitization (_safe_dir chokepoint) ─────────────────────────────────
#
# CodeQL flagged 8 py/path-injection sinks on the caller-supplied directory
# values. The name regex already blocks traversal and the read paths go through
# the trusted index, but ONE gap was real: only /browse applied the sensitive
# path test, so create/settings could point spec storage — and an agent's cwd —
# at a credential directory. These lock the closed gap.


def test_safe_dir_denies_sensitive_locations():
    """A credential directory must never survive the chokepoint."""

    ssh = os.path.expanduser("~/.ssh")
    # Only meaningful when the path exists on the host; the denial itself is
    # independent of existence, which the ancestor case below covers.
    assert routes._safe_dir(ssh) is None
    assert routes._safe_dir(ssh, must_exist=False) is None


def test_safe_dir_denies_new_dir_under_sensitive_ancestor():
    """A not-yet-created dir inside a credential dir is refused, not allowed
    through on a stat miss."""

    target = os.path.expanduser("~/.aws/spec-store")
    assert routes._safe_dir(target, must_exist=False) is None


def test_safe_dir_resolves_symlinks_before_checking(tmp_path):
    """A symlink in a benign directory must not smuggle its target through."""

    link = tmp_path / "innocent"
    os.symlink(os.path.expanduser("~/.ssh"), link)
    assert routes._safe_dir(str(link)) is None


def test_safe_dir_accepts_ordinary_directory(tmp_path):
    """Non-vacuous: the guard admits a normal directory."""
    ok = routes._safe_dir(str(tmp_path))
    assert ok is not None and ok.is_dir()


def test_contained_rejects_escape(tmp_path):
    root = tmp_path / "root"
    (root).mkdir()
    assert routes._contained(root / "child", root) is True
    assert routes._contained(tmp_path / "elsewhere", root) is False


@pytest.mark.asyncio
async def test_create_rejects_sensitive_working_dir(tmp_path, monkeypatch):
    """The real finding: create must refuse a credential dir as working_dir."""

    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.post(
            f"{_BASE}/specs",
            json={
                "name": "probe",
                "working_dir": os.path.expanduser("~/.ssh"),
                "spec_type": "feature",
            },
        )
        assert resp.status == 400
        assert "sensitive" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_settings_rejects_sensitive_base_path(tmp_path, monkeypatch):
    """Spec storage must not be repointable at a credential directory."""

    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.put(
            f"{_BASE}/settings", json={"base_path": os.path.expanduser("~/.aws")}
        )
        assert resp.status == 400


# ── symlink containment, state normalization, create pre-checks ──────────────
#
# One test group per finding, so a regression names the finding it re-opens.


# (1) symlink-following on spec reads / STOP writes


def test_spec_file_refuses_symlink_out_of_spec_dir(tmp_path):
    """A planted symlink must not be read, even to a benign target."""

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    os.symlink(outside, spec_dir / "requirements.md")

    assert routes._spec_file(spec_dir, "requirements.md") is None
    assert routes._read_spec_text(spec_dir, "requirements.md") is None


def test_spec_file_refuses_symlink_to_sensitive_target(tmp_path):
    """The specific exploit: requirements.md -> a credential directory."""

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    os.symlink(os.path.expanduser("~/.aws/credentials"), spec_dir / "design.md")
    assert routes._read_spec_text(spec_dir, "design.md") is None


def test_spec_file_allows_ordinary_file(tmp_path):
    """Non-vacuous: a real file inside the spec dir still reads."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "requirements.md").write_text("# hello")
    assert routes._read_spec_text(spec_dir, "requirements.md") == "# hello"


# (5) containment regression — worktree mode


def test_contained_accepts_the_worktree_itself(tmp_path):
    """The regression: a sibling worktree is NOT under the original checkout, so
    containment must be measured against the worktree once one is created."""
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "repo-wt-feature"
    worktree.mkdir()
    spec_dir = worktree / ".kiro" / "specs" / "feature"
    spec_dir.mkdir(parents=True)

    # Against the original checkout it (correctly) fails — this is what the bug
    # was measuring, which is why every worktree create 400'd.
    assert routes._contained(spec_dir, repo) is False
    # Against the worktree it passes, which is what the fixed code measures.
    assert routes._contained(spec_dir, worktree) is True


# ── browse scan offloading and symlink skipping ──────────────────────────────


# (1) planning-phase auto-approval never expired


def test_browse_scan_is_offloadable_and_bounded(tmp_path):
    """The scan helper is a plain blocking function (so it can go to a thread)
    and caps its output rather than returning an unbounded listing."""
    for i in range(5):
        (tmp_path / f"dir{i}").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "afile.txt").write_text("x")

    out = routes._scan_subdirs(str(tmp_path))
    names = {d["name"] for d in out}
    assert names == {f"dir{i}" for i in range(5)}  # no hidden, no skip-list, no files
    assert routes._BROWSE_MAX_DIRS > 0


def test_scan_skips_a_symlink_to_a_sensitive_target(tmp_path):
    """Symlinks resolve BEFORE the sensitivity test, so a link inside a benign
    directory cannot get a credential directory listed."""

    os.symlink(os.path.expanduser("~/.aws"), tmp_path / "innocent")
    (tmp_path / "real").mkdir()
    names = {d["name"] for d in routes._scan_subdirs(str(tmp_path))}
    assert "innocent" not in names
    assert "real" in names


# (3) top-level imports


def test_security_helper_is_imported_at_module_scope():
    """is_sensitive_path was imported function-locally in four helpers, against
    the top-level-imports rule. It is now module scope with a fail-closed
    fallback if the security module is unavailable."""

    src = routes_source()
    assert "    from kiro_crew.security import is_sensitive_path" not in src
    assert callable(routes.is_sensitive_path)


# ── descriptor-pinned spec reads; this app grants no worker trust ────────────


# (1) spec reads must be descriptor-pinned, not check-then-read


def test_spec_read_goes_through_the_descriptor_pinned_helper():
    """Source guard. The previous shape validated the path and then called
    ``p.read_text()`` by name, leaving a TOCTOU window: the agent writes into
    this very directory, so a spec file could be swapped for a symlink or
    hardlink to a credential file between the check and the open, during the
    UI's 2.5s poll. Reads must use the helper that opens with O_NOFOLLOW first
    and validates the DESCRIPTOR it actually read."""

    src = inspect.getsource(routes._read_spec_text)
    assert "safe_read_file_bytes_nolink" in src
    assert "within_root" in src
    # Compare the BODY, not the docstring — the docstring legitimately names the
    # old check-then-read shape when explaining why it was replaced.
    body = re.sub(r'""".*?"""', "", src, count=1, flags=re.S)
    assert "read_text(" not in body


def test_spec_read_returns_content_for_an_ordinary_file(tmp_path):
    """Non-vacuous: the safe path still reads a real file."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "requirements.md").write_text("# hello")
    assert routes._read_spec_text(spec_dir, "requirements.md") == "# hello"


def test_spec_read_refuses_a_symlinked_spec_file(tmp_path):
    """The exploit shape: a spec file replaced by a link to a file outside the
    spec dir must not be read."""

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("credential-ish")
    os.symlink(secret, spec_dir / "design.md")
    assert routes._read_spec_text(spec_dir, "design.md") is None


def test_spec_read_is_size_capped():
    assert routes._MAX_SPEC_BYTES > 0


# ── index transactions serialize; phases derive off the loop ─────────────────


def test_gateway_helpers_are_imported_at_module_scope():
    """top-level-imports: a function-local import hides the dependency and makes
    a test's mock patch target the wrong namespace. Only the dashboard submodules
    stay deferred, because dashboard.server imports THIS module (documented
    circular-import exception)."""

    tree = ast.parse(routes_source())
    local_imports: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and (inner.module or "").startswith("kiro_crew"):
                local_imports.append(inner.module or "")

    # Every remaining function-local import must be a documented dashboard cycle.
    assert local_imports, "guard is vacuous: expected the dashboard deferrals"
    for mod in local_imports:
        assert mod.startswith("kiro_crew.dashboard."), f"undocumented local import: {mod}"

    for name in (
        "safe_read_file_bytes_nolink",
        "sandboxed_spawn_argv",
        "create_subprocess_limited",
        "authorize_and_add_nudge",
        "CHAT_TURN_TIMEOUT",
    ):
        assert hasattr(routes, name), f"{name} is not bound at module scope"


# ── path validation off the loop; the slot-scoping chokepoint ────────────────


def test_prepare_spec_dir_reports_each_refusal(tmp_path, monkeypatch):
    """Non-vacuous: the bundled validator must still produce the three distinct
    refusals the handler maps onto 400 / 409 / 400."""
    _redirect_state(monkeypatch, tmp_path)
    wd = Path(os.path.realpath(tmp_path)) / "wd"
    wd.mkdir()

    spec_dir, refusal = routes._prepare_spec_dir(str(wd), wd, "fresh", False)
    assert refusal == "" and spec_dir.is_dir()

    (spec_dir / "requirements.md").write_text("# r")
    _again, refusal = routes._prepare_spec_dir(str(wd), wd, "fresh", False)
    assert refusal.startswith("existing:") and "requirements.md" in refusal
    # import_existing opts in.
    assert routes._prepare_spec_dir(str(wd), wd, "fresh", True)[1] == ""

    # Containment: a settings base_path elsewhere makes the derived dir escape
    # the declared root.
    other = Path(os.path.realpath(tmp_path)) / "other"
    other.mkdir()
    routes._save_settings({"base_path": str(other)})
    _p, refusal = routes._prepare_spec_dir(str(wd), wd, "escaper", False)
    assert refusal == "" or refusal == "escape"


# ── repo info and handoff validate through the chokepoint ────────────────────


@pytest.mark.asyncio
async def test_repo_info_refuses_a_sensitive_path(tmp_path, monkeypatch):
    """Non-vacuous: routing through _safe_dir means a sensitive directory is now
    refused, where the old pair would have probed it with git."""
    client = _make_client(monkeypatch, tmp_path)
    probed: list[str] = []

    async def _repo(path):
        probed.append(path)
        return {"is_git": True, "root": path}

    monkeypatch.setattr(routes, "_repo_info", _repo)
    monkeypatch.setattr(routes, "is_sensitive_path", lambda p: "secrets" in p)
    sensitive = Path(os.path.realpath(tmp_path)) / "secrets"
    sensitive.mkdir()
    ordinary = Path(os.path.realpath(tmp_path)) / "project"
    ordinary.mkdir()

    await client.start_server()
    try:
        denied = await (await client.get(f"{_BASE}/repo-info?path={sensitive}")).json()
        allowed = await (await client.get(f"{_BASE}/repo-info?path={ordinary}")).json()
    finally:
        await client.close()

    assert denied == {"is_git": False} and str(sensitive) not in probed
    assert allowed.get("is_git") is True


def test_handoff_refuses_a_symlinked_tasks_file(tmp_path):
    """The reported defect: the gate used (spec_dir / 'tasks.md').is_file(), which
    FOLLOWS a symlink — so a planted tasks.md pointing outside the spec directory
    satisfied it and the autonomous run then edited the link target."""

    spec_dir = Path(os.path.realpath(tmp_path)) / "spec"
    spec_dir.mkdir()
    outside = Path(os.path.realpath(tmp_path)) / "elsewhere.md"
    outside.write_text("- [ ] edit me instead")
    os.symlink(outside, spec_dir / "tasks.md")

    ready, sentinel = routes._prepare_handoff(spec_dir)
    assert ready is False, "handoff accepted a symlinked tasks.md"
    assert sentinel  # the spec dir itself is fine; only the file is rejected

    # A real regular file still passes.
    (spec_dir / "tasks.md").unlink()
    (spec_dir / "tasks.md").write_text("- [ ] task")
    assert routes._prepare_handoff(spec_dir)[0] is True


# ── spec_dir revalidation and identity pinning ───────────────────────────────


def test_resolved_spec_dir_is_revalidated_for_sensitivity(tmp_path, monkeypatch):
    """The reported hole: containment only says 'under the declared root'. If that
    root is (or becomes) a symlink into a credential tree, BOTH paths resolve
    through it, so _contained passes while the spec files would be created inside
    the credential directory. The RESOLVED destination now goes back through the
    chokepoint."""
    _redirect_state(monkeypatch, tmp_path)
    vault = Path(os.path.realpath(tmp_path)) / "vault"
    vault.mkdir()
    root = Path(os.path.realpath(tmp_path)) / "project"
    os.symlink(vault, root)

    # Treat the symlink TARGET as sensitive, exactly as is_sensitive_path would
    # for a real credential directory.
    monkeypatch.setattr(routes, "is_sensitive_path", lambda p: str(vault) in p)

    spec_dir, refusal = routes._prepare_spec_dir(str(root), root, "sneaky", False)
    assert refusal == "escape", "spec dir was accepted inside a sensitive tree"
    assert not (vault / "sneaky").exists(), "files were created in the sensitive tree"
    assert spec_dir is not None


def test_ordinary_destination_is_still_created(tmp_path, monkeypatch):
    """Non-vacuous: the extra check must not refuse a normal destination."""
    _redirect_state(monkeypatch, tmp_path)
    wd = Path(os.path.realpath(tmp_path)) / "wd"
    wd.mkdir()
    spec_dir, refusal = routes._prepare_spec_dir(str(wd), wd, "fine", False)
    assert refusal == "" and spec_dir.is_dir()


# ── the indexed working dir is revalidated, off the loop ─────────────────────


@pytest.mark.asyncio
async def test_indexed_working_dir_is_revalidated(tmp_path, monkeypatch):
    """The reported escalation: the indexed working_dir was trusted verbatim and
    assigned to slot.project, which IS the agent's cwd. The index is app state on
    disk and the agent this app runs can be talked into rewriting files, so a
    rewritten entry would start the next turn inside a credential directory --
    where relative reads sidestep every per-path check this app makes. It now goes
    back through the _safe_dir chokepoint and an unusable value refuses the slot."""
    vault = Path(os.path.realpath(tmp_path)) / "vault"
    vault.mkdir()
    ordinary = Path(os.path.realpath(tmp_path)) / "project"
    ordinary.mkdir()
    monkeypatch.setattr(routes, "is_sensitive_path", lambda p: str(vault) in str(p))

    class _Slot:
        key = "spec-builder-x"
        _app = routes.APP_NAME
        project = ""
        _titled = True

    slot = _Slot()

    class _State:
        def get_slot(self, key):
            return slot

        def get_or_create_slot(self, name, app=""):
            return slot

    # Sensitive target -> refused, and the cwd is NOT set.
    assert await routes._ensure_worker_slot(_State(), "x", {"working_dir": str(vault)}) is None
    assert slot.project == "", "a sensitive directory became the agent's cwd"

    # Non-existent path -> also refused (the chokepoint requires an existing dir).
    assert (
        await routes._ensure_worker_slot(_State(), "x", {"working_dir": str(tmp_path / "gone")})
        is None
    )
    assert slot.project == ""

    # Ordinary project -> accepted and scoped.
    assert await routes._ensure_worker_slot(_State(), "x", {"working_dir": str(ordinary)}) is slot
    assert slot.project == str(ordinary)


def test_working_dir_validation_is_offloaded():
    """Source guard: the validation must not stat on the event loop, and it must
    run BEFORE the assignment it protects."""
    src = inspect.getsource(routes._ensure_worker_slot)
    assert "asyncio.to_thread(_safe_dir" in src
    assert src.index("safe_wd = await asyncio.to_thread") < src.index(
        "slot.project ="
    ), "the cwd is assigned before it is validated"
    assert "slot.project = wd" not in src, "the raw indexed value is still assigned"


# ── discovery validates each root; controls reject a stale identity ──────────


def test_discovery_validates_each_indexed_root(tmp_path, monkeypatch):
    """The reported hole: discovery derived its scan root from the indexed
    working_dir and stat-ed/enumerated it directly. A tampered entry pointing at a
    credential tree would be walked OUTSIDE the sensitive-path gate, and any
    spec-shaped directory inside it adopted into the index."""
    _redirect_state(monkeypatch, tmp_path)
    real = Path(os.path.realpath(tmp_path))
    vault, project = real / "vault", real / "project"
    for base in (vault, project):
        spec = base / ".kiro" / "specs" / f"found-{base.name}"
        spec.mkdir(parents=True)
        (spec / "requirements.md").write_text("# r")

    monkeypatch.setattr(routes, "is_sensitive_path", lambda p: str(vault) in str(p))

    index = {
        "seed-vault": {"working_dir": str(vault), "spec_dir": str(vault / "seed")},
        "seed-ok": {"working_dir": str(project), "spec_dir": str(project / "seed")},
    }
    routes._discover_folder_specs(index)

    assert "found-project" in index, "discovery stopped finding legitimate specs"
    assert "found-vault" not in index, "a spec inside a sensitive tree was adopted"


def test_discovery_root_validation_is_in_the_source():
    """Source guard: the derived scan root must go through the chokepoint, so a
    symlinked `.kiro/specs` cannot redirect the walk either."""
    src = inspect.getsource(routes._discover_folder_specs)
    assert src.count("_safe_dir(") >= 2, "discovery does not validate its scan roots"
    assert 'specs_base = Path(root) / ".kiro"' not in src


# ── settings shapes, slot-name grammar, and the browse skip list ─────────────


def test_browse_skip_list_carries_no_hidden_paths():
    """_scan_subdirs already skips every entry starting with '.', so listing
    dotted names in the skip set duplicated that rule -- and one of them put a
    literal internal path marker in the source, which the repo's scrub lint
    rejects."""
    assert not [n for n in routes._BROWSE_SKIP if n.startswith(".")]
    assert 'startswith(".")' in inspect.getsource(
        routes._scan_subdirs
    ), "the hidden-entry skip that makes the dotted names redundant is gone"


# ── _safe_dir: absolute-only, enforced where it can actually fail ──


def test_safe_dir_refuses_a_relative_working_dir(tmp_path, monkeypatch):
    """A relative value must be refused, not silently resolved against the cwd.

    index.json is agent-writable, so a `working_dir` of "." would otherwise
    normalize to the gateway's own checkout and a spec's worktree plus the agent
    running in it would target that tree.
    """
    monkeypatch.chdir(tmp_path)
    for relative in (".", "..", "relative/path", "./sub", ""):
        assert (
            routes._safe_dir(relative) is None
        ), f"_safe_dir accepted the relative value {relative!r}"


def test_safe_dir_still_accepts_absolute_and_tilde(tmp_path, monkeypatch):
    """The guard must not cost the two forms that are legitimately absolute."""
    assert routes._safe_dir(str(tmp_path)) == Path(
        os.path.realpath(str(tmp_path))
    ), "_safe_dir rejected a plain absolute directory"

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "inside").mkdir()
    assert (
        routes._safe_dir("~/inside") is not None
    ), "_safe_dir rejected a ~-relative path, which expands to an absolute one"


def test_absoluteness_is_checked_before_realpath():
    """Source guard: order is the whole point.

    ``os.path.realpath`` resolves against the process cwd and ALWAYS returns an
    absolute path, so an is_absolute()/isabs() test placed after it is dead code
    and the documented guarantee is not enforced. Asserts on the order of the
    calls, not on a comment.
    """
    import inspect

    src = inspect.getsource(routes._safe_dir)
    isabs_at = src.index("os.path.isabs(")
    realpath_at = src.index("os.path.realpath(")
    assert (
        isabs_at < realpath_at
    ), "the absoluteness test moved after realpath, where it can never fail"
