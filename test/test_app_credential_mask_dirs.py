"""A ``credentialMasks`` entry ending in ``/`` masks a directory under the app's data dir.

The directory is masked whole, so a file the app's backend writes there by temp and
rename stays hidden from every other sandboxed process, including one that was already
running. These tests pin the grammar, the plan, the spawn-prep checks and the backend's
lift, and run the real Linux namespace sandbox for the two properties only a mount
namespace shows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from app_manager_helpers import _make_app_source

from kiro_crew import sandbox, sandbox_plan
from kiro_crew.apps import credential_masks
from kiro_crew.sandbox import SandboxCeilingUnsealable, userns_available
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox only")
_NAMESPACE = [
    pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only"),
    pytest.mark.skipif(
        not userns_available(),
        reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
        "namespaces)",
    ),
]


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    # The plan masks from the resolved data home, so every compared path is resolved too,
    # and ``$HOME`` is pinned so no step touches the developer's real data home.
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    monkeypatch.setattr(credential_masks, "config_dir", lambda: home)
    credential_masks.clear_cache()
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()
    credential_masks.clear_cache()


def _install(home: Path, masks: list[str], *, name: str = "demo", data: bool = True) -> Path:
    app = home / "apps" / name
    app.mkdir(parents=True, exist_ok=True)
    if data:
        (app / "data").mkdir(exist_ok=True)
    manifest = {"name": name, "version": "1.0.0", "credentialMasks": masks}
    (app / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    return app / "data"


class TestTheGrammar:
    def test_one_name_ending_in_a_slash_is_a_directory(self):
        assert credential_masks.entry_error("state/") is None
        assert credential_masks.is_directory_entry("state/")
        assert not credential_masks.is_directory_entry("state")
        assert credential_masks.entry_path("state/") == "state"
        assert credential_masks.crew_leaf("demo", "state/") == "apps/demo/data/state"
        assert credential_masks.staged_crew_leaf("demo", "state/") == "apps/.demo-data-tmp/state"

    @pytest.mark.parametrize("entry", ["a/b/", "/", "state//", "./", "../", " state/"])
    def test_anything_else_ending_in_a_slash_is_refused(self, entry):
        assert credential_masks.entry_error(entry) is not None

    @pytest.mark.parametrize(
        "masks", [["state/", "state/pat"], ["state/", "state"], ["pat", "state/", "state/x/y"]]
    )
    def test_an_entry_a_directory_entry_covers_is_refused(self, masks):
        errors = credential_masks.declaration_errors(masks)
        assert errors and "beneath the directory entry" in errors[-1], errors

    def test_at_runtime_the_covered_entry_is_dropped_and_the_directory_kept(self):
        assert credential_masks.valid_entries(["state/pat", "state/", "pat"]) == ("state/", "pat")

    def test_the_directory_leaves_are_listed_at_both_spellings(self, home):
        _install(home, ["state/", "token.json"])
        assert credential_masks.declared_crew_dir_leaves() == {
            "apps/demo/data/state",
            "apps/.demo-data-tmp/state",
        }
        assert "apps/demo/data/token.json" in credential_masks.declared_crew_leaves()


@_POSIX_ONLY
class TestThePlanMasksTheDirectory:
    def test_linux_masks_the_directory_in_every_mode(self, home):
        _install(home, ["state/"])
        for mode in ("standard", "cc", "strict"):
            plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, mode)
            assert str(home / "apps" / "demo" / "data" / "state") in set(plan.sensitive_dirs)

    def test_macos_denies_reads_under_the_directory(self, home):
        _install(home, ["state/"])
        profile = sandbox._build_seatbelt_profile("standard")
        state = home / "apps" / "demo" / "data" / "state"
        assert f'(deny file-read* (subpath "{state}"))' in profile


@_POSIX_ONLY
class TestSpawnPrep:
    def test_an_absent_directory_is_created_under_an_existing_data_dir(self, home):
        data = _install(home, ["state/"])
        established: list[str] = []
        created = sandbox._materialize_declared_mask_dirs(established)
        assert created == [str(data / "state")] and established == created
        assert (data / "state").is_dir() and not (data / "state").is_symlink()
        assert oct((data / "state").stat().st_mode & 0o777) == oct(0o700)
        # Nothing is made at the move-aside spelling.
        assert not (home / "apps" / ".demo-data-tmp").exists()

    def test_without_a_data_dir_nothing_is_created(self, home):
        """``data/`` is the gateway's to create: the lifecycle moves it aside and back."""
        _install(home, ["state/"], data=False)
        established: list[str] = []
        assert sandbox._materialize_declared_mask_dirs(established) == [] and established == []
        assert not (home / "apps" / "demo" / "data").exists()

    def test_a_spawn_inside_a_keep_data_uninstall_leaves_the_kept_data_whole(
        self, home, tmp_path, monkeypatch
    ):
        """The uninstall moves ``data/`` back by rename once the app tree is gone.

        A spawn in that window must not make a ``data/`` for the move to land inside.
        """
        from kiro_crew.apps import manager_uninstall
        from kiro_crew.apps.manager import install_app, uninstall_app

        (home / "config.json").write_text(
            json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
        )
        result = install_app(_make_app_source(tmp_path, name="demo", credentialMasks=["state/"]))
        assert result.ok, result.error
        data = home / "apps" / "demo" / "data"
        (data / "state").mkdir()
        (data / "state" / "pat").write_text("token", encoding="utf-8")
        removed_then_spawned: list[list[str]] = []
        remove_tree = manager_uninstall._rmtree_app_dir

        def remove_then_spawn(dest: Path) -> None:
            remove_tree(dest)
            removed_then_spawned.append(sandbox._materialize_declared_mask_dirs())

        monkeypatch.setattr(manager_uninstall, "_rmtree_app_dir", remove_then_spawn)
        assert uninstall_app("demo", keep_data=True).ok
        assert removed_then_spawned == [[]]
        assert (data / "state" / "pat").read_text(encoding="utf-8") == "token"
        assert not (data / ".demo-data-tmp").exists()

    def test_a_file_above_the_name_leaves_nothing_to_create(self, home):
        _install(home, ["state/"], data=False)
        (home / "apps" / "demo" / "data").write_text("x", encoding="utf-8")
        established: list[str] = []
        assert sandbox._materialize_declared_mask_dirs(established) == [] and established == []

    def test_an_existing_directory_is_kept_and_required(self, home):
        data = _install(home, ["state/"])
        (data / "state").mkdir()
        (data / "state" / "pat").write_text("token", encoding="utf-8")
        established: list[str] = []
        assert sandbox._materialize_declared_mask_dirs(established) == []
        assert established == [str(data / "state")]
        assert (data / "state" / "pat").read_text(encoding="utf-8") == "token"

    def test_a_link_in_the_chain_refuses(self, home, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / "data").mkdir(parents=True)
        _install(home, ["state/"], data=False)
        (home / "apps" / "demo" / "data").symlink_to(elsewhere / "data")
        with pytest.raises(SandboxCeilingUnsealable, match="LINK"):
            sandbox._materialize_declared_mask_dirs()
        assert not (elsewhere / "data" / "state").exists()

    def test_a_file_at_a_directory_entry_refuses_the_spawn(self, home):
        data = _install(home, ["state/"])
        (data / "state").write_text("not a directory", encoding="utf-8")
        with pytest.raises(SandboxCeilingUnsealable, match="not a DIRECTORY"):
            sandbox._refuse_aliased_masked_leaves()

    def test_a_link_at_a_directory_entry_refuses_the_spawn(self, home, tmp_path):
        data = _install(home, ["state/"])
        (tmp_path / "planted").mkdir()
        (data / "state").symlink_to(tmp_path / "planted")
        with pytest.raises(SandboxCeilingUnsealable, match="SYMLINK"):
            sandbox._refuse_aliased_masked_leaves()

    def test_a_real_directory_at_a_directory_entry_passes(self, home):
        data = _install(home, ["state/"])
        (data / "state").mkdir()
        sandbox._refuse_aliased_masked_leaves()

    def test_a_directory_at_a_file_entry_still_refuses(self, home):
        data = _install(home, ["state"])
        (data / "state").mkdir()
        with pytest.raises(SandboxCeilingUnsealable, match="must name a file"):
            sandbox._refuse_aliased_masked_leaves()


