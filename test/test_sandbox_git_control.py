"""A sandboxed agent cannot rewrite the git control files of the gateway's own checkout.

The gateway runs ``git`` in its install checkout outside any sandbox, and git executes
what that checkout's ``config``, ``hooks/`` and attributes name. These cases pin the
three layers that keep a sandboxed agent from choosing that code:

* the host gather (:mod:`kiro_crew.sandbox_git_control`) names the right seals and
  seal parents for a plain checkout, a linked worktree, includes and ``core.hooksPath``;
* the planner hands them to both backends, and the launcher seals after it pins;
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
from kiro_crew.sandbox_workspace import _literal_ancestor_guards, _resolution_entries
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


def _held(layout: gc.GitControlLayout) -> tuple[str, ...]:
    """Every entry guarded against rename for *layout*, derived as ``sandbox_argv`` does."""
    entries = [
        entry
        for name in layout.names
        for entry in (os.path.dirname(name), *_resolution_entries(name))
    ]
    return _literal_ancestor_guards(tuple(entries))


# --------------------------------------------------------------------------- #
# The host gather.
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
def test_a_plain_checkout_guards_its_git_dir_and_seals_the_control_files(
    repo: Path,
) -> None:
    layout = gc.git_control_layout_for((str(repo),))
    gitdir = str(repo / ".git")
    assert gitdir in _held(layout)
    assert str(repo) in _held(layout)
    for name in ("config", "hooks", "info"):
        assert os.path.join(gitdir, name) in layout.readonly
    # Objects, refs, the index and HEAD stay writable: commits must still work.
    for name in ("objects", "refs", "index", "HEAD", "logs"):
        assert os.path.join(gitdir, name) not in layout.readonly


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
    # Every directory from the common dir down to the worktree's own is guarded, and so
    # is the main checkout above the common dir.
    guarded = _held(layout)
    for parent in (common, common / "worktrees", own, repo, linked):
        assert str(parent) in guarded
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
    # The include's directory is guarded, so it cannot be renamed and rebuilt.
    assert str(tmp_path.resolve()) in _held(layout)


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
def test_an_include_of_100k_entries_refuses_the_launch(repo: Path) -> None:
    from kiro_crew.sandbox_seals import SandboxCeilingUnsealable

    flood = repo / ".git" / "flood.cfg"
    entries = "".join(f"\tpath = missing-{i}\n" for i in range(100_000))
    flood.write_text("[include]\n" + entries, encoding="utf-8")
    assert flood.stat().st_size > gc._MAX_CONFIG_BYTES
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write("[include]\n\tpath = flood.cfg\n")
    with pytest.raises(SandboxCeilingUnsealable, match="flood.cfg"):
        gc.git_control_layout_for((str(repo),))


@_POSIX_ONLY
def test_too_many_include_targets_refuse_the_launch(repo: Path) -> None:
    from kiro_crew.sandbox_seals import SandboxCeilingUnsealable

    many = "".join(f"\tpath = missing-{i}.cfg\n" for i in range(gc._MAX_CONFIG_TARGETS + 1))
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write("[include]\n" + many)
    with pytest.raises(SandboxCeilingUnsealable):
        gc.git_control_layout_for((str(repo),))


@_POSIX_ONLY
def test_an_include_past_the_file_limit_refuses_the_launch(repo: Path) -> None:
    from kiro_crew.sandbox_seals import SandboxCeilingUnsealable

    git_dir = repo / ".git"
    count = gc._MAX_CONFIG_FILES
    for i in range(count):
        (git_dir / f"sib-{i}.cfg").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    (git_dir / f"sib-{count - 1}.cfg").write_text(
        "[include]\n\tpath = deep.cfg\n", encoding="utf-8"
    )
    (git_dir / "deep.cfg").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    siblings = "".join(f"\tpath = sib-{i}.cfg\n" for i in range(count))
    with open(git_dir / "config", "a", encoding="utf-8") as fh:
        fh.write("[include]\n" + siblings)
    with pytest.raises(SandboxCeilingUnsealable):
        gc.git_control_layout_for((str(repo),))


@_POSIX_ONLY
def test_100k_repeats_of_one_missing_include_are_listed_once(repo: Path) -> None:
    flood = repo / ".git" / "flood.cfg"
    flood.write_text("[include]\n" + "\tpath = x\n" * 100_000, encoding="utf-8")
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write("[include]\n\tpath = flood.cfg\n")
    layout = gc.git_control_layout_for((str(repo),))
    assert layout.readonly.count(str(repo / ".git" / "x")) == 1


def _killed(*_args, **_kwargs) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], -9, b"", b"")


def _stopped(*_args, **_kwargs) -> subprocess.CompletedProcess:
    raise subprocess.TimeoutExpired("git", gc._GIT_CONFIG_TIMEOUT_S)


def _unstartable(*_args, **_kwargs) -> subprocess.CompletedProcess:
    raise BlockingIOError("fork: resource temporarily unavailable")


@_POSIX_ONLY
@pytest.mark.parametrize("answer", [_killed, _stopped, _unstartable])
def test_a_git_that_does_not_finish_listing_refuses_the_launch(
    repo: Path, monkeypatch: pytest.MonkeyPatch, answer
) -> None:
    """A sandboxed agent can stop or kill the gateway's git; that must not unseal an include."""
    from kiro_crew.sandbox_seals import SandboxCeilingUnsealable

    monkeypatch.setattr(subprocess, "run", answer)
    with pytest.raises(SandboxCeilingUnsealable, match="could not be listed by git"):
        gc.git_control_layout_for((str(repo),))


