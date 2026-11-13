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

    def test_an_app_tree_with_no_data_dir_refuses_and_creates_nothing(self, home):
        """``data/`` is the gateway's to create: the lifecycle moves it aside and back.

        A spawn that went ahead would hold the directory unmasked once ``data/`` arrives.
        """
        _install(home, ["state/"], data=False)
        with pytest.raises(SandboxCeilingUnsealable, match="data directory is missing"):
            sandbox._materialize_declared_mask_dirs()
        assert not (home / "apps" / "demo" / "data").exists()

    def test_with_nothing_of_the_app_on_disk_the_entry_is_skipped(self, home):
        """A retained declaration whose app has no tree and no data has nothing to mask yet."""
        (home / "apps").mkdir()
        credential_masks.record_path("demo").write_text(
            json.dumps({"name": "demo", "credentialMasks": ["state/"]}), encoding="utf-8"
        )
        assert credential_masks.declared_crew_dir_leaves()
        established: list[str] = []
        assert sandbox._materialize_declared_mask_dirs(established) == [] and established == []
        assert sorted(p.name for p in (home / "apps").iterdir()) == [".demo-credential-masks.json"]

    def test_while_the_data_is_moved_aside_the_directory_is_created_there(self, home):
        """The move-aside spelling holds the data mid-update; it is moved back as a whole."""
        _install(home, ["state/"], data=False)
        staged = home / "apps" / ".demo-data-tmp"
        staged.mkdir()
        established: list[str] = []
        created = sandbox._materialize_declared_mask_dirs(established)
        assert created == [str(staged / "state")] and established == created
        assert (staged / "state").is_dir()
        assert not (home / "apps" / "demo" / "data").exists()

    def test_with_data_at_both_spellings_each_gets_the_directory(self, home):
        """Either copy can be the one the lifecycle moves back over ``data/``."""
        data = _install(home, ["state/"])
        staged = home / "apps" / ".demo-data-tmp"
        staged.mkdir()
        established: list[str] = []
        created = sandbox._materialize_declared_mask_dirs(established)
        assert sorted(created) == sorted([str(data / "state"), str(staged / "state")])
        assert sorted(established) == sorted(created)

    def test_a_spawn_mid_install_before_the_data_dir_exists_refuses(
        self, home, tmp_path, monkeypatch
    ):
        """The tree is published before ``app_data_dir`` runs, and discovery reads the tree."""
        from kiro_crew.apps import manager
        from kiro_crew.apps.manager import install_app

        (home / "config.json").write_text(
            json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
        )
        outcomes: list[str] = []
        real_data_dir = manager.app_data_dir

        def spawn_then_create(name: str) -> Path:
            try:
                sandbox._materialize_declared_mask_dirs()
            except SandboxCeilingUnsealable as exc:
                outcomes.append(str(exc))
            else:
                outcomes.append("launched")
            return real_data_dir(name)

        monkeypatch.setattr(manager, "app_data_dir", spawn_then_create)
        result = install_app(_make_app_source(tmp_path, name="demo", credentialMasks=["state/"]))
        assert result.ok, result.error
        assert len(outcomes) == 1 and "data directory is missing" in outcomes[0], outcomes
        established: list[str] = []
        data = home / "apps" / "demo" / "data"
        assert sandbox._materialize_declared_mask_dirs(established) == [str(data / "state")]

    def test_an_install_whose_record_write_fails_leaves_spawns_working(
        self, home, tmp_path, monkeypatch
    ):
        """``data/`` is made before the record writes, so a failed one leaves no refusal."""
        from kiro_crew.apps import manager
        from kiro_crew.apps.manager import install_app

        (home / "config.json").write_text(
            json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
        )

        def disk_full(*_args: object, **_kwargs: object) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(manager, "_write_installed", disk_full)
        result = install_app(_make_app_source(tmp_path, name="demo", credentialMasks=["state/"]))
        assert not result.ok and "No space left" in (result.error or ""), result
        assert (home / "apps" / "demo" / "app.json").is_file()
        established: list[str] = []
        data = home / "apps" / "demo" / "data"
        assert sandbox._materialize_declared_mask_dirs(established) == [str(data / "state")]
        assert established == [str(data / "state")]

    def test_a_spawn_mid_update_masks_the_directory_that_comes_back(
        self, home, tmp_path, monkeypatch
    ):
        """The update moves ``data/`` aside, copies the new tree, then moves it back."""
        from kiro_crew.apps import manager
        from kiro_crew.apps.manager import install_app, update_app

        (home / "config.json").write_text(
            json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
        )
        result = install_app(_make_app_source(tmp_path, name="demo", credentialMasks=["state/"]))
        assert result.ok, result.error
        created_mid_copy: list[list[str]] = []
        real_copy = manager._copy_app_tree

        def spawn_then_copy(source: Path, dest: Path) -> None:
            created_mid_copy.append(sandbox._materialize_declared_mask_dirs())
            real_copy(source, dest)

        monkeypatch.setattr(manager, "_copy_app_tree", spawn_then_copy)
        newer = _make_app_source(
            tmp_path / "v2", name="demo", version="1.1.0", credentialMasks=["state/"]
        )
        result = update_app(newer)
        assert result.ok, result.error
        staged_state = str(home / "apps" / ".demo-data-tmp" / "state")
        assert created_mid_copy == [[staged_state]]
        assert (home / "apps" / "demo" / "data" / "state").is_dir()

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

    def test_a_file_at_the_data_dir_refuses(self, home):
        _install(home, ["state/"], data=False)
        (home / "apps" / "demo" / "data").write_text("x", encoding="utf-8")
        with pytest.raises(SandboxCeilingUnsealable, match="data directory is missing"):
            sandbox._materialize_declared_mask_dirs()

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
