"""The provider CLI (``gh`` / ``glab``) as a process: which executable is trusted,
how ``_run_json`` sandboxes, bounds and audits a run, and how a killed child is reaped,
plus the redaction of provider text in errors and payloads."""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from source_providers_helpers import _clear_issue_cache  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401

from kiro_crew import github_runner
from kiro_crew.dashboard.handlers import source_providers as source
from kiro_crew.sandbox import spawn_shim_argv


def _trust_ancestors_above(monkeypatch, base):
    """Pin ancestor owners without changing fixture files or permission bits."""
    ancestors = set(base.resolve().parents)
    real_stat = pathlib.Path.stat

    def fake_stat(self, **kwargs):
        info = real_stat(self, **kwargs)
        if self not in ancestors:
            return info

        class AncestorStat:
            st_uid = 0

            def __getattr__(self, name):
                return getattr(info, name)

        return AncestorStat()

    monkeypatch.setattr(pathlib.Path, "stat", fake_stat)


def test_safe_error_redacts_credentials_and_exfiltration_urls() -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    payload = "x" * 80
    error = source._safe_error(
        f"failed with {secret} at https://attacker.example/c?data={payload}".encode()
    )
    assert secret not in error
    assert payload not in error
    assert "[REDACTED" in error


def test_provider_executable_rejects_agent_writable_tree(monkeypatch, tmp_path) -> None:
    """A gh shim planted inside the project checkout is refused even though it
    is user-owned like every other accepted install — the agent can write there."""
    project = tmp_path / "project"
    (project / "bin").mkdir(parents=True)
    shim = project / "bin" / "gh"
    shim.write_text("#!/bin/sh\nexit 99\n")
    shim.chmod(0o755)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(project))
    monkeypatch.setenv("KIROCREW_GH_BIN", str(shim))

    with pytest.raises(source.SourceProviderError, match="inside the agent-writable tree"):
        source._resolve_provider_executable("gh")


def test_provider_executable_candidates_append_path_hits(monkeypatch, tmp_path) -> None:
    """Well-known dirs are tried first, then whatever PATH resolves — so an
    install the user already runs from their terminal is found."""
    user_bin = tmp_path / "user-bin"
    user_bin.mkdir()
    found = user_bin / "gh"
    found.write_text("#!/bin/sh\nexit 0\n")
    found.chmod(0o755)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setenv("PATH", f"{user_bin}:/usr/bin")
    monkeypatch.setattr(
        github_runner,
        "PROVIDER_EXECUTABLE_CANDIDATES",
        {"gh": ("/usr/local/libexec/kirocrew/gh",), "glab": ("/usr/bin/glab",)},
    )

    candidates = source.provider_executable_candidates("gh")

    assert candidates[0] == "/usr/local/libexec/kirocrew/gh"
    assert str(found) in candidates


def test_provider_executable_candidates_ignore_path_in_strict_mode(monkeypatch, tmp_path) -> None:
    user_bin = tmp_path / "user-bin"
    user_bin.mkdir()
    planted = user_bin / "gh"
    planted.write_text("#!/bin/sh\nexit 0\n")
    planted.chmod(0o755)
    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    monkeypatch.setenv("PATH", str(user_bin))

    assert (
        source.provider_executable_candidates("gh") == source._PROVIDER_EXECUTABLE_CANDIDATES["gh"]
    )