def test_the_spawn_refuses_rather_than_seal_nothing_on_overflow(monkeypatch) -> None:
    from kiro_crew.sandbox_seals import SandboxCeilingUnsealable

    def overflow(_roots):
        raise SandboxCeilingUnsealable("too many includes")

    monkeypatch.setattr(gc, "git_control_layout_for", overflow)
    with pytest.raises(SandboxCeilingUnsealable):
        gc.resolved_git_control_layout()


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
def test_a_symlinked_config_guards_its_target_parents(repo: Path, tmp_path: Path) -> None:
    shared = (tmp_path / "shared").resolve()
    shared.mkdir()
    config = repo / ".git" / "config"
    target = shared / "config"
    target.write_bytes(config.read_bytes())
    config.unlink()
    config.symlink_to(target)
    layout = gc.git_control_layout_for((str(repo),))
    assert str(target) in layout.readonly
    # The target's directory cannot be renamed and rebuilt under the link, and the
    # link itself is held too, so it cannot be swapped for a file of the agent's.
    assert str(shared) in _held(layout)
    assert str(config) in _held(layout)


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

_SEALS = ("/w/r/.git/config", "/w/r/.git/hooks", "/w/r/.git/info")
#: What ``sandbox_argv`` derives from the seals' parents: each one and every directory
#: above it, handed over in the field the project MCP files' parents use.
_GUARDS = ("/w/r/.git", "/w/r", "/w")


def _host(backend: str) -> PlanHost:
    return PlanHost(home="/home/u", git_control_readonly=_SEALS, project_ancestor_guards=_GUARDS)


@pytest.mark.parametrize("tier", ["standard", "cc", "strict"])
def test_the_namespace_launcher_receives_pins_and_seals(tier: str) -> None:
    plan = plan_confinement(SandboxRequest(tier=tier), _host(BACKEND_NAMESPACE))
    payload = namespace_payload(plan)
    for guard in _GUARDS:
        assert guard in payload["parent_pins"]
    for target in _SEALS:
        assert target in payload["readonly_dirs"]


@pytest.mark.parametrize("tier", ["standard", "cc", "strict"])
def test_the_seatbelt_profile_denies_writes_and_renames(tier: str) -> None:
    from kiro_crew.sandbox_seatbelt import render_seatbelt_profile

    plan = plan_confinement(
        SandboxRequest(tier=tier, backend=BACKEND_SEATBELT), _host(BACKEND_SEATBELT)
    )
    profile = render_seatbelt_profile(plan)
    for target in _SEALS:
        assert f'(deny file-write* (subpath "{target}"))' in profile
        assert f'(deny file-write* (literal "{target}"))' in profile
    for guard in _GUARDS:
        assert f'(deny file-write* (literal "{guard}"))' in profile
    # A guard is a literal: the directory's children stay writable.
    assert '(deny file-write* (subpath "/w/r/.git"))' not in profile