@_POSIX_ONLY
class TestTheBackendGetsItsDirectoryBack:
    def test_an_absent_or_real_directory_is_lifted(self, home):
        data = _install(home, ["state/"])
        target = str(data / "state")
        assert target in sandbox.app_declared_mask_targets("demo", builtin=False)
        (data / "state").mkdir()
        assert target in sandbox.app_declared_mask_targets("demo", builtin=False)

    @pytest.mark.parametrize("kind", ["file", "link"])
    def test_anything_else_stays_masked(self, home, tmp_path, kind):
        data = _install(home, ["state/"])
        if kind == "file":
            (data / "state").write_text("x", encoding="utf-8")
        else:
            (tmp_path / "planted").mkdir()
            (data / "state").symlink_to(tmp_path / "planted")
        assert str(data / "state") not in sandbox.app_declared_mask_targets("demo", builtin=False)


# The agent's side: report what the masked directory holds, once at start and once more
# after the test (playing the backend) has written into it.
_AGENT = r"""
import errno, json, os, sys

state = sys.argv[1]

def listing():
    try:
        return sorted(os.listdir(state))
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))

first = listing()
print("READY", flush=True)
sys.stdin.readline()
report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "first": first,
    "after": listing(),
}
print("REPORT " + json.dumps(report), flush=True)
"""