def test_provider_executable_not_found_gives_install_guidance(monkeypatch) -> None:
    monkeypatch.delenv("KIROCREW_GH_BIN", raising=False)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(
        github_runner,
        "PROVIDER_EXECUTABLE_CANDIDATES",
        {"gh": ("/nonexistent-kirocrew/gh",), "glab": ("/nonexistent-kirocrew/glab",)},
    )
    # Windows also scans the Program Files install dirs; a host with a real gh
    # there (GitHub's own runners ship one) must still read as "not found".
    monkeypatch.setattr(github_runner, "_wellknown_windows_dirs", lambda _executable: ())

    with pytest.raises(source.SourceProviderError) as excinfo:
        source._resolve_provider_executable("gh")

    message = str(excinfo.value)
    # Install-and-sign-in guidance, NOT the old root-owned sudo copy ritual.
    assert "brew install gh" in message
    assert "gh auth login" in message
    assert "sudo cp" not in message
    assert "KIROCREW_GH_BIN" in message
    assert "{executable}" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("missing", "executable_not_found"),
        ("candidate_untrusted", "executable_untrusted"),
        ("override_untrusted", "executable_untrusted"),
    ],
)
async def test_run_json_audits_provider_resolution_failure_reason(
    monkeypatch, tmp_path, _mock_source_sel, case: str, expected_reason: str
) -> None:
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    candidate_dir = tmp_path / "provider-bin"
    candidate_dir.mkdir()
    candidate = candidate_dir / "gh"
    monkeypatch.delenv("KIROCREW_GH_BIN", raising=False)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setenv("PATH", str(empty_path))
    monkeypatch.setattr(
        github_runner,
        "PROVIDER_EXECUTABLE_CANDIDATES",
        {"gh": (str(candidate),), "glab": (str(candidate_dir / "glab"),)},
    )
    monkeypatch.setattr(github_runner, "_wellknown_windows_dirs", lambda _executable: ())

    if case != "missing":
        candidate.write_text("#!/bin/sh\nexit 0\n")
        candidate.chmod(0o755)

        def reject_found(path: str) -> str:
            assert pathlib.Path(path).is_file()
            raise ValueError("executable parent is world-writable")

        monkeypatch.setattr(source, "_validate_provider_executable", reject_found)
    if case == "override_untrusted":
        monkeypatch.setenv("KIROCREW_GH_BIN", str(candidate))

    with pytest.raises(source.SourceProviderError):
        await source._run_json("gh", "api", "repos/acme/repo")

    call = _mock_source_sel.log_tool_invocation.call_args
    assert call.kwargs["outcome"] == "denied"
    assert call.kwargs["error"] == expected_reason
    assert call.kwargs["metadata"]["reason"] == expected_reason


def test_provider_executable_strict_mode_asks_for_a_root_owned_copy(monkeypatch) -> None:
    monkeypatch.delenv("KIROCREW_GH_BIN", raising=False)
    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    monkeypatch.setattr(
        github_runner,
        "PROVIDER_EXECUTABLE_CANDIDATES",
        {
            "gh": ("/usr/local/libexec/kirocrew/gh",),
            "glab": ("/usr/local/libexec/kirocrew/glab",),
        },
    )
    monkeypatch.setattr(
        source,
        "_validate_provider_executable",
        MagicMock(side_effect=ValueError("executable is not root-owned")),
    )

    with pytest.raises(source.SourceProviderError) as excinfo:
        source._resolve_provider_executable("gh")

    message = str(excinfo.value)
    assert "KIROCREW_PROVIDER_BIN_STRICT" in message
    assert 'sudo cp "$(command -v gh)" /usr/local/libexec/kirocrew/gh' in message
    assert "gh auth login" in message


def test_provider_executable_rejects_relative_override(monkeypatch) -> None:
    monkeypatch.setenv("KIROCREW_GH_BIN", "workspace/bin/gh")

    with pytest.raises(source.SourceProviderError, match="path must be absolute"):
        source._resolve_provider_executable("gh")


_tmp_owner_ok = sys.platform == "win32" or os.stat(tempfile.gettempdir()).st_uid in (
    0,
    os.geteuid(),
)


@pytest.mark.skipif(not _tmp_owner_ok, reason="temp dir not owned by root or current user")
def test_provider_executable_accepts_user_owned_install(monkeypatch, tmp_path) -> None:
    """The default policy accepts the user's own gh — the Homebrew case that
    would otherwise force a `sudo cp` into a root-owned directory."""
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setenv("KIROCREW_GH_BIN", str(executable))
    _trust_ancestors_above(monkeypatch, tmp_path)

    assert source._resolve_provider_executable("gh") == str(executable.resolve())


@pytest.mark.skipif(not _tmp_owner_ok, reason="temp dir not owned by root or current user")
def test_provider_executable_accepts_symlinked_install(monkeypatch, tmp_path) -> None:
    """Homebrew's layout (bin/gh -> ../Cellar/gh/<v>/bin/gh) resolves through the
    symlink instead of being refused for not being canonical."""
    cellar = tmp_path / "Cellar" / "gh" / "2.0.0" / "bin"
    cellar.mkdir(parents=True)
    target = cellar / "gh"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o555)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "gh"
    link.symlink_to(target)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setenv("KIROCREW_GH_BIN", str(link))
    _trust_ancestors_above(monkeypatch, tmp_path)

    assert source._resolve_provider_executable("gh") == str(target.resolve())


def test_provider_executable_strict_mode_rejects_user_owned_install(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o500)
    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    monkeypatch.setenv("KIROCREW_GH_BIN", str(executable))

    with pytest.raises(source.SourceProviderError, match="executable is not root-owned"):
        source._resolve_provider_executable("gh")