def test_a_writable_carveout_cannot_reopen_a_seal() -> None:
    host = PlanHost(
        home="/home/u",
        git_control_readonly=_SEALS,
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


def _exception_host(backend: str, exception: str) -> PlanHost:
    """:func:`_host` with ``/w`` read-only by the operator and *exception* writable."""
    from dataclasses import replace

    def probe(path: str) -> sandbox_plan.CarveoutProbe:
        return sandbox_plan.CarveoutProbe(raw=path, lexical=path, canonical=path, is_dir=True)

    return replace(
        _host(backend),
        operator_readonly_probes=(probe("/w"),),
        operator_exception_probes=(probe(exception),),
    )


#: An operator's write exception around the checkout ``/w/r``: the checkout itself, its
#: git directory, a sealed directory, a directory inside one, and an ordinary subfolder.
_OPERATOR_EXCEPTIONS = ["/w/r", "/w/r/.git", "/w/r/.git/hooks", "/w/r/.git/hooks/x", "/w/r/src"]


@_POSIX_ONLY
@pytest.mark.parametrize("exception", _OPERATOR_EXCEPTIONS)
def test_an_operator_exception_is_kept_and_drops_no_namespace_seal(exception: str) -> None:
    host = _exception_host(BACKEND_NAMESPACE, exception)
    plan = plan_confinement(SandboxRequest(tier="standard"), host)
    assert plan.operator_writable == (exception,)
    assert [r for r in plan.refusals if r.kind == "operator"] == []
    payload = namespace_payload(plan)
    for guard in _GUARDS:
        assert guard in payload["parent_pins"]
    for target in _SEALS:
        assert target in payload["readonly_dirs"]


@_POSIX_ONLY
@pytest.mark.parametrize("exception", _OPERATOR_EXCEPTIONS)
def test_an_operator_exception_is_kept_and_seatbelt_denies_after_it(exception: str) -> None:
    from kiro_crew.sandbox_seatbelt import render_seatbelt_profile

    host = _exception_host(BACKEND_SEATBELT, exception)
    plan = plan_confinement(SandboxRequest(tier="standard", backend=BACKEND_SEATBELT), host)
    assert plan.operator_writable == (exception,)
    assert [r for r in plan.refusals if r.kind == "operator"] == []
    profile = render_seatbelt_profile(plan)
    # Last match wins between an allow and a deny, so each seal must follow the allow.
    allowed = profile.index(f'(allow file-write* (subpath "{exception}"))')
    for target in _SEALS:
        assert profile.index(f'(deny file-write* (subpath "{target}"))') > allowed
        assert profile.index(f'(deny file-write* (literal "{target}"))') > allowed
    for guard in _GUARDS:
        assert profile.rindex(f'(deny file-write* (literal "{guard}"))') > allowed


@_POSIX_ONLY
def test_a_plan_built_in_the_suite_never_gathers_the_source_checkout(monkeypatch) -> None:
    from kiro_crew import sandbox as sandbox_mod

    source = Path(gc.__file__).resolve().parents[2]
    dotgit = source / ".git"
    before = sorted(p.name for p in dotgit.iterdir()) if dotgit.is_dir() else None
    monkeypatch.setattr(sandbox_mod, "_caller_can_rename", lambda path, uid: True)
    plan = sandbox_mod._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "strict")
    assert gc.gateway_checkout_roots() == ()
    assert not any(path.startswith(str(dotgit)) for path in plan.readonly)
    after = sorted(p.name for p in dotgit.iterdir()) if dotgit.is_dir() else None
    assert after == before


@_POSIX_ONLY
@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
def test_the_live_plan_guards_the_checkout_and_its_git_dir(
    repo: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """The host gather feeds the seals' parents to the guard every other seal's get."""
    from kiro_crew import sandbox as sandbox_mod

    hooks, real_hooks = repo / ".git" / "hooks", repo.parent / "shared-hooks"
    hooks.rename(real_hooks)
    hooks.symlink_to(real_hooks, target_is_directory=True)
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(repo))
    plan = sandbox_mod._spawn_plan(backend, "standard")
    assert str(repo / ".git" / "config") in plan.readonly
    assert str(real_hooks) in plan.readonly
    # ``hooks`` is a link: it is held as itself, so it cannot be swapped for a directory.
    for held in (repo / ".git", repo, repo.parent, hooks):
        assert str(held) in plan.runtime_ancestor_guards


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the namespace launcher is Linux-only"
)
def test_the_launcher_seals_after_the_operator_layer_and_the_parent_pins() -> None:
    import inspect

    from kiro_crew import sandbox_launcher_program as program

    order = inspect.getsource(program.place_masks)
    # The pins copy the operator's layer, its writable exceptions included, and every
    # crew seal lands after both, so no exception can re-open one.
    assert order.index("seal_operator_readonly(launch)") < order.index("pin_seal_parents(launch)")
    assert order.index("pin_seal_parents(launch)") < order.index("seal_readonly(launch)")
    # The working directory is entered by name after the mounts, so a relative path
    # goes through the seals.
    child = inspect.getsource(program.run_child)
    assert child.index("place_masks(launch)") < child.index("enter_exec_cwd(launch)")
    assert child.index("enter_exec_cwd(launch)") < child.index("exec_agent(launch, argv)")


# --------------------------------------------------------------------------- #
# The file-tool write gate.
# --------------------------------------------------------------------------- #


@pytest.fixture
def gateway(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """*repo* as the gateway's only checkout."""
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(repo))
    monkeypatch.setattr(gc, "_source_checkout_root", lambda: None)
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


def test_nested_module_paths_are_judged_in_one_pass() -> None:
    """Retrying every later suffix at each ``modules`` segment costs 2**k work for k of them."""
    nested = ["modules", "x"] * 2000
    assert not gc._gitdir_rest_is_control([*nested, "objects", "x"])
    assert not gc._gitdir_rest_is_control([*["worktrees", "w"] * 2000, "objects", "x"])
    assert gc._gitdir_rest_is_control([*nested, "config"])
    assert gc._gitdir_rest_is_control([*nested, "hooks", "pre-commit"])
    assert gc._gitdir_rest_is_control(["modules", "a", "b", "worktrees", "w", "config"])
    assert not gc._gitdir_rest_is_control(["modules", "config"])
    assert not gc._gitdir_rest_is_control([])


@_POSIX_ONLY
def test_the_gate_creates_nothing(gateway: Path) -> None:
    before = sorted(p.name for p in (gateway / ".git").iterdir())
    gc.git_control_write(str(gateway / ".git" / "config"))
    assert sorted(p.name for p in (gateway / ".git").iterdir()) == before


def test_reads_stay_allowed(tmp_path: Path) -> None:
    from kiro_crew.security import is_sensitive_path

    assert not is_sensitive_path(str(tmp_path / ".git" / "config"))
