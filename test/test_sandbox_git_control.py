"""A sandboxed agent cannot rewrite the git control files of the gateway's own checkout.

The gateway runs ``git`` in its install checkout outside any sandbox, and git executes
what that checkout's ``config``, ``hooks/`` and attributes name. These cases pin the
three layers that keep a sandboxed agent from choosing that code:

* the host gather (:mod:`kiro_crew.sandbox_git_control`) names the right pins and
  seals for a plain checkout, a linked worktree, includes and ``core.hooksPath``;
* the planner hands them to both backends, and the launcher pins before it seals;
* the file-tool write gate refuses an edit to a git control file in any repository.

The real-namespace half is in ``test_sandbox_git_control_real_sandbox.py``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox_git_control as gc
from kiro_crew import sandbox_plan
from kiro_crew.sandbox_plan import (
    BACKEND_NAMESPACE,
    BACKEND_SEATBELT,
    PlanHost,
    SandboxRequest,
    namespace_payload,
    plan_confinement,
)
from kiro_crew.security import is_sensitive_write_path

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX path layout")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "ws" / "checkout").resolve()
    root.mkdir(parents=True)
    _git("init", "-q", cwd=root)
    (root / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "README.md", cwd=root)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=root)
    return root


# --------------------------------------------------------------------------- #
# The host gather.
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
def test_a_plain_checkout_pins_its_git_dir_and_seals_the_control_files(
    repo: Path,
) -> None:
    layout = gc.git_control_layout_for((str(repo),))
    gitdir = str(repo / ".git")
    assert gitdir in layout.pins
    assert str(repo) in layout.pins  # its parent is writable, so it could be renamed
    for name in ("config", "hooks", "info"):
        assert os.path.join(gitdir, name) in layout.readonly
    # Objects, refs, the index and HEAD stay writable: commits must still work.
    for name in ("objects", "refs", "index", "HEAD", "logs"):
        assert os.path.join(gitdir, name) not in layout.readonly
    # Outermost first, so no pin is bound over one inside it.
    assert list(layout.pins) == sorted(layout.pins, key=lambda p: p.count(os.sep))
    assert gitdir in layout.guards and str(repo) in layout.guards


@_POSIX_ONLY
def test_missing_hooks_and_info_are_created_so_they_can_be_sealed(repo: Path) -> None:
    for name in ("hooks", "info"):
        target = repo / ".git" / name
        subprocess.run(["rm", "-rf", str(target)], check=True)
    layout = gc.git_control_layout_for((str(repo),))
    for name in ("hooks", "info"):
        target = repo / ".git" / name
        assert target.is_dir() and not any(target.iterdir())
        assert str(target) in layout.readonly


@_POSIX_ONLY
def test_config_worktree_is_always_created_empty_and_sealed(repo: Path) -> None:
    # Whether git reads it depends on extensions.worktreeConfig, which a parser of the
    # config can misjudge; an empty sealed file is safe either way.
    worktree_config = repo / ".git" / "config.worktree"
    layout = gc.git_control_layout_for((str(repo),))
    assert worktree_config.is_file() and worktree_config.read_bytes() == b""
    assert str(worktree_config) in layout.readonly
    _git("config", "extensions.worktreeConfig", "true", cwd=repo)
    _git("status", "--short", cwd=repo)


@_POSIX_ONLY
def test_an_absent_commondir_is_listed_but_never_created(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Seatbelt's path rule denies creating it. A stand-in file would break libgit2,
    # which refuses to open a main repository that has one.
    with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
        layout = gc.git_control_layout_for((str(repo),))
    commondir = repo / ".git" / "commondir"
    assert str(commondir) in layout.readonly
    assert not commondir.exists()
    # The normal plain-checkout state logs nothing: every spawn plans this.
    assert not [r for r in caplog.records if r.name == "kiro_crew.sandbox"]


@_POSIX_ONLY
def test_a_linked_worktree_seals_its_pointer_and_the_common_config(
    repo: Path, tmp_path: Path
) -> None:
    linked = (tmp_path / "other" / "linked").resolve()
    linked.parent.mkdir()
    _git("worktree", "add", "-q", str(linked), cwd=repo)
    layout = gc.git_control_layout_for((str(linked),))
    common = repo / ".git"
    own = common / "worktrees" / "linked"
    # The pointer file and the commondir say which config applies: both sealed.
    assert str(linked / ".git") in layout.readonly
    assert str(own / "commondir") in layout.readonly
    assert str(common / "config") in layout.readonly
    assert str(common / "hooks") in layout.readonly
    # Every directory from the common dir down to the worktree's own is pinned, and so
    # is the main checkout above the common dir.
    for pin in (common, common / "worktrees", own, repo, linked):
        assert str(pin) in layout.pins
    assert str(own / "HEAD") not in layout.readonly


@_POSIX_ONLY
def test_include_targets_and_hooks_path_are_sealed(repo: Path, tmp_path: Path) -> None:
    included = tmp_path / "extra.gitconfig"
    nested = tmp_path / "nested.gitconfig"
    nested.write_text("[core]\n\tfsmonitor = false\n", encoding="utf-8")
    included.write_text(f'[include]\n\tpath = "{nested}"\n', encoding="utf-8")
    hooks = repo / ".githooks"
    hooks.mkdir()
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write(f'[includeIf "gitdir:{repo}/"]\n\tpath = {included}\n')
        fh.write("[core]\n\thooksPath = .githooks\n")
        fh.write("[include]\n\tpath = does-not-exist\n")
    layout = gc.git_control_layout_for((str(repo),))
    assert str(included.resolve()) in layout.readonly
    assert str(nested.resolve()) in layout.readonly
    assert str(hooks) in layout.readonly
    # An absent include is never created by the gateway: it stays listed, for
    # Seatbelt's path rule.
    absent = repo / ".git" / "does-not-exist"
    assert not absent.exists()
    assert str(absent) in layout.readonly
    # The include's directory is pinned, so it cannot be renamed and rebuilt.
    assert str(tmp_path.resolve()) in layout.pins


@_POSIX_ONLY
def test_a_nested_include_resolves_beside_the_symlinked_name(repo: Path, tmp_path: Path) -> None:
    shared = (tmp_path / "shared").resolve()
    shared.mkdir()
    (shared / "base.cfg").write_text("[include]\n\tpath = extra.cfg\n", encoding="utf-8")
    link = repo / "local.cfg"
    link.symlink_to(shared / "base.cfg")
    extra = repo / "extra.cfg"
    extra.write_text("", encoding="utf-8")
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write("[include]\n\tpath = ../local.cfg\n")
    layout = gc.git_control_layout_for((str(repo),))
    assert str(extra) in layout.readonly


@_POSIX_ONLY
def test_an_absent_hooks_path_is_listed_but_never_created(repo: Path) -> None:
    _git("config", "core.hooksPath", "githooks", cwd=repo)
    layout = gc.git_control_layout_for((str(repo),))
    assert not (repo / "githooks").exists()
    assert str(repo / "githooks") in layout.readonly


@_POSIX_ONLY
def test_a_commondir_in_a_plain_checkout_is_not_followed(repo: Path, tmp_path: Path) -> None:
    # An agent-planted commondir must not steer what the gateway parses or creates.
    evil = tmp_path / "evil"
    evil.mkdir()
    planted = tmp_path / "planted.gitconfig"
    (evil / "config").write_text(f"[include]\n\tpath = {planted}\n", encoding="utf-8")
    (repo / ".git" / "commondir").write_text(str(evil) + "\n", encoding="utf-8")
    layout = gc.git_control_layout_for((str(repo),))
    assert str(repo / ".git" / "config") in layout.readonly
    assert not any(p.startswith(str(evil)) for p in layout.readonly)
    assert not planted.exists() and not (evil / "hooks").exists()


@_POSIX_ONLY
def test_a_fifo_at_a_config_path_does_not_block(repo: Path) -> None:
    os.mkfifo(repo / ".git" / "config.worktree")
    assert gc._read_small(str(repo / ".git" / "config.worktree")) is None
    gc.git_control_layout_for((str(repo),))


@_POSIX_ONLY
def test_a_config_starting_with_a_bom_is_read(repo: Path, tmp_path: Path) -> None:
    target = tmp_path / "bom.gitconfig"
    target.write_text("", encoding="utf-8")
    config = repo / ".git" / "config"
    config.write_bytes(b"\xef\xbb\xbf[include]\n\tpath = " + str(target).encode() + b"\n")
    layout = gc.git_control_layout_for((str(repo),))
    assert str(target.resolve()) in layout.readonly


@_POSIX_ONLY
def test_an_include_continued_on_the_next_line_is_sealed(repo: Path, tmp_path: Path) -> None:
    target = tmp_path / "continued.gitconfig"
    target.write_text("", encoding="utf-8")
    head, tail = str(target)[:-8], str(target)[-8:]
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write(f"[include]\n\tpath = {head}\\\n{tail}\n")
    layout = gc.git_control_layout_for((str(repo),))
    assert str(target.resolve()) in layout.readonly


@_POSIX_ONLY
def test_includes_are_read_as_git_reads_them(repo: Path, tmp_path: Path) -> None:
    # A comment ending in a backslash, a quoted path with spaces and an includeIf
    # whose condition holds: git parses all three, so the gather does too.
    spaced = repo / ".git" / "  spaced.cfg"
    spaced.write_text("", encoding="utf-8")
    conditional = tmp_path / "cond.cfg"
    conditional.write_text("", encoding="utf-8")
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write('[include]\n\t# a comment ending \\\n\tpath = "  spaced.cfg"\n')
        fh.write(f'[includeIf "gitdir:{repo}/"]\n\tpath = {conditional}\n')
    layout = gc.git_control_layout_for((str(repo),))
    assert str(spaced) in layout.readonly
    assert str(conditional.resolve()) in layout.readonly


@_POSIX_ONLY
def test_includes_under_an_inactive_condition_are_sealed(repo: Path) -> None:
    # The agent can switch branch (HEAD stays writable) and turn the condition on.
    branch_cfg = repo / ".git" / "branch.cfg"
    nested = repo / "nested.cfg"
    nested.write_text("", encoding="utf-8")
    branch_cfg.write_text(f"[include]\n\tpath = {nested}\n", encoding="utf-8")
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write('[includeIf "onbranch:feature/**"]\n\tpath = branch.cfg\n')
    layout = gc.git_control_layout_for((str(repo),))
    assert str(branch_cfg) in layout.readonly
    assert str(nested) in layout.readonly


@_POSIX_ONLY
def test_a_prefix_relative_include_is_expanded_by_git(repo: Path) -> None:
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write("[include]\n\tpath = %(prefix)/etc/kirocrew-test.cfg\n")
    layout = gc.git_control_layout_for((str(repo),))
    named = [p for p in layout.readonly if p.endswith("kirocrew-test.cfg")]
    assert named and all("%(prefix)" not in p for p in named)
    assert not any(p.startswith(str(repo / ".git" / "%(prefix)")) for p in named)


@_POSIX_ONLY
def test_a_git_on_path_is_never_run(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim_dir = tmp_path / "shimbin"
    shim_dir.mkdir()
    marker = tmp_path / "ran"
    shim = shim_dir / "git"
    shim.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    gc.git_control_layout_for((str(repo),))
    assert not marker.exists()


@_POSIX_ONLY
def test_a_symlinked_config_pins_its_target_parents(repo: Path, tmp_path: Path) -> None:
    shared = (tmp_path / "shared").resolve()
    shared.mkdir()
    config = repo / ".git" / "config"
    target = shared / "config"
    target.write_bytes(config.read_bytes())
    config.unlink()
    config.symlink_to(target)
    layout = gc.git_control_layout_for((str(repo),))
    assert str(target) in layout.readonly
    # The target's directory cannot be renamed and rebuilt under the link.
    assert str(shared) in layout.pins


@_POSIX_ONLY
def test_a_symlinked_hooks_dir_seals_its_target(repo: Path, tmp_path: Path) -> None:
    real = (tmp_path / "shared-hooks").resolve()
    real.mkdir()
    hooks = repo / ".git" / "hooks"
    subprocess.run(["rm", "-rf", str(hooks)], check=True)
    hooks.symlink_to(real, target_is_directory=True)
    layout = gc.git_control_layout_for((str(repo),))
    assert str(real) in layout.readonly


def test_a_root_without_git_contributes_nothing(tmp_path: Path) -> None:
    assert gc.git_control_layout_for((str(tmp_path),)) == gc.GitControlLayout()


def test_the_resolver_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> tuple[str, ...]:
        raise RuntimeError("boom")

    monkeypatch.setattr(gc, "gateway_checkout_roots", boom)
    assert gc.resolved_git_control_layout() == gc.GitControlLayout()


@_POSIX_ONLY
def test_the_project_dir_is_a_gateway_checkout(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(repo))
    assert str(repo) in gc.gateway_checkout_roots()


# --------------------------------------------------------------------------- #
# The planner and both renderers.
# --------------------------------------------------------------------------- #

_LAYOUT = gc.GitControlLayout(
    pins=("/w", "/w/r", "/w/r/.git"),
    readonly=("/w/r/.git/config", "/w/r/.git/hooks", "/w/r/.git/info"),
    guards=("/w/r/.git", "/w/r", "/w"),
)


def _host(backend: str) -> PlanHost:
    namespace = backend == BACKEND_NAMESPACE
    return PlanHost(
        home="/home/u",
        git_control_pins=_LAYOUT.pins if namespace else (),
        git_control_readonly=_LAYOUT.readonly,
        git_control_guards=() if namespace else _LAYOUT.guards,
    )


@pytest.mark.parametrize("tier", ["standard", "cc", "strict"])
def test_the_namespace_launcher_receives_pins_and_seals(tier: str) -> None:
    plan = plan_confinement(SandboxRequest(tier=tier), _host(BACKEND_NAMESPACE))
    payload = namespace_payload(plan)
    assert payload["pin_dirs"] == list(_LAYOUT.pins)
    for target in _LAYOUT.readonly:
        assert target in payload["readonly_dirs"]


@pytest.mark.parametrize("tier", ["standard", "cc", "strict"])
def test_the_seatbelt_profile_denies_writes_and_renames(tier: str) -> None:
    from kiro_crew.sandbox_seatbelt import render_seatbelt_profile

    plan = plan_confinement(
        SandboxRequest(tier=tier, backend=BACKEND_SEATBELT), _host(BACKEND_SEATBELT)
    )
    profile = render_seatbelt_profile(plan)
    for target in _LAYOUT.readonly:
        assert f'(deny file-write* (subpath "{target}"))' in profile
        assert f'(deny file-write* (literal "{target}"))' in profile
    for guard in _LAYOUT.guards:
        assert f'(deny file-write* (literal "{guard}"))' in profile
    # A guard is a literal: the directory's children stay writable.
    assert '(deny file-write* (subpath "/w/r/.git"))' not in profile


def test_a_writable_carveout_cannot_reopen_a_seal() -> None:
    host = PlanHost(
        home="/home/u",
        git_control_readonly=_LAYOUT.readonly,
        voice_runtime_parents=("/w/r/.git",),
        carveout_probes=(
            sandbox_plan.CarveoutProbe(
                raw="/w/r/.git/hooks",
                lexical="/w/r/.git/hooks",
                canonical="/w/r/.git/hooks",
                is_dir=True,
            ),
        ),
    )
    plan = plan_confinement(
        SandboxRequest(tier="standard", extra_writable_dirs=("/w/r/.git/hooks",)), host
    )
    assert plan.writable == ()


def test_a_host_without_a_checkout_plans_no_pins() -> None:
    plan = plan_confinement(SandboxRequest(tier="standard"), PlanHost(home="/home/u"))
    assert namespace_payload(plan)["pin_dirs"] == []


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the namespace launcher is Linux-only"
)
def test_the_launcher_pins_before_it_seals_and_reenters_its_cwd() -> None:
    import inspect

    from kiro_crew import sandbox_launcher_program as program

    order = inspect.getsource(program.place_masks)
    assert order.index("pin_dirs(launch)") < order.index("stage_private_windows(launch)")
    assert order.index("pin_dirs(launch)") < order.index("seal_readonly(launch)")
    child = inspect.getsource(program.run_child)
    assert child.index("place_masks(launch)") < child.index("reenter_pinned_cwd(launch)")
    assert child.index("reenter_pinned_cwd(launch)") < child.index("exec_agent(launch, argv)")


# --------------------------------------------------------------------------- #
# The file-tool write gate.
# --------------------------------------------------------------------------- #


@pytest.fixture
def gateway(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """*repo* as the gateway's only checkout."""
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(repo))
    monkeypatch.setattr(gc, "_own_source_checkout", lambda: None)
    return repo