def test_provider_executable_strict_mode_rejects_symlink(monkeypatch, tmp_path) -> None:
    target = tmp_path / "real-gh"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o500)
    link = tmp_path / "gh"
    link.symlink_to(target)
    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    monkeypatch.setenv("KIROCREW_GH_BIN", str(link))

    with pytest.raises(source.SourceProviderError, match="canonical.*no symlinks"):
        source._resolve_provider_executable("gh")


@pytest.mark.skipif(sys.platform == "win32", reason="exercises the POSIX uid branch")
def test_provider_executable_refuses_a_root_gateway(monkeypatch, tmp_path) -> None:
    """A root POSIX gateway is refused in BOTH modes. The sandbox masks the
    credential homes from the agent's children but leaves the filesystem
    writable, and a provider child runs unsandboxed with those credentials —
    so a root agent could overwrite a root-owned `gh` and this walk could not
    tell that write from the operator's install. The refusal keeps the mask a
    boundary; the Windows elevated case has no such boundary and is not refused
    (see the test below)."""
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setattr(github_runner.os, "geteuid", lambda: 0)

    with pytest.raises(ValueError, match="disabled for a root gateway"):
        source._validate_provider_executable(str(executable))


def test_provider_executable_elevated_windows_gateway_is_not_refused(monkeypatch, tmp_path) -> None:
    """On Windows the validator asks nothing about the token's elevation: the
    built-in Administrator account (always elevated, no UAC split token) and a
    "Run as administrator" launch both go through the same ACL walk as any user,
    keyed on the gateway user's SID."""
    executable = tmp_path / "gh.exe"
    executable.write_text("rem\n")
    executable.chmod(0o755)
    checked: list[tuple[str, str]] = []

    def fake_windows_check(path, *, label, me_sid, strict):
        checked.append((label, me_sid))

    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner.sys, "platform", "win32")
    monkeypatch.setattr(github_runner.platform_compat, "current_user_sid", lambda: "S-1-5-21-7-500")
    monkeypatch.setattr(github_runner, "check_provider_path_component_windows", fake_windows_check)
    monkeypatch.setattr(github_runner, "path_parents", lambda _path: [])

    assert github_runner.validate_provider_executable(str(executable)) == str(executable.resolve())
    assert checked == [("executable", "S-1-5-21-7-500")]


def test_provider_executable_windows_still_refuses_an_unverifiable_sid(
    monkeypatch, tmp_path
) -> None:
    """The SID is the analog of ``uid``; without it the ACL walk cannot say
    whose install this is, so that refusal stays."""
    executable = tmp_path / "gh.exe"
    executable.write_text("rem\n")
    executable.chmod(0o755)
    monkeypatch.setattr(github_runner.sys, "platform", "win32")
    monkeypatch.setattr(github_runner.platform_compat, "current_user_sid", lambda: None)

    with pytest.raises(ValueError, match="SID is unverifiable"):
        github_runner.validate_provider_executable(str(executable))


def test_provider_executable_rejects_binary_owned_by_another_user(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    real_stat = executable.stat()
    foreign_stat = github_runner.os.stat_result([*list(real_stat)[:4], 4242, *list(real_stat)[5:]])
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setattr(github_runner.Path, "stat", lambda _path: foreign_stat)

    with pytest.raises(ValueError, match="owned by another user"):
        source._validate_provider_executable(str(executable))


def test_provider_executable_rejects_world_writable_binary(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o777)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())

    with pytest.raises(ValueError, match="executable is world-writable"):
        source._validate_provider_executable(str(executable))


def test_provider_executable_rejects_world_writable_parent(monkeypatch, tmp_path) -> None:
    parent = tmp_path / "provider-bin"
    parent.mkdir()
    executable = parent / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    parent.chmod(0o777)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    _trust_ancestors_above(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="executable parent is world-writable"):
        source._validate_provider_executable(str(executable))


def test_provider_executable_tolerates_a_sticky_world_writable_parent(
    monkeypatch, tmp_path
) -> None:
    """`/tmp`-style 1777 dirs are fine: only the owner can replace an entry, so
    the "owned by another user" check still decides. Linux CI runners put every
    pytest tmp dir under /tmp, so rejecting sticky dirs outright would also make
    the accept-path untestable there."""
    parent = tmp_path / "sticky-bin"
    parent.mkdir()
    executable = parent / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    parent.chmod(0o1777)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    _trust_ancestors_above(monkeypatch, tmp_path)

    assert source._validate_provider_executable(str(executable)) == str(executable.resolve())


