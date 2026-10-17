"""Every git spawn the registry pipeline makes carries the hooks/fsmonitor neutralizer.

A repository the browse/install path clones can ship a ``.githooks/`` tree, and a
``core.hooksPath`` in the operator's global git config would then make git run those
hooks host-side during ``clone``/``checkout``/``pull``. The defense is a pair of
``-c`` overrides (``core.hooksPath`` pointed at :data:`os.devnull` and
``core.fsmonitor=false``) spliced in right after ``git`` on every argv the pipeline
builds. ``_git_fetch_ref`` already does this for its own spawns; this module pins the
same invariant across the five sibling spawn sites that browse anonymously:
``_fetch_app_manifest`` (manifests), ``_fetch_git_blob`` (blob proxy), the index
clone in ``_fetch_external_registry_index`` (indexes), and the ``git pull`` /
``git clone`` paths of ``_git_clone_or_pull`` (checkout).

The check is positional: the neutralizer must sit among the leading ``-c`` option
pairs, BEFORE the subcommand token — a ``-c`` after the subcommand is not honored by
git. A negative control removes the splice from one site and confirms the assertion
goes red, so the test cannot pass vacuously.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps import routes
from kiro_crew.apps.registry_pipeline import checkout, indexes, manifests

_HOOKS_VALUE = f"core.hooksPath={os.devnull}"


class _FakeProc:
    """Stands in for the child returned by ``create_subprocess_limited``."""

    def __init__(self, returncode: int = 1, output: bytes = b"boom") -> None:
        # Above every supported platform's pid_max, so a cleanup path that signals
        # this pid reaches no live process (a sibling pytest-xdist worker included).
        self.pid = 99_999_999_999
        self.returncode = returncode
        self._output = output

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._output, self._output

    def kill(self) -> None:  # pragma: no cover - only the timeout path calls this
        return None


def _git_subcommand_index(argv: list[str]) -> int:
    """Index of the git subcommand token: skip ``git`` and every leading ``-c KEY`` pair."""
    i = 1
    while i < len(argv) and argv[i] == "-c":
        i += 2
    return i


def _assert_neutralized(argvs: list[list[str]]) -> None:
    """Every captured git argv must carry the hooks neutralizer before its subcommand."""
    git_argvs = [a for a in argvs if a and a[0] == "git"]
    assert git_argvs, f"expected at least one git spawn, captured: {argvs}"
    for argv in git_argvs:
        sub = _git_subcommand_index(argv)
        leading_c = argv[1:sub]
        assert _HOOKS_VALUE in leading_c, (
            f"git argv is missing {_HOOKS_VALUE!r} among its leading -c options "
            f"(subcommand at index {sub}): {argv}"
        )


def _install_capture(monkeypatch, module, sink: list[list[str]]) -> None:
    """Patch *module*'s sandbox + spawn seams so raw argv reaches *sink* unwrapped."""

    async def _wrap(argv: list[str], **kwargs: Any) -> tuple[list[str], None]:
        return list(argv), None

    async def _spawn(*argv: str, **kwargs: Any) -> _FakeProc:
        sink.append(list(argv))
        # ``git init`` / ``git remote add`` succeed so ``_git_fetch_ref`` advances
        # far enough to spawn its network ``fetch`` too; the network op then fails
        # closed so no call site reaches a disk read of a real checkout.
        sub = _git_subcommand_index(list(argv))
        tokens = list(argv)[sub : sub + 2]
        if argv and argv[0] == "git" and tokens[:1] in (["init"], ["remote"]):
            return _FakeProc(returncode=0, output=b"")
        return _FakeProc(returncode=1)

    monkeypatch.setattr(module, "wrap_argv_async", _wrap)
    monkeypatch.setattr(module, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(module, "create_subprocess_limited", _spawn)


async def _capture_fetch_ref(monkeypatch, tmp_path: Path) -> list[list[str]]:
    """Drive ``_git_fetch_branch`` (a ``_git_fetch_ref`` wrapper) on a fresh dest."""
    sink: list[list[str]] = []
    _install_capture(monkeypatch, checkout, sink)
    monkeypatch.setattr(checkout, "_git_transport_env", lambda *a, **k: {})

    async def _rmtree(path: str | Path) -> None:
        return None

    monkeypatch.setattr(checkout, "_rmtree_force_settled", _rmtree)
    dest = tmp_path / "fetch_ref_dest"
    result = await checkout._git_fetch_branch(
        "https://github.com/acme/demo-app.git",
        "main",
        dest,
        [],
        clone_env={},
        sandbox_mode="strict",
    )
    # init + remote add succeed (fake proc), fetch fails, so the ref path bails
    # after capturing init/remote/fetch — every one a git argv.
    assert result is not None and result["ok"] is False
    return sink


async def _capture_clone_or_pull_clone(monkeypatch, tmp_path: Path) -> list[list[str]]:
    """Drive the fresh-clone branch of ``_git_clone_or_pull`` (no ``.git`` present)."""
    sink: list[list[str]] = []
    _install_capture(monkeypatch, checkout, sink)
    monkeypatch.setattr(checkout, "_git_transport_env", lambda *a, **k: {})
    monkeypatch.setattr(checkout, "_context_clone_sandbox_mode", lambda url: "strict")

    monkeypatch.setattr(checkout, "is_clone_host_trusted", lambda url: True)

    async def _rmtree(path: str | Path) -> None:
        return None

    monkeypatch.setattr(checkout, "_rmtree_force_settled", _rmtree)
    dest = tmp_path / "clone_dest" / "demo-app"
    result = await checkout._git_clone_or_pull(
        "https://github.com/acme/demo-app.git", "main", dest, []
    )
    assert result is not None and result["ok"] is False
    return sink


async def _capture_clone_or_pull_pull(monkeypatch, tmp_path: Path) -> list[list[str]]:
    """Drive the fast-forward (``git pull``) branch of ``_git_clone_or_pull``."""
    sink: list[list[str]] = []
    _install_capture(monkeypatch, checkout, sink)
    monkeypatch.setattr(checkout, "_git_transport_env", lambda *a, **k: {})
    monkeypatch.setattr(checkout, "_context_clone_sandbox_mode", lambda url: "strict")
    monkeypatch.setattr(checkout, "is_clone_host_trusted", lambda url: True)
    url = "https://github.com/acme/demo-app.git"

    async def _origin(path: Path) -> str:
        return url

    monkeypatch.setattr(checkout, "_clone_origin_url", _origin)
    monkeypatch.setattr(checkout, "_read_clone_branch", lambda path: "main")

    async def _kill(proc: Any) -> None:
        return None

    monkeypatch.setattr(checkout, "_kill_process_group", _kill)
    dest = tmp_path / "pull_dest" / "demo-app"
    (dest / ".git").mkdir(parents=True)
    result = await checkout._git_clone_or_pull(url, "main", dest, [])
    assert result is not None and result["ok"] is False
    return sink


async def _capture_manifest(monkeypatch) -> list[list[str]]:
    """Drive the anonymous throwaway clone in ``_fetch_app_manifest``."""
    sink: list[list[str]] = []
    _install_capture(monkeypatch, manifests, sink)
    monkeypatch.setattr(manifests, "_git_transport_env", lambda *a, **k: {})
    monkeypatch.setattr(manifests, "is_clone_host_trusted", lambda url: True)
    # No local fast path and no cache hit, so it reaches the clone.
    monkeypatch.setattr(manifests, "_read_manifest_cache", lambda entry: None)
    result = await manifests._fetch_app_manifest("https://github.com/acme/demo-app.git", "main")
    assert result is None
    return sink


async def _capture_blob(monkeypatch, tmp_path: Path) -> list[list[str]]:
    """Drive the anonymous clone in ``_fetch_git_blob`` (blob proxy)."""
    sink: list[list[str]] = []
    _install_capture(monkeypatch, routes, sink)

    # ``_fetch_git_blob`` imports ``is_clone_host_trusted`` from the registry
    # facade inside the function, so patch it at the source module.
    from kiro_crew.apps import registry as registry_facade

    monkeypatch.setattr(registry_facade, "is_clone_host_trusted", lambda url: True)
    cache_path = tmp_path / "cache" / "logo.png"
    ok = await routes._fetch_git_blob(
        "https://github.com/acme/demo-app.git",
        "main",
        "assets/logo.png",
        cache_path,
        git_url="https://github.com/acme/demo-app.git",
    )
    assert ok is False
    return sink


async def _capture_index(monkeypatch) -> list[list[str]]:
    """Drive the anonymous shallow clone in ``_fetch_external_registry_index``."""
    sink: list[list[str]] = []
    _install_capture(monkeypatch, indexes, sink)
    monkeypatch.setattr(indexes, "_context_clone_sandbox_mode", lambda url: "strict")
    # Production cleanup runs: the function's own ``mkdtemp`` scratch dir is
    # removed by its ``finally``, so the test leaves no residue under TMPDIR.
    result = await indexes._fetch_external_registry_index(
        "https://github.com/acme/apps-index.git", "main"
    )
    assert result is None
    return sink


@pytest.mark.asyncio
async def test_every_registry_git_spawn_carries_the_hooks_neutralizer(monkeypatch, tmp_path):
    captures = {
        "_fetch_external_registry_index": await _capture_index(monkeypatch),
        "_git_fetch_ref": await _capture_fetch_ref(monkeypatch, tmp_path),
        "_git_clone_or_pull(clone)": await _capture_clone_or_pull_clone(monkeypatch, tmp_path),
        "_git_clone_or_pull(pull)": await _capture_clone_or_pull_pull(monkeypatch, tmp_path),
        "_fetch_app_manifest": await _capture_manifest(monkeypatch),
        "_fetch_git_blob": await _capture_blob(monkeypatch, tmp_path),
    }
    for site, argvs in captures.items():
        git_argvs = [a for a in argvs if a and a[0] == "git"]
        assert git_argvs, f"{site} captured no git spawn: {argvs}"
        _assert_neutralized(argvs)


@pytest.mark.asyncio
async def test_negative_control_removing_the_splice_makes_the_pull_site_red(monkeypatch, tmp_path):
    # Remove the neutralizer from the pull site's constant and confirm the same
    # invariant now fails — proving the assertion is load-bearing, not vacuous.
    with pytest.MonkeyPatch.context() as removed:
        removed.setattr(checkout, "_HOOKS_NEUTRALIZER_ARGV", ())
        argvs = await _capture_clone_or_pull_pull(removed, tmp_path)
    git_argvs = [a for a in argvs if a and a[0] == "git"]
    assert git_argvs, argvs
    with pytest.raises(AssertionError):
        _assert_neutralized(argvs)


async def _capture_fetch_branch_envs(
    monkeypatch, tmp_path: Path, **fetch_kwargs: Any
) -> list[tuple[list[str], dict[str, str]]]:
    """Drive ``_git_fetch_branch`` to success and return ``(argv, env)`` per spawn.

    The network env is what production builds from the operator's env: it must NOT
    carry the config-disabling pair, so the ``NETWORK`` marker tells the two apart.
    """
    seen: list[tuple[list[str], dict[str, str]]] = []

    async def _wrap(argv: list[str], **kwargs: Any) -> tuple[list[str], None]:
        return list(argv), None

    async def _spawn(*argv: str, **kwargs: Any) -> _FakeProc:
        seen.append((list(argv), dict(kwargs.get("env") or {})))
        return _FakeProc(returncode=0, output=b"")

    monkeypatch.setattr(checkout, "wrap_argv_async", _wrap)
    monkeypatch.setattr(checkout, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(checkout, "create_subprocess_limited", _spawn)
    monkeypatch.setattr(
        checkout, "_git_transport_env", lambda target, safe, env: {**env, "NETWORK": "1"}
    )

    async def _rmtree(path: str | Path) -> None:
        return None

    monkeypatch.setattr(checkout, "_rmtree_force_settled", _rmtree)
    result = await checkout._git_fetch_branch(
        "https://github.com/acme/demo-app.git",
        "main",
        tmp_path / "dest",
        [],
        clone_env={"HOME": str(tmp_path / "gateway-home")},
        sandbox_mode="strict",
        **fetch_kwargs,
    )
    assert result is None, result
    subcommands = [argv[_git_subcommand_index(argv)] for argv, _env in seen]
    assert "fetch" in subcommands and "checkout" in subcommands, subcommands
    for argv, env in seen:
        if env.get("NETWORK"):
            assert argv[_git_subcommand_index(argv)] == "fetch", argv
            assert "GIT_CONFIG_GLOBAL" not in env, "the network step keeps the operator's config"
            assert "GIT_CONFIG_NOSYSTEM" not in env, "the network step keeps the operator's config"
    return seen


@pytest.mark.asyncio
async def test_prewarm_local_git_steps_run_without_system_or_global_config(monkeypatch, tmp_path):
    """With ``mask_local_git_config=True`` -- what the store-art prewarm passes -- the
    init/checkout/branch steps of ``_git_fetch_ref`` run with system and global git
    config disabled, so a ``filter.<name>.smudge`` program the operator's config
    defines cannot be selected by a repository's ``.gitattributes`` and run at
    checkout; the network fetch keeps the operator's config for its credential
    helpers. Every step is spawned here (fake procs succeed), and the env of each is
    captured beside its argv."""
    seen = await _capture_fetch_branch_envs(monkeypatch, tmp_path, mask_local_git_config=True)
    local = [(argv, env) for argv, env in seen if not env.get("NETWORK")]
    assert local, seen
    for argv, env in local:
        sub = argv[_git_subcommand_index(argv)]
        assert env.get("GIT_CONFIG_NOSYSTEM") == "1", (sub, env)
        assert env.get("GIT_CONFIG_GLOBAL") == os.devnull, (sub, env)
        assert env.get("HOME") == str(tmp_path / "gateway-home"), "the rest of the env is untouched"


@pytest.mark.asyncio
async def test_install_local_git_steps_keep_the_operator_git_config(monkeypatch, tmp_path):
    """The DEFAULT -- every install path (``_git_fetch_commit`` for a pinned install,
    ``_git_clone_or_pull``'s branch path) -- does NOT mask system/global git config on
    the local steps: the same masking disables Git LFS, so an install would check out
    pointer files instead of content. The local steps run with ``clone_env`` exactly
    as given, while the hooks/fsmonitor neutralizer still rides on every argv."""
    seen = await _capture_fetch_branch_envs(monkeypatch, tmp_path)
    local = [(argv, env) for argv, env in seen if not env.get("NETWORK")]
    assert local, seen
    for argv, env in local:
        sub = argv[_git_subcommand_index(argv)]
        assert "GIT_CONFIG_NOSYSTEM" not in env, (sub, env)
        assert "GIT_CONFIG_GLOBAL" not in env, (sub, env)
        assert env == {"HOME": str(tmp_path / "gateway-home")}, (sub, env)
    _assert_neutralized([argv for argv, _env in seen])


@pytest.mark.asyncio
async def test_pinned_install_local_git_steps_keep_the_operator_git_config(monkeypatch, tmp_path):
    """``_git_fetch_commit`` has no masking flag at all: a pinned install's local
    steps run with ``clone_env`` unmasked, so LFS content is checked out."""
    seen: list[tuple[list[str], dict[str, str]]] = []

    async def _wrap(argv: list[str], **kwargs: Any) -> tuple[list[str], None]:
        return list(argv), None

    async def _spawn(*argv: str, **kwargs: Any) -> _FakeProc:
        seen.append((list(argv), dict(kwargs.get("env") or {})))
        return _FakeProc(returncode=0, output=b"")

    monkeypatch.setattr(checkout, "wrap_argv_async", _wrap)
    monkeypatch.setattr(checkout, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(checkout, "create_subprocess_limited", _spawn)
    monkeypatch.setattr(
        checkout, "_git_transport_env", lambda target, safe, env: {**env, "NETWORK": "1"}
    )
    sha = "a" * 40
    monkeypatch.setattr(checkout, "_resolved_clone_commit", lambda dest: sha)

    async def _rmtree(path: str | Path) -> None:
        return None

    monkeypatch.setattr(checkout, "_rmtree_force_settled", _rmtree)
    result = await checkout._git_fetch_commit(
        "https://github.com/acme/demo-app.git",
        sha,
        tmp_path / "dest",
        [],
        clone_env={"HOME": str(tmp_path / "gateway-home")},
        sandbox_mode="strict",
    )
    assert result is None, result
    local = [(argv, env) for argv, env in seen if not env.get("NETWORK")]
    assert any(argv[_git_subcommand_index(argv)] == "checkout" for argv, _env in local), seen
    for argv, env in local:
        assert env == {"HOME": str(tmp_path / "gateway-home")}, (argv, env)
    _assert_neutralized([argv for argv, _env in seen])