_CONTROL = [
    ".git/config",
    ".git/config.worktree",
    ".git/commondir",
    ".git/hooks/pre-commit",
    ".git/hooks",
    ".git/info/attributes",
    ".git/info/exclude",
    ".git/worktrees/w/commondir",
    ".git/worktrees/w/config.worktree",
    ".git/modules/sub/config",
    ".git/modules/sub/hooks/post-checkout",
    ".git",
    # Case variants reach the same entry on a case-insensitive filesystem.
    ".GIT/CONFIG",
    ".Git/Hooks/x",
]


@_POSIX_ONLY
@pytest.mark.parametrize("rel", _CONTROL)
def test_the_file_tool_refuses_the_gateway_checkouts_control_files(gateway: Path, rel: str) -> None:
    assert gc.git_control_write(str(gateway / rel))
    assert is_sensitive_write_path(str(gateway / rel))


@_POSIX_ONLY
@pytest.mark.parametrize("rel", _CONTROL)
def test_another_repository_keeps_its_file_tool_route(
    gateway: Path, tmp_path: Path, rel: str
) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    _git("init", "-q", cwd=other)
    assert not gc.git_control_write(str(other / rel))
    assert not is_sensitive_write_path(str(other / rel))


@_POSIX_ONLY
@pytest.mark.parametrize(
    "rel",
    [
        "src/app.py",
        ".gitattributes",
        ".gitignore",
        ".github/workflows/ci.yml",
        ".git/objects/ab/cdef",
        ".git/refs/heads/main",
        ".git/HEAD",
        ".git/worktrees/w/HEAD",
        "notes/config",
    ],
)
def test_the_gateway_checkout_keeps_ordinary_files_writable(gateway: Path, rel: str) -> None:
    assert not gc.git_control_write(str(gateway / rel))
    assert not is_sensitive_write_path(str(gateway / rel))


@_POSIX_ONLY
def test_a_symlink_into_git_is_refused_through_its_canonical_form(gateway: Path) -> None:
    link = gateway / "innocent.cfg"
    link.symlink_to(gateway / ".git" / "config")
    assert gc.git_control_write(str(link))
    assert is_sensitive_write_path(str(link))


@_POSIX_ONLY
def test_the_gate_creates_nothing(gateway: Path) -> None:
    before = sorted(p.name for p in (gateway / ".git").iterdir())
    gc.git_control_write(str(gateway / ".git" / "config"))
    assert sorted(p.name for p in (gateway / ".git").iterdir()) == before


def test_reads_stay_allowed(tmp_path: Path) -> None:
    from kiro_crew.security import is_sensitive_path

    assert not is_sensitive_path(str(tmp_path / ".git" / "config"))