def test_provider_executable_strict_mode_rejects_untrusted_ancestor(monkeypatch, tmp_path) -> None:
    """Strict mode keeps the historical root-owned, unwritable-ancestor rule."""
    parent = tmp_path / "provider-bin"
    parent.mkdir()
    executable = parent / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o500)
    executable_stat = executable.stat()
    root_executable_stat = github_runner.os.stat_result(
        [*list(executable_stat)[:4], 0, *list(executable_stat)[5:]]
    )
    real_stat = github_runner.Path.stat

    def fake_stat(path):
        if path == executable:
            return root_executable_stat
        return real_stat(path)

    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    monkeypatch.setattr(
        github_runner.platform_compat,
        "traversed_components_and_links",
        lambda _path: ([parent, executable.resolve()], []),
    )
    monkeypatch.setattr(github_runner.Path, "stat", fake_stat)
    monkeypatch.setattr(
        github_runner.os, "access", lambda _path, mode: mode == github_runner.os.X_OK
    )

    with pytest.raises(ValueError, match="executable parent is not root-owned"):
        source._validate_provider_executable(str(executable))


def test_provider_executable_relaxed_mode_declines_a_writable_hop_in_the_middle_of_a_chain(
    monkeypatch, tmp_path
) -> None:
    """``trusted/gh -> writable/hop -> trusted/real-gh``: the hop's directory decides.

    Both ENDPOINTS' directory chains are tight, so two lexical chains over the
    endpoints (the resolved path's parents, the original's parents) never name
    ``writable`` and accept the chain end to end. The component walk reads
    ``writable`` to follow the hop, so it is checked like any other parent.
    """
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    writable = tmp_path / "writable"
    writable.mkdir()
    target = trusted / "real-gh"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    middle = writable / "hop"
    middle.symlink_to(target)
    entry = trusted / "gh"
    entry.symlink_to(middle)
    writable.chmod(0o777)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    _trust_ancestors_above(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="executable parent is world-writable"):
        github_runner.validate_provider_executable(str(entry))


def test_provider_executable_relaxed_mode_declines_a_writable_ancestor_of_a_symlinked_component(
    monkeypatch, tmp_path
) -> None:
    """``prefix/bin -> holder/bin``: the target's own parent ``holder`` is checked.

    The resolved path's own lexical chain names ``holder``, so this pins a
    refusal the component walk must keep rather than a gap it closes.
    """
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    holder = tmp_path / "holder"
    holder.mkdir()
    real_bin = holder / "bin"
    real_bin.mkdir()
    leaf = real_bin / "gh"
    leaf.write_text("#!/bin/sh\nexit 0\n")
    leaf.chmod(0o755)
    (prefix / "bin").symlink_to(real_bin)
    holder.chmod(0o777)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    _trust_ancestors_above(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="executable parent is world-writable"):
        github_runner.validate_provider_executable(str(prefix / "bin" / "gh"))


@pytest.mark.skipif(not _tmp_owner_ok, reason="temp dir not owned by root or current user")
def test_provider_executable_relaxed_mode_still_accepts_a_chain_through_tight_directories(
    monkeypatch, tmp_path
) -> None:
    """The widening is strictly a widening: the same hop chain through directories
    that pass the policy is accepted, so an ordinary symlinked install (Homebrew's
    ``bin/gh -> ../Cellar/...``, a ``/usr/local/bin`` link into ``/opt``) gains no
    new refusal."""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    hops = tmp_path / "hops"
    hops.mkdir()
    # Only the link's side reads this directory, so its group bit decides
    # whether the entry keeps its name; pin it whatever the umask.
    hops.chmod(0o755)
    target = trusted / "real-gh"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    middle = hops / "hop"
    middle.symlink_to(target)
    entry = trusted / "gh"
    entry.symlink_to(middle)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    # The link rule applies where an ACL's write shows in the mode bits.
    monkeypatch.setattr(github_runner, "_acl_writes_show_in_mode_bits", lambda: True)
    _trust_ancestors_above(monkeypatch, tmp_path)

    # The entry is named differently from its target and every hop is the
    # user's, so the entry's own spelling is what gets launched.
    assert github_runner.validate_provider_executable(str(entry)) == str(entry)