# The backend's side: write a credential the way a careful writer does, temp then rename.
_BACKEND = r"""
import json, os, sys

state = sys.argv[1]
tmp = os.path.join(state, "pat.tmp")
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write("ghp_written_inside")
os.replace(tmp, os.path.join(state, "pat"))
print("REPORT " + json.dumps({"listing": sorted(os.listdir(state)), "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE")}), flush=True)
"""


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}


def _report(stdout: str) -> dict:
    (line,) = [ln for ln in stdout.splitlines() if ln.startswith("REPORT ")]
    return json.loads(line[len("REPORT ") :])


class TestInTheNamespaceSandbox:
    pytestmark = _NAMESPACE

    def test_a_running_agent_never_sees_what_the_backend_writes_later(self, home):
        """The directory is absent before the agent starts; spawn prep creates it."""
        data = _install(home, ["state/"])
        state = data / "state"
        agent = subprocess.Popen(
            sandbox.namespace_argv([sys.executable, "-c", _AGENT, str(state)], "standard"),
            cwd=home,
            env=_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            assert agent.stdout is not None
            assert agent.stdout.readline().strip() == "READY", agent.stderr
            assert state.is_dir(), "spawn prep did not create the declared directory"
            lifted = sandbox.app_declared_mask_targets("demo", builtin=False)
            backend = subprocess.run(
                sandbox.namespace_argv(
                    [sys.executable, "-c", _BACKEND, str(state)],
                    "standard",
                    extra_visible_dirs=lifted,
                ),
                cwd=home,
                env=_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=FALLBACK_TIMEOUT_SECS,
            )
            assert backend.returncode == 0, backend.stdout + backend.stderr
            assert _report(backend.stdout) == {"listing": ["pat"], "marker": "1"}
            out, err = agent.communicate("go\n", timeout=FALLBACK_TIMEOUT_SECS)
        finally:
            if agent.poll() is None:
                agent.kill()
                agent.communicate(timeout=FALLBACK_TIMEOUT_SECS)
        assert agent.returncode == 0, err
        report = _report(out)
        assert report["marker"] == "1" and report["mnt_ns"] != os.readlink("/proc/self/ns/mnt")
        assert report["first"] == [] and report["after"] == [], report
        assert (state / "pat").read_text(encoding="utf-8") == "ghp_written_inside"

    def test_a_new_agent_sees_an_empty_directory(self, home):
        data = _install(home, ["state/"])
        (data / "state").mkdir()
        (data / "state" / "pat").write_text("ghp_secret", encoding="utf-8")
        agent = subprocess.run(
            sandbox.namespace_argv([sys.executable, "-c", _AGENT, str(data / "state")], "standard"),
            cwd=home,
            env=_env(),
            input="go\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=FALLBACK_TIMEOUT_SECS,
        )
        assert agent.returncode == 0, agent.stderr
        report = _report(agent.stdout)
        assert report["marker"] == "1"
        assert report["first"] == [] and report["after"] == [], report
