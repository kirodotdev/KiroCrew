"""Windows sandbox policy, publication, admission and fail-closed launch contracts."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import sandbox, sandbox_plan, sandbox_windows
from kiro_crew import sandbox_windows_launcher as launcher
from kiro_crew.sandbox_unavailable import SandboxUnavailableError
from kiro_crew.sandbox_windows_policy import (
    WindowsPolicyUnsupported,
    permission_entries,
    render_permissions,
)


def _plan(**request):
    home = r"C:\Users\sandbox"
    host = sandbox_plan.PlanHost(
        home=home,
        cwd=home + r"\workspace",
        tier_dirs=(".aws", ".kiro/crew/cache", ".kiro/crew/.env"),
        cc_files=(".netrc",),
        cc_expose_files=(".aws/config",),
        crew_readonly_targets=(
            ".kiro/crew/security_policy.json",
            ".kiro/crew/profiles",
        ),
        crew_hidden_dirs=(".kiro/crew/.env",),
        unreadable_mask_leaves=(),
        sensitive_env_prefixes=("AWS_",),
        agent_denied_env_keys=("SLACK_BOT_TOKEN",),
        python_env_prefixes=("PYTHONPATH",),
    )
    return sandbox_plan.plan_confinement(
        sandbox_plan.SandboxRequest(tier="cc", backend=sandbox_plan.BACKEND_WINDOWS, **request),
        host,
    )


def _write_policy(path):
    path.write_text(render_permissions({":root": "write"}, network=False), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_windows_uses_common_plan_without_losing_governance_or_credentials():
    plan = _plan()
    entries = permission_entries(plan)
    assert entries[":root"] == "write"
    assert entries["C:/Users/sandbox/.aws"] == "deny"
    assert entries["C:/Users/sandbox/.aws/config"] == "read"
    assert entries["C:/Users/sandbox/.kiro/crew/.env"] == "deny"
    assert entries["C:/Users/sandbox/.kiro/crew/security_policy.json"] == "read"
    assert entries["C:/Users/sandbox/.kiro/crew/profiles"] == "read"


@pytest.mark.skipif(os.name != "nt", reason="Windows path planning")
def test_private_window_and_nested_remask_are_both_retained():
    plan = _plan(
        extra_hidden_dirs=(
            r"C:\Users\sandbox\scratch",
            r"C:\Users\sandbox\scratch\work\private",
        ),
        extra_private_dirs=(r"C:\Users\sandbox\scratch\work",),
    )
    assert not plan.refusals
    entries = permission_entries(plan)
    assert entries["C:/Users/sandbox/scratch"] == "deny"
    assert entries["C:/Users/sandbox/scratch/work/private"] == "deny"
    assert entries["C:/Users/sandbox/scratch/work"] == "write"


@pytest.mark.parametrize(
    "name",
    [
        "C:/x/?key",
        "C:/x/*",
        "relative",
        "C:relative",
        "",
        "C:/sealed:$I30:$INDEX_ALLOCATION/child",
        "C:/sealed::$INDEX_ALLOCATION/child",
        "C:/secret:stream",
    ],
)
def test_unrepresentable_literal_paths_refuse_instead_of_becoming_globs(name):
    plan = replace(_plan(), readonly=(name,))
    with pytest.raises(WindowsPolicyUnsupported):
        permission_entries(plan)


def test_case_alias_cannot_turn_a_readonly_seal_into_a_write_grant():
    plan = replace(_plan(), readonly=(r"C:\Workspace\Policy",), writable=(r"c:\workspace\policy",))
    with pytest.raises(WindowsPolicyUnsupported, match="Conflicting"):
        permission_entries(plan)


def test_profile_strings_roundtrip_without_injecting_permissions():
    malicious = 'C:/test/"\n[permissions.evil.filesystem]\n":root" = "write'
    text = render_permissions(
        {":root": "write", malicious: "deny", "C:/ö/😀": "read"}, network=False
    )
    parsed = json.loads(text)
    assert set(parsed) == {"version", "filesystem", "network"}
    assert parsed["filesystem"][malicious] == "deny"
    assert parsed["network"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows filesystem policy paths")
def test_profile_is_unique_preserves_codex_home_and_seals_its_own_file(tmp_path, monkeypatch):
    home = tmp_path
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "untouched-codex-home"))
    monkeypatch.setattr(sandbox_windows, "_windows_policy_dir", lambda: home)
    one, digest = sandbox_windows._stage_windows_profile({":root": "write"}, network=True)
    two, _ = sandbox_windows._stage_windows_profile({":root": "write"}, network=True)
    try:
        assert one != two and one.parent == home
        assert hashlib.sha256(one.read_bytes()).hexdigest() == digest
        parsed = json.loads(one.read_text())
        assert parsed["filesystem"][one.as_posix()] == "read"
        assert os.environ["CODEX_HOME"] == str(tmp_path / "untouched-codex-home")
    finally:
        one.unlink()
        two.unlink()


def test_launcher_requires_atomic_policy_and_preserves_argv(tmp_path):
    profile = tmp_path / "kirocrew-native-test.json"
    digest = _write_policy(profile)
    original = ["node.exe", "adapter.js", "a & b", 'value"quoted']
    wrapped = sandbox_windows._windows_launcher_argv(
        "codex.exe", profile, digest, str(tmp_path), original
    )
    assert wrapped[1:3] == ["-I", "-S"]
    document = json.loads(Path(wrapped[-2]).read_bytes())
    assert document["launch"]["executable"] == "codex.exe"
    assert document["launch"]["argv"] == original
    assert document["launch"]["cwd"] == str(tmp_path)
    assert document["filesystem"] == {":root": "write"}
    assert hashlib.sha256(profile.read_bytes()).hexdigest() == wrapped[-1]


def test_native_command_line_overflow_refuses_before_launch(tmp_path):
    with pytest.raises(WindowsPolicyUnsupported, match="command limit"):
        sandbox_windows._windows_launcher_argv(
            "codex.exe",
            tmp_path / "test.json",
            "digest",
            str(tmp_path),
            ["node.exe", "😀" * 30000],
        )


def test_windows_environment_scrub_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("Aws_Secret_Access_Key", "synthetic")
    monkeypatch.setenv("Slack_Bot_Token", "synthetic")
    monkeypatch.setenv("KIROCREW_SESSION_ID", "session")
    result = launcher.child_environment(["AWS_"], ["SLACK_BOT_TOKEN"])
    assert not any(key.upper().startswith("AWS_") for key in result)
    assert not any(key.upper() == "SLACK_BOT_TOKEN" for key in result)
    assert result["KIROCREW_SESSION_ID"] == "session"


def test_changed_private_directory_identity_refuses(tmp_path):
    info = tmp_path.stat()
    launcher.verify_identities([[str(tmp_path), info.st_dev, info.st_ino]])
    with pytest.raises(OSError, match="changed"):
        launcher.verify_identities([[str(tmp_path), info.st_dev, info.st_ino + 1]])


@pytest.mark.skipif(os.name != "nt", reason="Win32 sharing flags")
def test_profile_pin_prevents_write_unlink_and_ancestor_rename(tmp_path):
    directory = tmp_path / "profile-home"
    directory.mkdir()
    profile = directory / "test.json"
    profile.write_bytes(b"trusted")
    digest = hashlib.sha256(b"trusted").hexdigest()
    with launcher.pinned_profile(str(profile), digest):
        assert profile.read_bytes() == b"trusted"
        with pytest.raises(OSError):
            profile.write_bytes(b"tampered")
        with pytest.raises(OSError):
            profile.unlink()
        with pytest.raises(OSError):
            directory.rename(tmp_path / "replacement")
    profile.write_bytes(b"released")


@pytest.mark.skipif(os.name != "nt", reason="Win32 sharing flags")
def test_profile_pin_rejects_tampering_and_releases_handles(tmp_path):
    profile = tmp_path / "test.json"
    profile.write_bytes(b"tampered")
    with pytest.raises(OSError, match="changed"):
        with launcher.pinned_profile(str(profile), hashlib.sha256(b"trusted").hexdigest()):
            pytest.fail("tampered profile admitted")
    profile.unlink()


def test_failed_pin_never_invokes_subprocess(monkeypatch, capsys):
    @contextlib.contextmanager
    def refuse(*args):
        raise OSError("synthetic secret must not be logged")
        yield

    def forbidden(*args, **kwargs):
        pytest.fail("unconfined process started")

    monkeypatch.setattr(launcher, "pinned_profile", refuse)
    monkeypatch.setattr(launcher.subprocess, "run", forbidden)
    result = launcher.main({"identities": [], "profile_path": "x", "profile_sha256": "x"})
    assert result == 125
    assert "synthetic secret" not in capsys.readouterr().err


def test_native_failure_status_is_preserved_without_unwrapped_retry(monkeypatch):
    seen = []

    @contextlib.contextmanager
    def pinned(*args):
        yield

    def run(command, **kwargs):
        seen.append(command)
        assert kwargs["env"].get("KIROCREW_SESSION_ID") == "session"
        return SimpleNamespace(returncode=72)

    monkeypatch.setenv("KIROCREW_SESSION_ID", "session")
    monkeypatch.setattr(launcher, "pinned_profile", pinned)
    from types import SimpleNamespace

    monkeypatch.setattr(launcher, "subprocess", SimpleNamespace(run=run))
    payload = {
        "identities": [],
        "profile_path": "x",
        "profile_sha256": "x",
        "command": launcher.windows_native_spawn_argv("runner.exe", "x", "x", None, ["node.exe"]),
        "cwd": None,
        "scrub_prefixes": [],
        "scrub_names": [],
    }
    assert launcher.main(payload) == 72
    assert seen == [payload["command"]]


def test_missing_optin_does_not_probe_or_start_any_process(monkeypatch):
    monkeypatch.delenv(sandbox_windows.WINDOWS_SANDBOX_EXE_ENV, raising=False)
    monkeypatch.setattr(
        sandbox_windows.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("unexpected Windows probe"),
    )
    assert sandbox_windows.probe_windows_sandbox() is False


@pytest.mark.parametrize(
    "unsupported", [{"extra_alias_credential_ids": ((1, 2),)}, {"exec_cwd": "x"}]
)
def test_unsupported_guarantees_refuse_before_materialization(monkeypatch, unsupported):
    monkeypatch.setattr(sandbox_windows, "windows_sandbox_executable", lambda: "codex.exe")
    monkeypatch.setattr(
        sandbox_windows.sandbox_argv,
        "_materialize_sealable_ceilings",
        lambda *args: pytest.fail("materialized an unsupported launch"),
    )
    with pytest.raises(SandboxUnavailableError, match="no process was started"):
        sandbox_windows.windows_sandbox_argv(["node.exe"], **unsupported)


def test_positive_backend_evidence_admits_credential_mask(monkeypatch):
    monkeypatch.setattr(sandbox, "detect_backend", lambda **kwargs: "windows-mxc")
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
    assert sandbox.credential_mask_applies("strict") is True


def test_absent_backend_still_blocks_credential_mask(monkeypatch):
    monkeypatch.setattr(sandbox, "detect_backend", lambda **kwargs: "none")
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
    assert sandbox.credential_mask_applies("strict") is False


@pytest.mark.skipif(os.name != "nt", reason="Win32 sharing flags")
def test_boundary_pin_prevents_replacing_masks_and_private_windows(tmp_path):
    protected = tmp_path / "protected"
    private = protected / "private"
    private.mkdir(parents=True)
    with launcher.pinned_boundaries([str(protected), str(private), str(protected / "future")]):
        (private / "allowed").write_text("ok")
        with pytest.raises(OSError):
            private.rename(protected / "substitute")
        with pytest.raises(OSError):
            protected.rename(tmp_path / "moved")
    private.rename(protected / "released")


@pytest.mark.skipif(os.name != "nt", reason="Win32 sharing flags")
def test_profile_pin_rejects_hardlink_alias(tmp_path):
    profile = tmp_path / "test.json"
    profile.write_bytes(b"trusted")
    try:
        os.link(profile, tmp_path / "alias")
    except OSError as exc:
        if getattr(exc, "winerror", None) == 5:
            pytest.skip("this Windows execution boundary refuses hard-link fixture creation")
        raise
    with pytest.raises(OSError, match="unlinked"):
        with launcher.pinned_profile(str(profile), hashlib.sha256(b"trusted").hexdigest()):
            pytest.fail("hardlinked profile admitted")


@pytest.mark.parametrize("mutate", ["drop_policy", "change_digest", "raw_command"])
def test_launcher_rejects_weakened_native_command(monkeypatch, mutate):
    @contextlib.contextmanager
    def pinned(*args):
        yield

    def forbidden(*args, **kwargs):
        pytest.fail("unsafe command executed")

    monkeypatch.setattr(launcher, "pinned_profile", pinned)
    monkeypatch.setattr(launcher.subprocess, "run", forbidden)
    command = launcher.windows_native_spawn_argv("runner.exe", "x", "x", None, ["node.exe"])
    if mutate == "drop_policy":
        command.remove("--policy-file")
    elif mutate == "change_digest":
        command[command.index("--policy-sha256") + 1] = "changed"
    else:
        command = ["node.exe", "adapter.js"]
    assert (
        launcher.main(
            {
                "identities": [],
                "profile_path": "x",
                "profile_sha256": "digest",
                "command": command,
                "cwd": None,
                "scrub_prefixes": [],
                "scrub_names": [],
            }
        )
        == 125
    )


def test_profile_publish_failure_removes_partial_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_windows, "windows_literal_path", lambda path: path)
    monkeypatch.setattr(sandbox_windows, "_windows_policy_dir", lambda: tmp_path)

    def refuse(*args, **kwargs):
        raise ValueError("unsupported")

    monkeypatch.setattr(sandbox_windows, "render_permissions", refuse)
    with pytest.raises(ValueError):
        sandbox_windows._stage_windows_profile({":root": "write"}, network=True)
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows filesystem policy paths")
def test_windows_builder_carries_every_requested_mask_and_window(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_windows, "_windows_policy_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox_windows, "windows_sandbox_executable", lambda: "codex.exe")
    for name in (
        "_materialize_sealable_ceilings",
        "_materialize_maskable_dirs",
        "_refuse_linked_models_leaf",
        "_sweep_legacy_md_notebook_temps",
        "_migrate_md_notebook_legacy_state",
        "_materialize_md_notebook_mask_targets",
        "_materialize_live_target_mask_target",
        "_sweep_legacy_auth_store_temps",
        "_reconcile_auth_store_staging_links",
    ):
        monkeypatch.setattr(sandbox_windows.sandbox_argv, name, lambda *args: None)
    monkeypatch.setattr(
        sandbox_windows.sandbox_argv,
        "_refuse_multilinked_credential_leaves",
        lambda **kwargs: (),
    )
    monkeypatch.setattr(
        sandbox_windows.sandbox_argv,
        "_refuse_aliased_masked_leaves",
        lambda *args: (),
    )
    seen = {}

    def make_plan(backend, level, **kwargs):
        seen.update(kwargs)
        assert backend == sandbox_plan.BACKEND_WINDOWS and level == "strict"
        return _plan()

    monkeypatch.setattr(sandbox_windows.sandbox_argv, "_spawn_plan", make_plan)
    # This forwarding test uses a synthetic Windows home; filesystem snapshots
    # and missing-parent lowering are covered with isolated tmp_path fixtures.
    monkeypatch.setattr(sandbox_windows, "_windows_policy_path_heads", lambda entries: {})
    monkeypatch.setattr(sandbox_windows, "_windows_file_exposures", lambda plan: (plan, ()))
    info = tmp_path.stat()
    identities = ((str(tmp_path), info.st_dev, info.st_ino),)
    requested = {
        "extra_hidden_dirs": (str(tmp_path),),
        "extra_hidden_dir_ids": identities,
        "extra_visible_dirs": (str(tmp_path / "visible"),),
        "extra_private_dirs": (str(tmp_path / "private"),),
        "extra_private_dir_ids": identities,
        "extra_writable_dirs": (str(tmp_path / "work"),),
        "extra_expose_files": (str(tmp_path / "config"),),
        "seal_hidden_dirs": True,
        "strip_python_env": True,
        "forward_ssh_auth_sock": True,
    }
    wrapped, artifact = sandbox_windows.windows_sandbox_argv(
        ["node.exe", "adapter.js"], **requested
    )
    try:
        assert all(seen[key] == value for key, value in requested.items())
        payload = json.loads(Path(wrapped[-2]).read_bytes())["launch"]
        assert payload["identities"] == [[*identities[0], True], [*identities[0], True]]
        assert "AWS_" in payload["scrub_prefixes"]
        assert payload["argv"] == ["node.exe", "adapter.js"]
        assert payload["protected_paths"]
        assert payload["immutable_roots"]
    finally:
        Path(artifact).unlink()


def test_failed_windows_probe_never_poisons_capability_cache(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sandbox, "_backend", None)
    monkeypatch.setattr(sandbox, "probe_windows_sandbox", lambda: False)
    sandbox._background_warm()
    assert sandbox._backend is None
    assert sandbox._last_unshare_failure[0] is True
    monkeypatch.setattr(sandbox, "probe_windows_sandbox", lambda: True)
    sandbox._background_warm()
    assert sandbox._backend == "windows-mxc"
    assert sandbox._last_unshare_failure is None


@pytest.mark.asyncio
async def test_cold_windows_detection_defers_without_blocking_event_loop(monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sandbox, "_backend", None)
    monkeypatch.setattr(sandbox, "windows_sandbox_executable", lambda: "codex.exe")
    monkeypatch.setattr(sandbox, "_kick_background_warm", lambda: seen.append("deferred"))
    monkeypatch.setattr(
        sandbox,
        "probe_windows_sandbox",
        lambda: pytest.fail("probed on the event loop"),
    )
    assert sandbox.detect_backend("strict") == "none"
    assert seen == ["deferred"]
    assert sandbox._backend is None and sandbox._last_unshare_failure[0] is True


@pytest.mark.parametrize("backend, expected", [("namespace", 1), ("windows-mxc", 2), ("none", 0)])
def test_codex_rss_measurement_counts_resident_windows_wrappers(monkeypatch, backend, expected):
    from kiro_crew.acp.harness import codex as codex_harness

    monkeypatch.setattr(codex_harness, "detect_backend", lambda mode: backend)
    assert codex_harness._sandbox_wrapper_generations("strict") == expected


@pytest.mark.parametrize(
    "status, marker, expected",
    [
        (0, b"KIROCREW_MXC_CONFINEMENT_OK", True),
        (0, b"", False),
        (1, b"KIROCREW_MXC_CONFINEMENT_OK", False),
    ],
)
def test_probe_requires_success_status_and_exact_confinement_marker(
    tmp_path, monkeypatch, status, marker, expected
):
    monkeypatch.setattr(sandbox_windows, "windows_literal_path", lambda path: path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(sandbox_windows, "windows_sandbox_executable", lambda: "codex.exe")
    monkeypatch.setattr(sandbox_windows.sandbox_argv, "_ensure_run_dir", lambda: str(tmp_path))

    class Process:
        returncode = status

        def __init__(self, command, **kwargs):
            payload = json.loads(Path(command[-2]).read_bytes())
            assert command[4] == "--request-file"
            assert payload["launch"]["executable"] == "codex.exe"
            assert hashlib.sha256(Path(command[-2]).read_bytes()).hexdigest() == command[-1]
            assert payload["network"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def communicate(self, **kwargs):
            return marker, b"synthetic"

    monkeypatch.setattr(sandbox_windows.subprocess, "Popen", Process)
    assert sandbox_windows.probe_windows_sandbox() is expected
    assert not list(tmp_path.glob("kirocrew_native_*.json"))
    assert not list(tmp_path.glob("kirocrew-native-probe-*"))


def test_file_exposure_never_turns_a_directory_into_a_readable_window(tmp_path):
    plan = replace(_plan(), expose=((str(tmp_path), "config"),))
    with pytest.raises(WindowsPolicyUnsupported, match="regular files"):
        sandbox_windows._windows_file_exposures(plan)


def test_file_exposure_records_identity_and_omits_missing_sources(tmp_path):
    config = tmp_path / "config"
    config.write_text("synthetic")
    missing = tmp_path / "absent"
    plan, identities = sandbox_windows._windows_file_exposures(
        replace(_plan(), expose=((str(config), "config"), (str(missing), "missing")))
    )
    assert plan.expose == ((str(config), "config"),)
    info = config.stat()
    assert identities == [(str(config), info.st_dev, info.st_ino, False)]


@pytest.mark.skipif(os.name != "nt", reason="Windows filesystem policy paths")
def test_omitted_exposure_leaves_no_future_read_grant(tmp_path):
    missing = tmp_path / "absent-config"
    plan = _plan(extra_hidden_dirs=(str(tmp_path),), extra_expose_files=(str(missing),))
    plan, _ = sandbox_windows._windows_file_exposures(plan)
    entries = permission_entries(plan)
    assert missing.as_posix() not in entries
    assert entries[tmp_path.as_posix()] == "deny"


def test_parent_cleanup_reclaims_all_native_launch_metadata(tmp_path):
    profile = tmp_path / "kirocrew_native_parent_cleanup.json"
    profile.write_text(render_permissions({":root": "write"}, network=False), encoding="utf-8")
    digest = hashlib.sha256(profile.read_bytes()).hexdigest()
    sandbox_windows._windows_launcher_argv(
        "native.exe", profile, digest, str(tmp_path), ["child.exe", "synthetic-private-argument"]
    )
    # Every wrap_argv caller owns one cleanup path, including a cancelled
    # off-loop preparation and a spawn that fails before the launcher starts.
    profile.unlink()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("short_seal", [False, True])
def test_immutable_ancestry_compares_objects_across_alias_spellings(
    tmp_path, monkeypatch, short_seal
):
    root = tmp_path / "immutable-long-name"
    root.mkdir()
    (root / "child").mkdir()
    alias = tmp_path / "IMMUTA~1"
    real_lstat = os.lstat

    def lstat(path):
        path = Path(path)
        if path.is_relative_to(alias):
            path = root / path.relative_to(alias)
        return real_lstat(path)

    monkeypatch.setattr(launcher, "os", SimpleNamespace(lstat=lstat))
    seal, window = (alias, root / "child") if short_seal else (root, alias / "child")
    with pytest.raises(OSError, match="immutable root"):
        launcher.verify_writable_ancestry((str(seal),), (str(window),))


def test_launcher_rechecks_immutable_ancestry_after_pinning_before_spawn(tmp_path, monkeypatch):
    child = tmp_path / "child"
    child.mkdir()
    profile = tmp_path / "kirocrew_native_ancestry.json"
    digest = _write_policy(profile)
    pinned = []

    @contextlib.contextmanager
    def boundaries(paths):
        pinned.append(True)
        try:
            yield
        finally:
            pinned.pop()

    real_verify = launcher.verify_writable_ancestry

    def verify(roots, windows):
        assert pinned
        real_verify(roots, windows)

    monkeypatch.setattr(launcher, "pinned_boundaries", boundaries)
    monkeypatch.setattr(launcher, "pinned_profile", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(launcher, "verify_writable_ancestry", verify)
    monkeypatch.setattr(
        launcher,
        "subprocess",
        SimpleNamespace(run=lambda *a, **kw: pytest.fail("started workload")),
    )
    assert (
        launcher.main(
            {
                "profile_path": str(profile),
                "profile_sha256": digest,
                "identities": [],
                "immutable_roots": [str(tmp_path)],
                "writable_paths": [str(child)],
            }
        )
        == 125
    )


@pytest.mark.skipif(os.name != "nt", reason="Win32 profile pinning")
def test_profile_pin_refuses_multiple_links_reported_by_filesystem(tmp_path, monkeypatch):
    profile = tmp_path / "test.json"
    profile.write_bytes(b"trusted")
    original = Path.lstat

    def lstat(path, **kwargs):
        info = original(path, **kwargs)
        if path == profile:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=info.st_file_attributes,
                st_nlink=2,
            )
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(OSError, match="unlinked"):
        with launcher.pinned_profile(str(profile), hashlib.sha256(b"trusted").hexdigest()):
            pytest.fail("multiple link count admitted")
    profile.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows filesystem policy paths")
def test_timed_out_probe_kills_owned_process_tree_and_removes_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(sandbox_windows, "windows_sandbox_executable", lambda: "codex.exe")
    monkeypatch.setattr(sandbox_windows.sandbox_argv, "_ensure_run_dir", lambda: str(tmp_path))
    seen = []

    class Process:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def communicate(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("synthetic", 20)
            return b"", b""

    monkeypatch.setattr(sandbox_windows.subprocess, "Popen", Process)
    monkeypatch.setattr(
        sandbox_windows.platform_compat, "kill_popen_tree", lambda p: seen.append(p)
    )
    diagnostics = []
    assert sandbox_windows.probe_windows_sandbox(diagnostics=diagnostics) is False
    assert len(seen) == 1 and diagnostics == ["probe-timeout"]
    assert not list(tmp_path.glob("kirocrew_native_*.json"))
    assert not list(tmp_path.glob("kirocrew-native-probe-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows path planning")
@pytest.mark.parametrize(
    "masked", [r"C:\Users\sandbox\.kiro\crew", r"C:\Users\sandbox\.kiro\crew\profiles"]
)
def test_private_window_cannot_reopen_a_governance_seal(masked):
    plan = _plan(
        extra_hidden_dirs=(masked,),
        extra_private_dirs=(r"C:\Users\sandbox\.kiro\crew\profiles\work",),
    )
    assert plan.windows == ()
    assert any(refusal.kind == "readonly-window" for refusal in plan.refusals)


@pytest.mark.parametrize("exists, directory", [(False, False), (True, False), (True, True)])
def test_ssh_known_hosts_is_never_a_directory_window(tmp_path, exists, directory):
    target = tmp_path / "known_hosts"
    if exists:
        if directory:
            target.mkdir()
        else:
            target.write_text("synthetic-host-key")
    source = replace(_plan(), hide_ssh=True, ssh_known_hosts=str(target))
    if directory:
        with pytest.raises(WindowsPolicyUnsupported, match="regular files"):
            sandbox_windows._windows_file_exposures(source)
    else:
        plan, identities = sandbox_windows._windows_file_exposures(source)
        assert bool(plan.ssh_known_hosts) is exists
        assert len(identities) == int(exists)


@pytest.mark.skipif(os.name != "nt", reason="Windows path planning")
@pytest.mark.parametrize(
    "host_field", ["install_root_targets", "relocated_crew_readonly", "tier_dirs"]
)
def test_masked_immutable_root_cannot_grow_a_writable_window(host_field):
    home = r"C:\Users\sandbox"
    sealed = home + r"\policy_cache"
    targets = ("policy_cache",) if host_field == "tier_dirs" else (sealed,)
    host = sandbox_plan.PlanHost(home=home, **{host_field: targets})
    request = sandbox_plan.SandboxRequest(
        tier="cc",
        backend=sandbox_plan.BACKEND_WINDOWS,
        extra_hidden_dirs=(sealed,),
        extra_private_dirs=(sealed + r"\lib",),
    )
    plan = sandbox_plan.plan_confinement(request, host)
    assert plan.windows == ()
    assert any(refusal.kind == "readonly-window" for refusal in plan.refusals)


def test_private_window_cannot_be_a_file(tmp_path):
    path = tmp_path / "file"
    path.write_text("synthetic")
    with pytest.raises(WindowsPolicyUnsupported, match="real directory"):
        sandbox_windows._windows_window_identities(replace(_plan(), windows=(str(path),)))


def test_private_directory_identity_is_always_carried(tmp_path):
    info = tmp_path.stat()
    assert sandbox_windows._windows_window_identities(
        replace(_plan(), windows=(str(tmp_path),))
    ) == [(str(tmp_path), info.st_dev, info.st_ino, True)]


def test_brackets_are_literal_in_native_policy_paths():
    entries = permission_entries(replace(_plan(), readonly=(r"C:\Users\[literal]\policy",)))
    document = json.loads(render_permissions(entries, network=False))
    assert document["filesystem"]["C:/Users/[literal]/policy"] == "read"


def test_base_python_installation_is_immutable_with_a_venv(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    base = tmp_path / "base-python"
    monkeypatch.setattr(sys, "executable", str(venv / "Scripts" / "python.exe"))
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(base))
    roots = sandbox_windows._windows_trusted_roots(str(tmp_path / "runner" / "native.exe"))
    assert str(base) in roots
    assert str(venv) in roots


@pytest.mark.skipif(os.name != "nt", reason="Windows filesystem policy paths")
def test_native_profile_cannot_be_augmented_by_codex_config(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    malicious = codex_home / "config.toml"
    malicious.write_text(
        '[permissions.kirocrew-native.filesystem]\n"C:/Users/sandbox/.aws/credentials"="write"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(sandbox_windows, "_windows_policy_dir", lambda: tmp_path)
    profile, _digest = sandbox_windows._stage_windows_profile(
        {":root": "write", "C:/Users/sandbox/.aws": "deny"}, network=True
    )
    try:
        document = json.loads(profile.read_text())
        assert "C:/Users/sandbox/.aws/credentials" not in document["filesystem"]
        assert document["filesystem"]["C:/Users/sandbox/.aws"] == "deny"
        assert "permissions" not in document
    finally:
        profile.unlink()


def test_native_wrapper_ownership_is_recorded_without_granting_passthrough():
    from pathlib import Path

    from kiro_crew import sandbox_delegation

    launcher = str(Path(sandbox_windows.__file__).with_name("sandbox_windows_launcher.py"))
    argv = [sys.executable, "-I", "-S", launcher, "{}"]
    assert sandbox_delegation.wrapped_by_crew_sandbox(argv)
    assert not sandbox_delegation.wrapped_by_crew_sandbox(
        [sys.executable, "-c", "pass", launcher, "{}"]
    )


def test_native_launcher_uses_the_actual_callers_working_directory(tmp_path, monkeypatch):
    import hashlib

    from kiro_crew import sandbox_windows_launcher as launcher

    profile = tmp_path / "kirocrew_native_test.json"
    profile.write_bytes(b"{}")
    digest = hashlib.sha256(b"{}").hexdigest()
    actual = tmp_path / "actual-cwd"
    actual.mkdir()
    monkeypatch.chdir(actual)
    command = launcher.windows_native_spawn_argv(
        "native.exe", str(profile), digest, str(tmp_path), ["child.exe"]
    )
    captured = {}

    def run(args, **kwargs):
        captured.update(args=args, **kwargs)
        return type("Result", (), {"returncode": 0})()

    from types import SimpleNamespace

    monkeypatch.setattr(launcher, "subprocess", SimpleNamespace(run=run))
    monkeypatch.setattr(launcher, "pinned_profile", lambda *args: contextlib.nullcontext())
    payload = dict(
        profile_path=str(profile),
        profile_sha256=digest,
        protected_paths=[],
        identities=[],
        command=command,
        cwd=str(tmp_path),
        inherit_cwd=True,
        scrub_prefixes=[],
        scrub_names=[],
    )
    assert launcher.main(payload) == 0
    assert captured["cwd"] == str(actual)
    assert captured["args"][6] == str(actual)


@pytest.mark.skipif(os.name != "nt", reason="Windows atomic rename publication")
def test_ceiling_publication_is_complete_and_does_not_replace_a_winner(tmp_path):
    from kiro_crew import sandbox

    target = tmp_path / "ceiling.json"
    assert sandbox._publish_empty_ceiling(str(target), str(tmp_path), b'{"first":true}')
    assert target.read_bytes() == b'{"first":true}'
    assert not sandbox._publish_empty_ceiling(str(target), str(tmp_path), b'{"other":true}')
    assert target.read_bytes() == b'{"first":true}'


def test_large_launch_metadata_uses_a_short_authenticated_request(tmp_path):
    import hashlib
    from pathlib import Path

    profile = tmp_path / "kirocrew_native_metadata.json"
    digest = _write_policy(profile)
    protected = tuple(str(tmp_path / ("protected-" + str(i))) for i in range(1500))
    wrapped = sandbox_windows._windows_launcher_argv(
        "native.exe", profile, digest, str(tmp_path), ["child.exe"], protected_paths=protected
    )
    assert wrapped[4] == "--request-file"
    request = Path(wrapped[5])
    contents = request.read_bytes()
    assert len(contents) > 32767
    assert hashlib.sha256(contents).hexdigest() == wrapped[6]
    assert len(" ".join(wrapped)) < 4000
    assert json.loads(contents)["launch"]["protected_paths"] == list(protected)


def test_request_tampering_cannot_start_a_workload(tmp_path, monkeypatch):
    from kiro_crew import sandbox_windows_launcher as launcher

    request = tmp_path / "kirocrew_native_tampered.request.json"
    request.write_text('{"command": []}')
    called = []
    monkeypatch.setattr(launcher, "main", lambda payload: called.append(payload) or 0)
    assert launcher.entrypoint(["--request-file", str(request), "wrong"]) != 0
    assert called == []


@pytest.mark.skipif(os.name != "nt", reason="Win32 profile pinning")
def test_request_reconstructs_authenticated_native_command_and_cleans_up(tmp_path, monkeypatch):
    profile = tmp_path / "kirocrew_native_valid.json"
    digest = _write_policy(profile)
    wrapped = sandbox_windows._windows_launcher_argv(
        "native.exe", profile, digest, str(tmp_path), ["child.exe", "& literal"]
    )
    seen = []
    monkeypatch.setattr(launcher, "main", lambda payload: seen.append(payload) or 0)
    assert launcher.entrypoint(wrapped[4:]) == 0
    assert seen[0]["command"] == launcher.windows_native_spawn_argv(
        "native.exe", str(profile), wrapped[-1], str(tmp_path), ["child.exe", "& literal"]
    )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows path planning")
def test_adapter_credential_roster_cannot_hide_governance_floors():
    target = r"C:\Users\sandbox\.kiro\crew\security_policy.json"
    plan = _plan(extra_hidden_dirs=(target, target.upper()))
    entries = permission_entries(plan)
    assert entries["C:/Users/sandbox/.kiro/crew/security_policy.json"] == "read"
    assert entries["C:/Users/sandbox/.kiro/crew/.env"] == "deny"
    assert all(
        mask.read_only_when_cancelled for mask in plan.masks if mask.path.lower() == target.lower()
    )


def test_stock_npm_windows_shim_resolves_to_its_node_entry(tmp_path):
    from kiro_crew.acp.node_adapter_cmd import npm_cmd_entry

    entry = tmp_path / "node_modules" / "adapter" / "dist" / "index.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("console.log('fixture')")
    shim = tmp_path / "adapter.cmd"
    shim.write_text('"\x25_prog\x25"  "\x25dp0\x25\\node_modules\\adapter\\dist\\index.js" \x25*\n')
    assert npm_cmd_entry(str(shim)) == entry.resolve()


def test_custom_batch_command_is_not_reinterpreted_as_an_npm_shim(tmp_path):
    from kiro_crew.acp.node_adapter_cmd import npm_cmd_entry

    shim = tmp_path / "custom.cmd"
    shim.write_text("echo custom\n")
    assert npm_cmd_entry(str(shim)) is None


@pytest.mark.parametrize("access", ["deny", "read"])
def test_missing_policy_parent_is_sealed_without_creating_host_directories(tmp_path, access):
    missing = tmp_path / "future-provider"
    target = missing / "nested" / "token"
    existing = tmp_path / "policy.json"
    existing.write_text("immutable", encoding="utf-8")
    visible = tmp_path / "workspace"
    visible.mkdir()
    entries = {
        ":root": "write",
        target.as_posix(): access,
        existing.as_posix(): "read",
        visible.as_posix(): "write",
    }
    heads = sandbox_windows._windows_policy_path_heads(entries)
    projected = sandbox_plan.project_windows_permissions(entries, heads)
    assert projected[missing.as_posix()] == access
    assert target.as_posix() not in projected
    assert projected[existing.as_posix()] == "read"
    assert projected[visible.as_posix()] == "write"
    assert not missing.exists()


def test_missing_leaf_under_existing_parent_keeps_its_exact_rule(tmp_path):
    target = tmp_path / "future-secret"
    entries = {":root": "write", target.as_posix(): "deny"}
    heads = sandbox_windows._windows_policy_path_heads(entries)
    assert sandbox_plan.project_windows_permissions(entries, heads) == entries


def test_missing_windows_descendants_coalesce_to_hidden_without_losing_existing_governance():
    root = "C:/home/absent"
    existing = "C:/home/security_policy.json"
    entries = {
        ":root": "write",
        root + "/credential/token": "deny",
        root + "/governance/config": "read",
        existing: "read",
    }
    heads = {root + "/credential/token": root, root + "/governance/config": root}
    result = sandbox_plan.project_windows_permissions(entries, heads)
    assert result == {":root": "write", root: "deny", existing: "read"}


def test_missing_writable_window_cannot_expand_to_its_parent():
    target = "C:/home/absent/work"
    with pytest.raises(ValueError, match="Cannot broaden"):
        sandbox_plan.project_windows_permissions(
            {":root": "write", target: "write"}, {target: "C:/home/absent"}
        )


def test_windows_snapshot_cannot_redirect_a_mask_to_an_unrelated_directory():
    target = "C:/home/absent/secret"
    with pytest.raises(ValueError, match="does not name an ancestor"):
        sandbox_plan.project_windows_permissions(
            {":root": "write", target: "deny"}, {target: "C:/elsewhere"}
        )