def test_provider_executable_refuses_a_chain_the_walk_cannot_enumerate(
    monkeypatch, tmp_path
) -> None:
    """A walk that answers ``None`` is a refusal, never a shorter parent list."""
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setattr(
        github_runner.platform_compat, "traversed_components_and_links", lambda _path: None
    )

    with pytest.raises(ValueError, match="executable hierarchy is not accessible"):
        github_runner.validate_provider_executable(str(executable))


def test_redact_provider_data_recurses_through_external_strings() -> None:
    secret = "ghp_" + "a" * 36
    query = "x" * 80
    raw = {
        "description": f"token={secret}",
        "files": [{"patch": f"+{secret}"}],
        "comments": [{"body": f"see https://attacker.example/c?data={query}"}],
        "count": 1,
    }

    cleaned = source._redact_provider_data(raw)

    serialized = source.json.dumps(cleaned)
    assert secret not in serialized
    assert query not in serialized
    assert serialized.count("[REDACTED") >= 3
    assert cleaned["count"] == 1


@pytest.mark.asyncio
async def test_run_json_kills_process_tree_when_stdout_exceeds_limit(monkeypatch) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on the fake process, moved verbatim; tracked for a de-flake pass
            self.pid = 4242
            self.stdout = source.asyncio.StreamReader()
            self.stderr = source.asyncio.StreamReader()
            self.stdout.feed_data(b"12345")
            self.stderr.feed_eof()
            self.returncode = None
            self.killed = False
            self.done = source.asyncio.Event()

        async def wait(self):
            # flake-ok: pre-existing unbounded hold in the fake process until the kill sets done, moved verbatim; tracked for a de-flake pass
            await self.done.wait()
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.done.set()

        async def communicate(self):
            # The bounded reap drains the pipes via communicate() rather than a
            # bare wait() that a full pipe could hang.
            self.returncode = -9
            self.done.set()
            return b"", b""

    proc = FakeProcess()
    spawn_kwargs = {}

    async def fake_create(*_args, **kwargs):
        spawn_kwargs.update(kwargs)
        return proc

    tree_kills: list[tuple[int, int]] = []

    async def kill_tree(pid, sig):
        tree_kills.append((pid, sig))
        proc.returncode = -sig
        proc.done.set()
        return True

    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(source.platform_compat, "kill_process_tree_async", kill_tree)
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(source.SourceProviderError, match="response was too large"):
        await source._run_json("gh", "api", "repos/acme/repo", max_output_bytes=4)
    # The whole tree is SIGKILLed through the bounded reap (kill_and_reap).
    assert tree_kills == [(proc.pid, source.platform_compat.SIGKILL)]
    assert spawn_kwargs["env"]["GH_HOST"] == "github.com"
    assert spawn_kwargs["start_new_session"] is source.platform_compat.IS_POSIX
    assert spawn_kwargs["creationflags"] == source.platform_compat.CREATE_NEW_PROCESS_GROUP


