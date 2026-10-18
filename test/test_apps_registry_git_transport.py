"""App registry git transport hardening.

The one-shot credential reaches only the network fetch: hooks, askpass, checkout filters,
remote helpers and ambiguous transport shapes are refused or neutralized. A failed or
cancelled fetch or clone leaves no partial checkout behind, including Windows read-only
pack files. The module-wide autouse fixtures come from ``apps_registry_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from unittest.mock import AsyncMock

import pytest
from apps_registry_helpers import _catalog_absent  # noqa: F401 - fixture
from apps_registry_helpers import _explicit_registry_execution_admission  # noqa: F401 - fixture
from apps_registry_helpers import unsandboxed_spawn  # noqa: F401 - fixture

from kiro_crew import platform_compat
from kiro_crew.apps import registry
from kiro_crew.testing.wait import await_until


def _command_git_config(env: dict[str, object]) -> list[tuple[object, object]]:
    """Ordered command-scope Git config pairs captured at a spawn boundary."""
    count = int(env["GIT_CONFIG_COUNT"])
    return [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)]


def _assert_credential_transport_hardening(
    env: dict[str, object], raw_url: str, public_url: str
) -> None:
    pairs = _command_git_config(env)
    assert (f"url.{raw_url}.insteadOf", public_url) in pairs
    assert pairs[-4:] == [
        ("core.fsmonitor", "false"),
        ("credential.helper", ""),
        ("core.askPass", ""),
        ("core.hooksPath", os.devnull),
    ]


@pytest.mark.asyncio
async def test_clone_stream_never_emits_embedded_https_credentials(monkeypatch, tmp_path):
    secret = "StreamingCloneSecret"
    raw_url = f"https://user:{secret}@example.com/o/demo.git"
    public_url = "https://example.com/o/demo.git"
    queue: asyncio.Queue[str] = asyncio.Queue()
    log_lines = registry.StreamingLogLines(queue)
    spawned: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(
        registry,
        "minimal_env",
        lambda **extra: {
            "BASE": "1",
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/tmp/repository-controlled-hooks",
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_VALUE_1": "/tmp/repository-controlled-fsmonitor",
            "GIT_CONFIG_KEY_2": "credential.helper",
            "GIT_CONFIG_VALUE_2": "!malicious-helper",
            "GIT_CONFIG_KEY_3": "filter.leak.process",
            "GIT_CONFIG_VALUE_3": "malicious-filter",
            **extra,
        },
    )

    class _Proc:
        def __init__(self, returncode=0, output=b""):
            self.returncode = returncode
            self.output = output

        pid = 4242

        async def communicate(self):
            return self.output, b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append((list(argv), kwargs))
        if "init" in argv:
            (tmp_path / "demo" / ".git").mkdir(parents=True)
        if "fetch" in argv:
            return _Proc(1, f"fatal: unable to access {raw_url}".encode())
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)

    result = await registry._git_clone_or_pull(
        public_url,
        "main",
        tmp_path / "demo",
        log_lines,
        credential_target=raw_url,
    )
    streamed = []
    while not queue.empty():
        streamed.append(queue.get_nowait())
    visible = "\n".join([*log_lines, *streamed, str(result)])

    assert secret not in visible
    assert raw_url not in visible
    assert public_url in visible
    assert "git transport output redacted (credentialed remote)" in visible
    # The network mapping may carry the raw credential in process memory, but
    # only fetch receives it; init, origin setup and checkout use the base env.
    assert spawned
    fetch_argv, spawn_kwargs = next(call for call in spawned if "fetch" in call[0])
    assert public_url in fetch_argv
    assert secret not in "\n".join(fetch_argv)
    transport_env = spawn_kwargs["env"]
    assert isinstance(transport_env, dict)
    pairs = _command_git_config(transport_env)
    assert pairs[:4] == [
        ("core.hooksPath", "/tmp/repository-controlled-hooks"),
        ("core.fsmonitor", "/tmp/repository-controlled-fsmonitor"),
        ("credential.helper", "!malicious-helper"),
        ("filter.leak.process", "malicious-filter"),
    ]
    _assert_credential_transport_hardening(transport_env, raw_url, public_url)


@pytest.mark.asyncio
async def test_credentialed_pull_appends_exec_neutralizers_after_inherited_config(
    monkeypatch, tmp_path
):
    secret = "PullTransportSecret"
    raw_url = f"https://user:{secret}@example.com/o/demo.git"
    public_url = "https://example.com/o/demo.git"
    dest = tmp_path / "demo"
    (dest / ".git").mkdir(parents=True)
    spawned: list[tuple[list[str], dict[str, object]]] = []
    log_lines: list[str] = []

    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
    monkeypatch.setattr(registry, "_read_clone_branch", lambda clone_dir: "main")
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(
        registry,
        "minimal_env",
        lambda **extra: {
            "BASE": "1",
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/tmp/repository-controlled-hooks",
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_VALUE_1": "/tmp/repository-controlled-fsmonitor",
            "GIT_CONFIG_KEY_2": "credential.helper",
            "GIT_CONFIG_VALUE_2": "!malicious-helper",
            "GIT_CONFIG_KEY_3": "filter.leak.process",
            "GIT_CONFIG_VALUE_3": "malicious-filter",
            **extra,
        },
    )

    async def _fake_origin(path):
        return public_url

    monkeypatch.setattr(registry, "_clone_origin_url", _fake_origin)

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"Already up to date.", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append((list(argv), kwargs))
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)

    result = await registry._git_clone_or_pull(
        public_url,
        "main",
        dest,
        log_lines,
        credential_target=raw_url,
    )

    assert result is None
    assert len(spawned) == 5
    assert not any(argv[:2] == ["git", "pull"] for argv, _ in spawned)
    fetch_argv, spawn_kwargs = next(call for call in spawned if "fetch" in call[0])
    assert public_url in fetch_argv
    assert "--no-auto-maintenance" in fetch_argv
    assert secret not in repr(fetch_argv)
    assert secret not in "\n".join(log_lines)
    transport_env = spawn_kwargs["env"]
    assert isinstance(transport_env, dict)
    assert _command_git_config(transport_env)[:4] == [
        ("core.hooksPath", "/tmp/repository-controlled-hooks"),
        ("core.fsmonitor", "/tmp/repository-controlled-fsmonitor"),
        ("credential.helper", "!malicious-helper"),
        ("filter.leak.process", "malicious-filter"),
    ]
    _assert_credential_transport_hardening(transport_env, raw_url, public_url)
    for argv, kwargs in spawned:
        if "fetch" not in argv:
            # The local steps of an INSTALL carry the inherited command config
            # untouched and are NOT masked (``mask_local_git_config`` is the
            # prewarm's flag): masking would disable Git LFS for the installed tree.
            assert kwargs["env"] == {
                "BASE": "1",
                "GIT_CONFIG_COUNT": "4",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": "/tmp/repository-controlled-hooks",
                "GIT_CONFIG_KEY_1": "core.fsmonitor",
                "GIT_CONFIG_VALUE_1": "/tmp/repository-controlled-fsmonitor",
                "GIT_CONFIG_KEY_2": "credential.helper",
                "GIT_CONFIG_VALUE_2": "!malicious-helper",
                "GIT_CONFIG_KEY_3": "filter.leak.process",
                "GIT_CONFIG_VALUE_3": "malicious-filter",
            }


@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="real Git hook inheritance and executable-bit semantics are POSIX-only",
)
def test_credential_transport_env_prevents_checkout_and_merge_hooks_from_seeing_secret(
    tmp_path,
):
    """A Git child carrying the one-shot credential must not run hooks.

    The control commands prove both hooks are executable and receive the raw
    command-scope rewrite. The same checkout/merge with the production env must
    leave no marker, pinning the behavior rather than only the env's shape.
    """

    secret = "HookInheritanceSecret"
    raw_url = f"https://user:{secret}@example.com/o/demo.git"
    public_url = "https://example.com/o/demo.git"
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    hooks = tmp_path / "hooks"
    checkout_marker = tmp_path / "post-checkout-ran"
    merge_marker = tmp_path / "post-merge-ran"
    repo.mkdir()
    home.mkdir()
    hooks.mkdir()

    fixture_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Registry Test",
        "GIT_AUTHOR_EMAIL": "registry@example.invalid",
        "GIT_COMMITTER_NAME": "Registry Test",
        "GIT_COMMITTER_EMAIL": "registry@example.invalid",
    }
    commands: list[list[str]] = []

    def _git(*args: str, env: dict[str, str] | None = None) -> None:
        argv = ["git", *args]
        commands.append(argv)
        try:
            completed = subprocess.run(
                argv,
                cwd=repo,
                env=env or fixture_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                timeout=15,
                check=False,
            )
        except FileNotFoundError:
            pytest.skip("git is unavailable")
        assert completed.returncode == 0, completed.stderr

    _git("init", "--quiet", "--template=")
    _git("checkout", "--quiet", "-b", "main")
    (repo / "payload.txt").write_text("base\n", encoding="utf-8")
    _git("add", "payload.txt")
    _git("commit", "--quiet", "--no-verify", "-m", "base")
    _git("checkout", "--quiet", "-b", "topic")
    (repo / "payload.txt").write_text("topic\n", encoding="utf-8")
    _git("commit", "--quiet", "--no-verify", "-am", "topic")
    _git("checkout", "--quiet", "main")

    # The vulnerable control env is the former shape: inherited hooksPath plus
    # the raw insteadOf mapping, without the fixed neutralizers.
    inherited = {
        **fixture_env,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(hooks),
    }
    protected = registry._git_transport_env(raw_url, public_url, inherited)
    protected_count = int(protected["GIT_CONFIG_COUNT"])
    vulnerable_count = protected_count - 4
    vulnerable = {
        key: value
        for key, value in protected.items()
        if not (key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"))
        or int(key.rsplit("_", 1)[1]) < vulnerable_count
    }
    vulnerable["GIT_CONFIG_COUNT"] = str(vulnerable_count)
    raw_mapping_index = vulnerable_count - 1

    for hook_name, marker in (
        ("post-checkout", checkout_marker),
        ("post-merge", merge_marker),
    ):
        hook = hooks / hook_name
        hook_body = "#!/bin/sh\n"
        hook_body += f'printf "%s" "$GIT_CONFIG_KEY_{raw_mapping_index}" > "{marker}"\n'
        hook.write_text(
            hook_body,
            encoding="utf-8",
        )
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

    # Prove post-checkout is live and can read the embedded credential, then
    # repeat under the hardened environment.
    _git("checkout", "--quiet", "topic", env=vulnerable)
    assert secret in checkout_marker.read_text(encoding="utf-8")
    checkout_marker.unlink()
    _git("checkout", "--quiet", "main", env=protected)
    _git("checkout", "--quiet", "topic", env=protected)
    assert not checkout_marker.exists()
    _git("checkout", "--quiet", "main", env=protected)

    # The same control/protected pair for the hook that `git pull` runs after a
    # successful merge.
    _git("merge", "--quiet", "--ff-only", "topic", env=vulnerable)
    assert secret in merge_marker.read_text(encoding="utf-8")
    _git("reset", "--quiet", "--hard", "HEAD^", env=fixture_env)
    merge_marker.unlink()
    _git("merge", "--quiet", "--ff-only", "topic", env=protected)
    assert not merge_marker.exists()

    assert secret not in repr(commands)


def test_credential_transport_env_prevents_askpass_from_seeing_secret(tmp_path):
    """The protected transport disables a real inherited askpass command."""

    secret = "AskPassInheritanceSecret"
    raw_url = f"https://user:{secret}@example.invalid/o/demo.git"
    public_url = "https://example.invalid/o/demo.git"
    marker = tmp_path / "askpass-marker"
    askpass = tmp_path / "askpass"
    # The marker is named ABSOLUTELY, like the hook markers above: git runs an
    # askpass helper from the repository top level when it discovers one, and a
    # `tmp_path` under a checkout (a developer's `TMPDIR=./tmp`) would land a
    # cwd-relative marker -- carrying the secret -- at that checkout's root.
    askpass.write_text(
        "#!/bin/sh\n" f'printf "%s" "$GIT_CONFIG_KEY_1" > "{marker}"\n' 'printf "supplied\\n"\n',
        encoding="utf-8",
    )
    askpass.chmod(askpass.stat().st_mode | stat.S_IXUSR)

    inherited = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.askPass",
        "GIT_CONFIG_VALUE_0": str(askpass),
    }
    protected = registry._git_transport_env(raw_url, public_url, inherited)
    protected_count = int(protected["GIT_CONFIG_COUNT"])
    vulnerable_count = protected_count - 4
    vulnerable = {
        key: value
        for key, value in protected.items()
        if not (key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"))
        or int(key.rsplit("_", 1)[1]) < vulnerable_count
    }
    vulnerable["GIT_CONFIG_COUNT"] = str(vulnerable_count)

    def _credential_fill(env):
        try:
            return subprocess.run(
                ["git", "credential", "fill"],
                cwd=tmp_path,
                env=env,
                input="protocol=https\nhost=example.invalid\nusername=user\n\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                timeout=15,
                check=False,
            )
        except FileNotFoundError:
            pytest.skip("git is unavailable")

    control = _credential_fill(vulnerable)
    assert control.returncode == 0, control.stderr
    assert secret in marker.read_text(encoding="utf-8")
    marker.unlink()

    hardened = _credential_fill(protected)
    assert hardened.returncode != 0
    assert not marker.exists()


def test_checkout_filter_sees_secret_only_when_given_network_environment(tmp_path):
    """A real smudge filter proves why fetch and checkout need separate envs."""
    secret = "FilterInheritanceSecret"
    raw_url = f"https://user:{secret}@example.invalid/o/demo.git"
    public_url = "https://example.invalid/o/demo.git"
    repo = tmp_path / "repo"
    marker = tmp_path / "filter-marker"
    filter_script = tmp_path / "filter.py"
    repo.mkdir()
    filter_script.write_text(
        "import os, pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text("
        "os.environ.get('GIT_CONFIG_KEY_1', ''), encoding='utf-8')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    filter_command = f'"{sys.executable}" "{filter_script}"'
    fixture_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Registry Test",
        "GIT_AUTHOR_EMAIL": "registry@example.invalid",
        "GIT_COMMITTER_NAME": "Registry Test",
        "GIT_COMMITTER_EMAIL": "registry@example.invalid",
    }

    def _git(*args, env):
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=repo,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                timeout=15,
                check=False,
            )
        except FileNotFoundError:
            pytest.skip("git is unavailable")
        assert completed.returncode == 0, completed.stderr

    _git("init", "--quiet", "--template=", env=fixture_env)
    (repo / ".gitattributes").write_text("payload.txt filter=leak\n", encoding="utf-8")
    (repo / "payload.txt").write_text("payload\n", encoding="utf-8")
    _git("add", ".gitattributes", "payload.txt", env=fixture_env)
    _git("commit", "--quiet", "--no-verify", "-m", "base", env=fixture_env)

    checkout_env = {
        **fixture_env,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "filter.leak.smudge",
        "GIT_CONFIG_VALUE_0": filter_command,
    }
    network_env = registry._git_transport_env(raw_url, public_url, checkout_env)

    # Vulnerable combined clone/pull control: the fetched tree chooses the filter,
    # and the filter inherits the raw URL rewrite from the network subprocess.
    (repo / "payload.txt").unlink()
    _git("checkout", "--", "payload.txt", env=network_env)
    assert secret in marker.read_text(encoding="utf-8")

    # Production split: checkout still honours the configured filter, but receives
    # only the base environment, so there is no credential to inherit.
    marker.unlink()
    (repo / "payload.txt").unlink()
    _git("checkout", "--", "payload.txt", env=checkout_env)
    assert marker.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("raw_url", "reason"),
    [
        (
            "https://example.com/o/demo.git?repo=A&access_token=secret-a",
            "query or fragment",
        ),
        (
            "https://example.com/o/demo.git?repo=B&access_token=secret-b",
            "query or fragment",
        ),
        ("ssh://deploy@example.com/o/demo.git#private-ref", "query or fragment"),
        (
            "deploy:password@example.invalid:Owner/Repo.git",
            "ambiguous Git transport identity",
        ),
        (
            "ssh://deploy:password@example.invalid/Owner/Repo.git",
            "ambiguous Git transport identity",
        ),
        (
            "git+ssh://deploy:password@example.invalid/Owner/Repo.git",
            "ambiguous Git transport identity",
        ),
    ],
)
def test_git_transport_rejects_unsupported_identity_split(raw_url, reason):
    safe_url = registry._strip_git_target_userinfo(raw_url)

    with pytest.raises(ValueError, match=reason):
        registry._git_transport_env(raw_url, safe_url, {"BASE": "1"})


@pytest.mark.parametrize("scheme", ["ftp", "git+https", "custom"])
def test_git_transport_rejects_credentialed_remote_helper_schemes(scheme):
    raw_url = f"{scheme}://user:secret@example.invalid/o/demo.git"
    safe_url = f"{scheme}://example.invalid/o/demo.git"

    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        registry._git_transport_env(raw_url, safe_url, {"BASE": "1"})


@pytest.mark.asyncio
async def test_branch_fetch_rejects_credentialed_remote_helper_before_spawn(monkeypatch, tmp_path):
    async def _must_not_spawn(*args, **kwargs):
        raise AssertionError("unsupported credential transport must fail before git")

    monkeypatch.setattr(registry, "create_subprocess_limited", _must_not_spawn)
    dest = tmp_path / "checkout"
    result = await registry._git_fetch_branch(
        "git+https://example.invalid/o/demo.git",
        "main",
        dest,
        [],
        credential_target="git+https://user:secret@example.invalid/o/demo.git",
        clone_env={"BASE": "1"},
        sandbox_mode="strict",
    )

    assert result is not None and result["ok"] is False
    assert "HTTP(S)" in result["error"]
    assert not dest.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "deploy:password@example.invalid:Owner/Repo.git",
        "ssh://deploy:password@example.invalid/Owner/Repo.git",
    ],
)
async def test_clone_build_rejects_ambiguous_transport_before_git_or_filesystem(monkeypatch, raw):
    async def _must_not_clone(*args, **kwargs):
        raise AssertionError("ambiguous Git target must fail before git")

    monkeypatch.setattr(registry, "_git_clone_or_pull", _must_not_clone)
    result = await registry._clone_build_app(raw, "demoapp", [])

    assert result["ok"] is False
    assert "ambiguous Git transport" in result["error"]
    assert raw not in result["error"]


@pytest.mark.asyncio
async def test_pinned_fetch_isolates_credentials_to_network_transport(monkeypatch, tmp_path):
    """Only the fetch subprocess receives the command-scoped auth mapping.

    The generic sandbox boundary and the three local git operations see only
    the credential-free repository identity and base environment.
    """
    secret = "PinnedFetchSecret"
    raw_url = f"https://user:{secret}@example.com/o/demo.git"
    public_url = "https://example.com/o/demo.git"
    dest = tmp_path / "demo"
    commit = "a" * 40
    spawned: list[tuple[list[str], dict[str, object]]] = []
    wrapped: list[list[str]] = []

    def _fake_wrap(cmd, mode="", **_kwargs):
        wrapped.append(list(cmd))
        return cmd, None

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append((list(argv), kwargs))
        if "init" in argv:
            (dest / ".git").mkdir(parents=True)
        return _Proc()

    monkeypatch.setattr(registry, "wrap_argv", _fake_wrap)
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: commit)

    result = await registry._git_fetch_commit(
        public_url,
        commit,
        dest,
        [],
        credential_target=raw_url,
        clone_env={"BASE": "1"},
        sandbox_mode="strict",
    )

    assert result is None
    assert len(wrapped) == len(spawned) == 4
    assert all(secret not in "\n".join(argv) for argv in wrapped)
    assert all(secret not in "\n".join(argv) for argv, _ in spawned)
    # Every spawn carries the ``-c`` hooks/fsmonitor neutralizer right after ``git``;
    # the subcommand is what follows those pairs.

    def _subcommand(argv: list[str]) -> list[str]:
        rest = argv[1:]
        while rest[:1] == ["-c"]:
            rest = rest[2:]
        return rest

    assert any(_subcommand(argv)[:3] == ["remote", "add", "origin"] for argv in wrapped)
    assert public_url in "\n".join(part for argv in wrapped for part in argv)

    for argv, kwargs in spawned:
        process_env = kwargs["env"]
        assert isinstance(process_env, dict)
        if "fetch" in argv:
            _assert_credential_transport_hardening(process_env, raw_url, public_url)
        else:
            assert secret not in repr(process_env)


@pytest.mark.asyncio
async def test_branch_fetch_materializes_tracking_branch_with_real_git(monkeypatch, tmp_path):
    """The split fetch has real clone-equivalent branch/tracking semantics."""
    work = tmp_path / "work"
    remote = tmp_path / "remote.git"
    dest = tmp_path / "checkout"
    work.mkdir()

    def _git(*args, cwd):
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                timeout=15,
                check=False,
            )
        except FileNotFoundError:
            pytest.skip("git is unavailable")
        assert completed.returncode == 0, completed.stderr
        return completed.stdout.strip()

    _git("init", "--quiet", "--template=", cwd=work)
    _git("checkout", "--quiet", "-b", "main", cwd=work)
    (work / "payload.txt").write_text("branch payload\n", encoding="utf-8")
    _git("add", "payload.txt", cwd=work)
    _git(
        "-c",
        "user.name=Registry Test",
        "-c",
        "user.email=registry@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "base",
        cwd=work,
    )
    _git("clone", "--quiet", "--bare", str(work), str(remote), cwd=tmp_path)

    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    result = await registry._git_fetch_branch(
        str(remote),
        "main",
        dest,
        [],
        clone_env=registry.minimal_env(),
        sandbox_mode="strict",
    )

    assert result is None
    assert _git("branch", "--show-current", cwd=dest) == "main"
    assert _git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=dest) == "origin/main"
    assert (dest / "payload.txt").read_text(encoding="utf-8") == "branch payload\n"


@pytest.mark.asyncio
async def test_failed_credentialed_update_restores_same_origin_checkout(monkeypatch, tmp_path):
    dest = tmp_path / "demo"
    (dest / ".git").mkdir(parents=True)
    (dest / "local-edit.txt").write_text("preserve me", encoding="utf-8")
    public_url = "https://example.com/o/demo.git"
    raw_url = "https://user:secret@example.com/o/demo.git"
    pending: list = []
    restorable: list = []

    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)

    async def _origin(path):
        return public_url

    async def _failed_fetch(*args, **kwargs):
        assert not dest.exists(), "same-origin checkout must be isolated before network fetch"
        return {"ok": False, "error": "fetch failed"}

    monkeypatch.setattr(registry, "_clone_origin_url", _origin)
    monkeypatch.setattr(registry, "_git_fetch_branch", _failed_fetch)

    result = await registry._git_clone_or_pull(
        public_url,
        "main",
        dest,
        [],
        credential_target=raw_url,
        pending_cleanup=pending,
        restorable_stale=restorable,
    )

    assert result == {"ok": False, "error": "fetch failed"}
    assert (dest / "local-edit.txt").read_text(encoding="utf-8") == "preserve me"
    assert pending == []
    assert restorable == []


@pytest.mark.asyncio
async def test_double_cancel_waits_for_fetch_cleanup_before_restoring_checkout(
    monkeypatch, tmp_path
):
    """A background deleter must never outlive rollback to the same path."""
    dest = tmp_path / "demo"
    (dest / ".git").mkdir(parents=True)
    (dest / "local-edit.txt").write_text("preserve me", encoding="utf-8")
    public_url = "https://example.com/o/demo.git"
    raw_url = "https://user:secret@example.com/o/demo.git"
    fetch_started = asyncio.Event()
    cleanup_started = threading.Event()
    cleanup_release = threading.Event()
    cleanup_done = threading.Event()
    cleanup_calls = 0

    async def _origin(_path):
        return public_url

    class _Proc:
        pid = 4242
        returncode = 0

        def __init__(self, argv):
            self.argv = argv

        async def communicate(self):
            if "init" in self.argv:
                (dest / ".git").mkdir(parents=True, exist_ok=True)
            if "fetch" in self.argv:
                fetch_started.set()
                # flake-ok: pre-existing never-set Event: the fake fetch hangs until the test cancels it, moved verbatim; tracked for a de-flake pass
                await asyncio.Event().wait()
            return b"", b""

    async def _spawn(*argv, **_kwargs):
        return _Proc(argv)

    def _gated_rmtree(path):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            cleanup_started.set()
            assert cleanup_release.wait(30), "cleanup release was never signalled"
        shutil.rmtree(path, ignore_errors=True)
        if cleanup_calls == 1:
            cleanup_done.set()

    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda _url: True)
    monkeypatch.setattr(registry, "_clone_origin_url", _origin)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(registry, "create_subprocess_limited", _spawn)
    monkeypatch.setattr(registry, "_kill_process_group", AsyncMock())
    monkeypatch.setattr(platform_compat, "rmtree_force", _gated_rmtree)

    task = asyncio.create_task(
        registry._git_clone_or_pull(
            public_url,
            "main",
            dest,
            [],
            credential_target=raw_url,
        )
    )
    await await_until(fetch_started, what="the fetch starting")
    task.cancel()
    # flake-ok: pre-existing 5 s thread-pool wait for the cleanup the test gates, failing by name; moved verbatim
    assert await asyncio.to_thread(cleanup_started.wait, 5)

    task.cancel()
    # flake-ok: pre-existing 50 ms negative window after the second cancel, moved verbatim; tracked for a de-flake pass
    # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing negative window after the second cancel, moved verbatim; tracked for a de-flake pass
    await asyncio.sleep(0.05)
    assert not task.done(), "repeated cancellation escaped before cleanup settled"

    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        # flake-ok: pre-existing unbounded await of the clone task the test released above, moved verbatim; tracked for a de-flake pass
        await task

    assert cleanup_done.is_set()
    assert (dest / "local-edit.txt").read_text(encoding="utf-8") == "preserve me"
    # flake-ok: pre-existing 50 ms late-cleanup window before the last assertion, moved verbatim; tracked for a de-flake pass
    # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing late-cleanup window before the last assertion, moved verbatim; tracked for a de-flake pass
    await asyncio.sleep(0.05)
    assert (dest / "local-edit.txt").is_file(), "late cleanup deleted restored checkout"


# ---------------------------------------------------------------------------
# A failed FRESH clone must actually remove the partial checkout on Windows.
#
# git writes `.git/objects/pack/*.{pack,idx,rev}` read-only. On Windows that is
# FILE_ATTRIBUTE_READONLY, so `shutil.rmtree(..., ignore_errors=True)` cannot
# unlink them and silently reports success over a tree that is still on disk.
# The update path already copes with a surviving tree -- its `finally` moves the
# leftover aside so the restore rename cannot collide -- but the fresh-install
# path had no such guard, so the leftover became permanent: the next install
# sees `dest/.git` with a matching origin, takes the fast-forward branch, and
# `git pull` in a never-finished clone fails on every retry.
#
# POSIX cannot express this: the read-only bit there does not govern unlink (the
# parent directory's write permission does), so `rmtree` succeeds either way and
# the test would be green before the fix. Hence a platform gate rather than a
# simulated failure -- the real file attribute is the entire mechanism.
# ---------------------------------------------------------------------------


def _partial_clone(dest):
    """What a `git clone` killed partway through leaves behind at *dest*."""
    pack = dest / ".git" / "objects" / "pack"
    pack.mkdir(parents=True, exist_ok=True)
    (dest / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://example.com/demo.git\n', encoding="utf-8"
    )
    blob = pack / "pack-0123456789abcdef0123456789abcdef01234567.pack"
    blob.write_bytes(b"PACK")
    os.chmod(blob, stat.S_IREAD)
    return blob


@pytest.mark.asyncio
@pytest.mark.skipif(
    platform_compat.IS_POSIX,
    reason="the read-only attribute only blocks unlink on Windows",
)
async def test_manifest_and_index_temp_roots_remove_read_only_git_children(monkeypatch, tmp_path):
    """Both throwaway clone consumers must remove Windows read-only pack files."""
    roots = [tmp_path / "manifest-tmp", tmp_path / "registry-tmp"]
    returned_roots = iter(roots)
    raw = "https://user:secret@example.com/org/apps.git"

    def _mkdtemp(**_kwargs):
        root = next(returned_roots)
        root.mkdir(parents=True)
        return str(root)

    async def _fetch(_git_url, _branch, dest, _log_lines, **_kwargs):
        _partial_clone(dest)
        (dest / "app.json").write_text(json.dumps({"name": "private-app"}), encoding="utf-8")
        (dest / "app-registry.json").write_text(
            json.dumps([{"name": "private-app", "repo": raw}]),
            encoding="utf-8",
        )
        return None

    monkeypatch.setattr("tempfile.mkdtemp", _mkdtemp)
    monkeypatch.setattr(registry, "_git_fetch_branch", _fetch)
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda _url: True)
    monkeypatch.setattr(registry, "_sel_fn", None)

    manifest = await registry._fetch_app_manifest(
        raw,
        "main",
        git_url=raw,
        owner_designated=True,
    )
    index = await registry._fetch_external_registry_index(raw, "main")

    assert manifest == {"name": "private-app"}
    assert index is not None and index[0]["name"] == "private-app"
    assert all(not root.exists() for root in roots)


def _fresh_clone_harness(monkeypatch, dest, *, mode):
    """Patch registry so a fresh clone into *dest* fails in *mode*."""
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(registry, "_kill_process_group", AsyncMock())
    monkeypatch.setattr(registry, "_CLONE_TIMEOUT", 0.05)

    class _Proc:
        returncode = 1 if mode == "exit" else 0
        pid = 4242

        async def communicate(self):
            if mode == "timeout":
                # flake-ok: the fake clone's hang stand-in, which the patched _CLONE_TIMEOUT cuts short; moved verbatim
                await asyncio.sleep(30)
            if mode == "cancel":
                raise asyncio.CancelledError()
            return b"fatal: early EOF", b""

    async def _fake_spawn(*argv, **kwargs):
        # git created the destination and wrote pack files before it died.
        _partial_clone(dest)
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)


@pytest.mark.asyncio
@pytest.mark.skipif(
    platform_compat.IS_POSIX,
    reason="the read-only attribute only blocks unlink on Windows",
)
@pytest.mark.parametrize("mode", ["exit", "timeout", "cancel"])
async def test_failed_fresh_clone_removes_read_only_partial_checkout(monkeypatch, tmp_path, mode):
    """Every fresh-clone failure exit must leave no destination behind."""
    dest = tmp_path / "app-sources" / "demoapp"
    _fresh_clone_harness(monkeypatch, dest, mode=mode)

    if mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await registry._git_clone_or_pull("https://example.com/demo.git", "main", dest, [])
    else:
        err = await registry._git_clone_or_pull("https://example.com/demo.git", "main", dest, [])
        assert err is not None and err["ok"] is False

    assert not dest.exists(), (
        "the partial checkout survived: the next install would find its .git, "
        "take the fast-forward branch and fail on every retry"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    platform_compat.IS_POSIX,
    reason="the read-only attribute only blocks unlink on Windows",
)
async def test_failed_pinned_fetch_removes_read_only_partial_checkout(monkeypatch, tmp_path):
    """The pinned path materialises its own destination with `git init`; a failed
    fetch must discard it as completely as the clone path does."""
    dest = tmp_path / "app-sources" / "pinnedapp"
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(registry, "_kill_process_group", AsyncMock())

    class _Proc:
        pid = 4242

        def __init__(self, rc):
            self.returncode = rc

        async def communicate(self):
            return b"fatal: could not read from remote repository", b""

    async def _fake_spawn(*argv, **kwargs):
        if "init" in argv:
            _partial_clone(dest)  # git init made it; the fetch left pack files
            return _Proc(0)
        if "fetch" in argv:
            return _Proc(1)
        return _Proc(0)

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)

    err = await registry._git_fetch_commit(
        "https://example.com/demo.git",
        "a" * 40,
        dest,
        [],
        clone_env={},
        sandbox_mode="strict",
    )

    assert err is not None and err["ok"] is False
    assert not dest.exists(), "the pinned path left an undeletable checkout behind"