@pytest.mark.asyncio
async def test_run_json_on_windows_defers_to_the_sandbox_gate(monkeypatch) -> None:
    """Windows is not refused by a platform check of its own.

    It has no OS sandbox backend, but neither does a backend-less Linux host, and
    both must reach the same gate: ``sandboxed_spawn_argv`` fail-closes unless the
    operator opted into unsandboxed exec, and its refusal names that opt-in. The
    old blanket check ran BEFORE that gate, so it made the documented escape
    hatch unreachable on Windows alone and left the Changes panel permanently
    dead there. Asserting the resolver is now REACHED is what pins that: it sat
    behind the removed refusal, so a reintroduced platform check fails here.
    """
    resolver = MagicMock(return_value="C:\\gh\\gh.exe")
    sandbox = MagicMock(side_effect=RuntimeError("no OS-level sandbox backend"))
    spawn = AsyncMock()
    monkeypatch.setattr(source.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(source, "_resolve_provider_executable", resolver)
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(source.SourceProviderError, match="could not start securely"):
        await source._run_json("gh", "api", "repos/acme/repo")

    resolver.assert_called_once()
    sandbox.assert_called_once()
    # The sandbox refused, so nothing was ever executed unisolated.
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_json_on_windows_proceeds_once_the_sandbox_gate_allows(monkeypatch) -> None:
    """With the opt-in in force the gate returns argv and the read completes.

    The companion to the test above: together they show Windows now has BOTH
    outcomes the gate defines, rather than one hard-coded refusal.

    On Windows the provider read runs off the gateway event loop: ``_run_provider``
    wraps the argv through the sandbox gate as before, then spawns the one-shot via
    ``_oneshot_drain_limited`` on a worker thread. It is stubbed because its real
    ``popen_limited`` applies the POSIX spawn shim on this POSIX CI host; what this
    test pins is the sandbox gate, not the spawn mechanics.
    """

    from kiro_crew.dashboard.source_providers import runner as runner_module

    monkeypatch.setattr(source.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        source, "_resolve_provider_executable", MagicMock(return_value="/usr/bin/gh")
    )
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(
        runner_module, "_oneshot_drain_limited", MagicMock(return_value=(0, b"{}", b""))
    )

    assert await source._run_json("gh", "api", "repos/acme/repo") == {}


@pytest.mark.asyncio
async def test_run_json_resolves_the_provider_cli_off_the_event_loop(monkeypatch) -> None:
    """Resolution stats every candidate and the whole parent chain of each hit,
    and the sidebar chip refresh reaches it on a timer with no user present. On
    the loop thread a slow filesystem freezes every task until the loop watchdog
    kills the gateway, so the walk has to happen on a worker thread."""

    class FakeProcess:
        returncode = 0

    resolver_threads: list[int] = []

    def recording_resolver(_name: str) -> str:
        resolver_threads.append(threading.get_ident())
        return "/usr/bin/gh"

    monkeypatch.setattr(source, "_resolve_provider_executable", recording_resolver)
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(source, "_collect_process_output", AsyncMock(return_value=(b"{}", b"")))

    assert await source._run_json("gh", "api", "repos/acme/repo") == {}

    assert len(resolver_threads) == 1
    assert resolver_threads[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_run_json_sandboxes_with_minimal_provider_environment(monkeypatch) -> None:
    class FakeProcess:
        returncode = 0

    # An absolute launcher path, as sandboxed_spawn_argv really returns: the
    # spawn shim execs without a PATH search, so a bare name here would not
    # describe anything the chokepoint actually produces.
    sandbox = MagicMock(
        return_value=(["/usr/bin/sandbox-launcher", "/usr/bin/gh", "api"], {"SAFE": "1"}, None)
    )
    spawn = AsyncMock(return_value=FakeProcess())
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "a" * 36)
    monkeypatch.setenv("PATH", "/workspace/attacker-bin")
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(source, "_collect_process_output", AsyncMock(return_value=(b"{}", b"")))

    assert await source._run_json("gh", "api", "repos/acme/repo") == {}

    base_env = sandbox.call_args.kwargs["env"]
    assert sandbox.call_args.args[0] == ["/usr/bin/gh", "api", "repos/acme/repo"]
    assert sandbox.call_args.kwargs["mode"] == "standard"
    assert base_env["GH_TOKEN"].startswith("ghp_")
    assert base_env["GH_HOST"] == "github.com"
    assert "SLACK_BOT_TOKEN" not in base_env
    assert "AWS_ACCESS_KEY_ID" not in base_env
    assert base_env["PATH"] == source._PROVIDER_SYSTEM_PATH
    assert "/workspace/attacker-bin" not in base_env["PATH"]
    assert spawn.call_args.kwargs["env"] == {"SAFE": "1"}
    # The resource ceiling is delivered AFTER exec by the spawn shim, never by a
    # preexec_fn: one would fork this threaded gateway and run Python in the
    # child, where a wedge blocks the event loop and pins the inherited fds.
    assert spawn.call_args.kwargs["preexec_fn"] is None
    shim = spawn_shim_argv()
    assert shim, "POSIX hosts must have a shim available for this assertion"
    assert spawn.call_args.args[: len(shim)] == shim
    assert spawn.call_args.args[len(shim) :] == (
        "/usr/bin/sandbox-launcher",
        "/usr/bin/gh",
        "api",
    )


@pytest.mark.asyncio
async def test_run_json_globally_bounds_provider_processes(monkeypatch) -> None:
    class FakeProcess:
        returncode = 0

    active = 0
    peak = 0

    async def collect(_proc, _executable, max_output_bytes):
        nonlocal active, peak
        assert max_output_bytes == source._METADATA_OUTPUT_BYTES
        active += 1
        peak = max(peak, active)
        # flake-ok: pre-existing 10 ms sleep inside the stub so concurrent runs overlap while the test counts the peak, moved verbatim; tracked for a de-flake pass
        await source.asyncio.sleep(0.01)
        active -= 1
        return b"{}", b""

    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(source, "_collect_process_output", collect)

    await source.asyncio.gather(
        *(source._run_json("gh", "api", f"repos/acme/repo/{i}") for i in range(10))
    )

    assert peak <= source._PROVIDER_CONCURRENCY


def _prepare_audited_provider_run(monkeypatch, collect) -> None:
    class FakeProcess:
        returncode = 0

    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(source, "_collect_process_output", collect)


@pytest.mark.asyncio
async def test_run_json_audits_success_without_sensitive_values(
    monkeypatch, _mock_source_sel
) -> None:
    secret = "ghp_" + "a" * 36
    raw_url = "https://github.com/acme/private/pull/12"
    collect = AsyncMock(return_value=(b"{}", b""))
    _prepare_audited_provider_run(monkeypatch, collect)
    monkeypatch.setenv("GH_TOKEN", secret)

    assert await source._run_json("gh", "pr", "view", raw_url) == {}

    calls = _mock_source_sel.log_tool_invocation.call_args_list
    assert [call.kwargs["outcome"] for call in calls] == ["invoked", "completed"]
    assert calls[0].kwargs["critical"] is True
    serialized = str(calls)
    assert raw_url not in serialized
    assert secret not in serialized
    assert "pr view" not in serialized


@pytest.mark.asyncio
async def test_run_json_awaits_critical_audit_off_loop_before_spawn(
    monkeypatch, _mock_source_sel
) -> None:
    audit_started = source.asyncio.Event()
    release_audit = source.asyncio.Event()
    order: list[str] = []

    async def fake_to_thread(func, *args, **kwargs):
        # Only the audit offload is under test; every other offload on this path
        # (executable resolution) has to pass through with its real result.
        if func is not source._audit_provider_cli:
            return func(*args, **kwargs)
        order.append("audit-started")
        audit_started.set()
        # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
        await release_audit.wait()
        result = func(*args, **kwargs)
        order.append("audit-completed")
        return result

    class FakeProcess:
        returncode = 0

    async def fake_spawn(*_args, **_kwargs):
        order.append("spawned")
        return FakeProcess()

    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(source.asyncio, "to_thread", fake_to_thread)
    spawn = AsyncMock(side_effect=fake_spawn)
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        source,
        "_collect_process_output",
        AsyncMock(return_value=(b"{}", b"")),
    )

    task = source.asyncio.create_task(source._run_json("gh", "api", "repos/acme/private"))
    # flake-ok: pre-existing unbounded wait for the stub to signal it started, moved verbatim; tracked for a de-flake pass
    await audit_started.wait()
    spawn.assert_not_awaited()

    release_audit.set()
    # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
    assert await task == {}
    assert order == ["audit-started", "audit-completed", "spawned"]
    call = _mock_source_sel.log_tool_invocation.call_args_list[0]
    assert call.kwargs["outcome"] == "invoked"
    assert call.kwargs["critical"] is True


@pytest.mark.asyncio
async def test_run_json_cancellation_reconciles_inflight_critical_audit_before_return(
    monkeypatch,
) -> None:
    audit_started = threading.Event()
    release_audit = threading.Event()
    events: list[tuple[str, str, bool]] = []

    def blocking_audit(
        _executable: str,
        outcome: str,
        reason: str,
        *,
        critical: bool = False,
    ) -> None:
        if outcome == "invoked":
            audit_started.set()
            # flake-ok: pre-existing 2 s ceiling in the audit stub's worker thread until the test releases it, moved verbatim; tracked for a de-flake pass
            assert release_audit.wait(timeout=2)
        events.append((outcome, reason, critical))

    class FakeProcess:
        returncode = 0

    spawn = AsyncMock(return_value=FakeProcess())
    monkeypatch.setattr(source, "_audit_provider_cli", blocking_audit)
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", spawn)

    task = source.asyncio.create_task(source._run_json("gh", "api", "repos/acme/private"))
    for _ in range(100):
        if audit_started.is_set():
            break
        # flake-ok: pre-existing bounded 1 s poll (100 x 10 ms) for the audit stub to start, moved verbatim; tracked for a de-flake pass
        await source.asyncio.sleep(0.01)
    assert audit_started.is_set()

    task.cancel()
    await source.asyncio.sleep(0)
    assert not task.done()
    spawn.assert_not_awaited()

    release_audit.set()
    with pytest.raises(source.asyncio.CancelledError):
        # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
        await task

    spawn.assert_not_awaited()
    assert events == [
        ("invoked", "dispatch", True),
        ("failed", "request_cancelled", False),
    ]


@pytest.mark.asyncio
async def test_run_json_audits_denial_without_rejected_argv(
    _mock_source_sel,
) -> None:
    secret = "ghp_" + "b" * 36

    with pytest.raises(source.SourceProviderError, match="unsupported provider command"):
        await source._run_json("sh", "-c", f"echo {secret}")

    call = _mock_source_sel.log_tool_invocation.call_args
    assert call.kwargs["outcome"] == "denied"
    assert call.kwargs["error"] == "unsupported_provider"
    assert secret not in str(call)
    assert "echo" not in str(call)


@pytest.mark.asyncio
async def test_run_json_audits_spawn_failure_without_exception_text(
    monkeypatch, _mock_source_sel
) -> None:
    secret = "ghp_" + "c" * 36
    _prepare_audited_provider_run(monkeypatch, AsyncMock())
    monkeypatch.setattr(
        source.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=OSError(f"spawn failed {secret}")),
    )

    with pytest.raises(source.SourceProviderError, match="could not start"):
        await source._run_json("gh", "api", "repos/acme/private")

    calls = _mock_source_sel.log_tool_invocation.call_args_list
    assert [call.kwargs["outcome"] for call in calls] == ["invoked", "failed"]
    assert calls[-1].kwargs["error"] == "provider_error"
    assert secret not in str(calls)


@pytest.mark.asyncio
async def test_run_json_audits_cancellation_and_reraises(monkeypatch, _mock_source_sel) -> None:
    collect = AsyncMock(side_effect=source.asyncio.CancelledError())
    _prepare_audited_provider_run(monkeypatch, collect)

    with pytest.raises(source.asyncio.CancelledError):
        await source._run_json("gh", "api", "repos/acme/private")

    calls = _mock_source_sel.log_tool_invocation.call_args_list
    assert [call.kwargs["outcome"] for call in calls] == ["invoked", "failed"]
    assert calls[-1].kwargs["error"] == "request_cancelled"


@pytest.mark.asyncio
async def test_run_json_denies_spawn_when_critical_audit_is_unavailable(
    monkeypatch, _mock_source_sel
) -> None:
    spawn = AsyncMock()
    _prepare_audited_provider_run(monkeypatch, AsyncMock())
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", spawn)
    _mock_source_sel.log_tool_invocation.side_effect = OSError("audit filesystem unavailable")

    with pytest.raises(source.SourceProviderError, match="provider audit unavailable"):
        await source._run_json("gh", "api", "repos/acme/private")

    spawn.assert_not_awaited()
    call = _mock_source_sel.log_tool_invocation.call_args
    assert call.kwargs["outcome"] == "invoked"
    assert call.kwargs["critical"] is True


@pytest.mark.asyncio
async def test_run_json_rejects_non_provider_executable() -> None:
    with pytest.raises(source.SourceProviderError, match="unsupported provider command"):
        await source._run_json("sh", "-c", "echo unsafe")


class _ReapProbe:
    """A PIPE-stdio child double that records how it is reaped.

    A killed child blocking on a write into a full pipe -- or a surviving
    descendant still holding the pipes open -- makes a bare ``await
    proc.wait()`` hang the caller forever. The bounded reap must
    therefore drain the pipes via ``communicate()`` and must never touch
    ``wait()``.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: "int | None" = None
        self.kill_calls = 0
        self.wait_calls = 0
        self.communicate_calls = 0

    async def communicate(self):
        self.communicate_calls += 1
        self.returncode = -9
        return b"", b""

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.wait_calls += 1
        return -9


@pytest.mark.asyncio
async def test_terminate_process_reaps_via_communicate_not_wait(monkeypatch):
    """``_terminate_process`` must route through the bounded, pipe-draining
    ``kill_and_reap`` -- a bare ``await proc.wait()`` here can hang the gateway
    task forever when the child is killed with a full pipe."""
    from kiro_crew import platform_compat

    proc = _ReapProbe()
    tree_kills: "list[tuple[int, int]]" = []

    async def _fake_tree(pid, sig):
        tree_kills.append((pid, sig))
        return True

    # Fake pid + patched tree kill so no test can reach a real killpg. Both the
    # async helper (used by kill_and_reap) and the legacy sync entry point are
    # patched so the pin stays safe even when run against unmodified code.
    monkeypatch.setattr(platform_compat, "kill_process_tree_async", _fake_tree)
    monkeypatch.setattr(platform_compat, "kill_process_tree", lambda *a, **k: tree_kills.append(a))

    await source._terminate_process(proc)

    assert proc.communicate_calls == 1
    assert proc.wait_calls == 0
    assert tree_kills and tree_kills[0][0] == proc.pid
