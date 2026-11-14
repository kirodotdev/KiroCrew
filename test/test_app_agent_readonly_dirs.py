"""App-declared agent-read-only directories: ``agentReadOnlyDirs`` in ``app.json``.

An app lists directories under its OWN data directory (``apps/<name>/data/``) and agents
may read them but never write them: the file-edit tool gate refuses a write under each
one, and every sandbox mode seals it read-only like a core ``READONLY`` crew-home leaf.
The app's own backend keeps write access. These cases pin the manifest field, where the
declarations are read, the tool gate, the Linux plan and the Seatbelt profile, the link
refusal, the backend's own lift, the record that outlives the manifest, and the negative
half: an app cannot fence outside its own data directory and cannot unfence a core leaf.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 -- requested by name
from app_manager_helpers import _make_app_source
from sandbox_absent_ceiling_seal_helpers import (  # noqa: F401 -- fixtures are requested by name
    _no_host_ssh_probe,
    crew_home_fixture,
)

from kiro_crew import sandbox, sandbox_plan, security
from kiro_crew.apps import agent_readonly_dirs, credential_masks
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.security import paths_targets
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox only")
_MODES = ("standard", "cc", "strict")
_PREFIXES = (".kiro/crew", ".kirocrew")


@pytest.fixture(autouse=True)
def _fresh_cache():
    credential_masks.clear_cache()
    yield
    credential_masks.clear_cache()


@pytest.fixture(name="home")
def home_fixture(crew_home, monkeypatch):
    """The scratch data home, seen by the declaration reader as well as by the sandbox."""
    monkeypatch.setattr(credential_masks, "config_dir", lambda: crew_home)
    return crew_home


def _install(home: Path, directory: str, dirs: object, *, name: str | None = None) -> Path:
    """Write ``apps/<directory>/app.json`` declaring *dirs*; return the app's data dir."""
    app = home / "apps" / directory
    (app / "data").mkdir(parents=True, exist_ok=True)
    manifest = {"name": name or directory, "version": "1.0.0", "agentReadOnlyDirs": dirs}
    (app / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    return app / "data"


def _targets(leaf: str) -> set[str]:
    home = str(Path.home())
    return {os.path.join(home, f"{prefix}/{leaf}") for prefix in _PREFIXES}


def _installed_leaves() -> tuple[str, ...]:
    """The declared leaves of the installed trees only (the built-ins declare their own)."""
    return tuple(
        sorted(
            leaf
            for app, entries in agent_readonly_dirs.declared_dirs(builtin=False).items()
            for entry in entries
            for leaf in (
                credential_masks.crew_leaf(app, entry),
                credential_masks.staged_crew_leaf(app, entry),
            )
        )
    )


class TestTheManifestField:
    def test_it_round_trips_validates_and_is_signed(self):
        manifest = AppManifest.from_dict(
            {"name": "demo", "version": "1.0.0", "agentReadOnlyDirs": ["notes", "trust"]}
        )
        assert manifest.agentReadOnlyDirs == ["notes", "trust"]
        assert manifest.to_dict()["agentReadOnlyDirs"] == ["notes", "trust"]
        assert "agentReadOnlyDirs" not in manifest.extra
        assert json.loads(manifest.signing_payload())["agentReadOnlyDirs"] == ["notes", "trust"]
        plain = AppManifest.from_dict({"name": "demo", "version": "1.0.0"})
        assert b"agentReadOnlyDirs" not in plain.signing_payload()
        empty = AppManifest.from_dict({"name": "demo", "version": "1.0.0", "agentReadOnlyDirs": []})
        assert empty.signing_payload() == plain.signing_payload()

    @pytest.mark.parametrize(
        "entry", ["../other/data/x", "/etc", "a/../../b", "", ".", "a\\b", "c:x", "a/./b"]
    )
    def test_an_entry_that_could_leave_the_data_dir_refuses_the_manifest(self, entry):
        manifest = AppManifest.from_dict(
            {"name": "demo", "version": "1.0.0", "agentReadOnlyDirs": [entry, "ok"]}
        )
        assert manifest.agentReadOnlyDirs == ["ok"]
        assert any("agentReadOnlyDirs[0]" in error for error in manifest.validate())

    def test_a_non_list_refuses_the_manifest(self):
        manifest = AppManifest.from_dict(
            {"name": "demo", "version": "1.0.0", "agentReadOnlyDirs": "notes"}
        )
        assert manifest.agentReadOnlyDirs == []
        assert "agentReadOnlyDirs must be a list of paths" in manifest.validate()

    @pytest.mark.parametrize("entry", ["private", "private/inner"])
    def test_a_dir_at_or_beneath_a_credential_mask_refuses_the_manifest(self, entry):
        """A mask names a file, so a read-only directory there would refuse every spawn."""
        manifest = AppManifest.from_dict(
            {
                "name": "demo",
                "version": "1.0.0",
                "credentialMasks": ["private", "d/s.json"],
                "agentReadOnlyDirs": [entry, "d"],
            }
        )
        errors = [e for e in manifest.validate() if "credentialMasks entry" in e]
        assert errors == [
            "agentReadOnlyDirs[0] must not be or sit beneath a credentialMasks entry (private)"
        ]

    @pytest.mark.parametrize("entry", ["state", "state/inner"])
    def test_a_dir_at_or_beneath_a_masked_directory_refuses_the_manifest(self, entry):
        """A masked directory hides what is under it, so a read-only seal there fences nothing."""
        manifest = AppManifest.from_dict(
            {
                "name": "demo",
                "version": "1.0.0",
                "credentialMasks": ["state/"],
                "agentReadOnlyDirs": [entry, "d"],
            }
        )
        errors = [e for e in manifest.validate() if "credentialMasks entry" in e]
        assert errors == [
            "agentReadOnlyDirs[0] must not be or sit beneath a credentialMasks entry (state/)"
        ]

    def test_a_trailing_slash_is_not_a_read_only_directory_form(self):
        """Only ``credentialMasks`` has directory entries; here ``/`` is an empty segment."""
        manifest = AppManifest.from_dict(
            {"name": "demo", "version": "1.0.0", "agentReadOnlyDirs": ["notes/", "ok"]}
        )
        assert manifest.agentReadOnlyDirs == ["ok"]
        assert any(
            e.startswith("agentReadOnlyDirs[0] must not contain an empty")
            for e in manifest.validate()
        )

    def test_both_built_ins_declare_the_directories_core_used_to_fence(self):
        assert agent_readonly_dirs.declared_dirs(installed=False) == {
            "meetings": ("notes",),
            "spec-builder": ("trust",),
        }


class TestWhereDeclarationsAreRead:
    def test_an_installed_app_declares_dirs_under_its_own_data_dir(self, home):
        _install(home, "demo", ["notes"])
        assert agent_readonly_dirs.declared_dirs(builtin=False) == {"demo": ("notes",)}

    def test_the_two_declarations_never_read_each_other(self, home):
        _install(home, "demo", ["notes"])
        assert credential_masks.declared_masks(builtin=False) == {}

    def test_a_declaration_cannot_reach_another_apps_data(self, home):
        """The negative case: every spelling an app has stays inside its own data dir."""
        _install(home, "victim", [])
        _install(home, "evil", ["../../victim/data/notes", "/etc", "mine"])
        _install(home, "impostor", ["notes"], name="victim")

        assert _installed_leaves() == ("apps/.evil-data-tmp/mine", "apps/evil/data/mine")


class TestTheToolGateRefusesAgentWrites:
    def test_a_declared_dir_is_write_fenced_and_readable(self, home, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        _install(home, "demo", ["notes"])
        inside = home / "apps" / "demo" / "data" / "notes" / "n.md"
        sibling = home / "apps" / "demo" / "data" / "other.md"

        assert security.is_sensitive_write_path(str(inside))
        assert security.is_sensitive_write_path(str(inside.parent))
        assert not security.is_sensitive_path(str(inside)), "reads stay open"
        assert not security.is_sensitive_write_path(str(sibling))

    @pytest.mark.parametrize("prefix", _PREFIXES)
    def test_the_built_in_rows_core_used_to_carry_still_fence(self, prefix):
        for leaf in ("apps/meetings/data/notes/m/note.md", "apps/spec-builder/data/trust/x"):
            assert security.is_sensitive_write_path(f"~/{prefix}/{leaf}")


class TestTheDeclarationsAreListedWithinADeadline:
    """Every caller takes the rows from a bounded listing; the event loop also reuses it."""

    @pytest.fixture(autouse=True)
    def _fresh_listing(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(paths_targets, "_app_readonly_rows_cache", None)
        _floor_monkeypatch.setattr(paths_targets, "_app_readonly_rows_pending", None)
        self.now = 1000.0
        _floor_monkeypatch.setattr(paths_targets, "_app_readonly_rows_clock", lambda: self.now)

    def _counting_listing(self, monkeypatch, *, gate: threading.Event | None = None):
        listed = paths_targets._list_app_readonly_dir_rows
        threads: list[int] = []

        def listing():
            threads.append(threading.get_ident())
            if gate is not None:
                assert gate.wait(FALLBACK_TIMEOUT_SECS)
            return listed()

        monkeypatch.setattr(paths_targets, "_list_app_readonly_dir_rows", listing)
        return threads

    @staticmethod
    def _let_the_listing_finish():
        # The pool may start a held listing only after every check has refused, so count
        # its starts once it has run.
        pending = paths_targets._app_readonly_rows_pending
        if pending is not None:
            pending[1].result(FALLBACK_TIMEOUT_SECS)

    def test_the_listing_runs_off_the_loop_and_is_reused_until_it_expires(self, home, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        inside = str(_install(home, "demo", ["notes"]) / "notes" / "n.md")
        threads = self._counting_listing(monkeypatch)

        async def gate_twice():
            refusals = [security.sensitive_write_path_refusal(inside) for _ in range(2)]
            return threading.get_ident(), refusals

        loop_thread, refusals = asyncio.run(gate_twice())
        assert all(r and not security.is_unverifiable_path_refusal(r) for r in refusals)
        assert len(threads) == 1 and threads[0] != loop_thread

        self.now += paths_targets._APP_READONLY_ROWS_TTL_SECS
        _, refusals = asyncio.run(gate_twice())
        assert refusals[0] and len(threads) == 2, "an expired listing is taken again"

    @pytest.mark.parametrize("on_loop", [True, False], ids=["event-loop", "worker-thread"])
    def test_a_wedged_listing_refuses_and_is_never_started_twice(self, home, monkeypatch, on_loop):
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        inside = str(_install(home, "demo", ["notes"]) / "notes" / "n.md")
        sibling = str(home / "apps" / "demo" / "data" / "other.md")
        monkeypatch.setattr(
            paths_targets,
            "_APP_READONLY_ROWS_BUDGET_SECS",
            0.05,  # flake-ok: the listing is held until the test ends, so it always runs out
        )
        gate = threading.Event()
        threads = self._counting_listing(monkeypatch, gate=gate)

        def gate_both():
            return [security.sensitive_write_path_refusal(path) for path in (inside, sibling)]

        async def gate_both_on_the_loop():
            return gate_both()

        try:
            refusals = asyncio.run(gate_both_on_the_loop()) if on_loop else gate_both()
        finally:
            gate.set()
        self._let_the_listing_finish()
        assert all(r and security.is_unverifiable_path_refusal(r) for r in refusals)
        assert len(threads) == 1, "the wedged listing is waited on, not started again"

    def test_a_listing_past_its_deadline_refuses_later_checks_at_once(self, home, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        inside = str(_install(home, "demo", ["notes"]) / "notes" / "n.md")
        monkeypatch.setattr(
            paths_targets,
            "_APP_READONLY_ROWS_BUDGET_SECS",
            0.05,  # flake-ok: the listing is held until the test ends, so it always runs out
        )
        gate = threading.Event()
        threads = self._counting_listing(monkeypatch, gate=gate)
        later: list[str | None] = []
        caller = threading.Thread(
            target=lambda: later.append(security.sensitive_write_path_refusal(inside))
        )
        try:
            assert security.is_unverifiable_path_refusal(
                security.sensitive_write_path_refusal(inside)
            )
            # A second full wait would outlast the join below: only a refusal at once returns.
            monkeypatch.setattr(
                paths_targets,
                "_APP_READONLY_ROWS_BUDGET_SECS",
                3600.0,  # flake-ok: never waited on; it only has to outlast the bounded join
            )
            caller.start()
            caller.join(FALLBACK_TIMEOUT_SECS)
            assert not caller.is_alive(), "a listing past its deadline is not waited on again"
        finally:
            gate.set()
            if caller.ident is not None:
                caller.join(FALLBACK_TIMEOUT_SECS)
        self._let_the_listing_finish()
        assert later and security.is_unverifiable_path_refusal(later[0])
        assert len(threads) == 1, "the late listing is not started again"

    def test_off_the_loop_every_check_lists_afresh_off_its_thread(self, home, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        threads = self._counting_listing(monkeypatch)
        inside = home / "apps" / "late" / "data" / "notes" / "n.md"

        assert security.sensitive_write_path_refusal(str(inside)) is None
        _install(home, "late", ["notes"])
        assert security.sensitive_write_path_refusal(str(inside))
        assert len(threads) >= 2, "each check took its own listing"
        assert threading.get_ident() not in threads, "the listing ran on the bounded pool"


@_POSIX_ONLY
class TestThePlanSealsTheDeclaredDirs:
    @pytest.mark.parametrize("mode", _MODES)
    def test_linux_seals_exactly_the_declared_dir_read_only(self, home, mode):
        _install(home, "demo", ["notes"])
        plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, mode)
        readonly = set(plan.readonly)

        for target in _targets("apps/demo/data/notes") | _targets("apps/.demo-data-tmp/notes"):
            assert target in readonly
        for sibling in _targets("apps/demo/data") | _targets("apps/demo/data/other"):
            assert sibling not in readonly, f"{sibling} sealed though nothing declared it"

    @pytest.mark.parametrize("mode", _MODES)
    def test_macos_denies_writes_and_links_under_the_declared_dir(self, home, mode):
        _install(home, "demo", ["notes"])
        profile = sandbox._build_seatbelt_profile(mode)

        for target in _targets("apps/demo/data/notes"):
            assert f'(deny file-write* (subpath "{target}"))' in profile
            assert f'(deny file-link (subpath "{target}"))' in profile
            assert f'(deny file-read* (subpath "{target}"))' not in profile, "reads stay open"

    @pytest.mark.parametrize("backend", [sandbox_plan.BACKEND_NAMESPACE, "seatbelt"])
    def test_every_parent_is_guarded_against_rename(self, home, backend):
        _install(home, "demo", ["deep/notes"])
        if backend == "seatbelt":
            text = sandbox._build_seatbelt_profile("standard")
            guarded = {
                line.split('"')[1] for line in text.splitlines() if "file-write* (literal" in line
            }
        else:
            guarded = set(
                sandbox._spawn_plan(
                    sandbox_plan.BACKEND_NAMESPACE, "standard"
                ).runtime_ancestor_guards
            )
        data = os.path.join(str(Path.home()), ".kiro/crew/apps/demo/data")
        for parent in (data, os.path.join(data, "deep")):
            assert parent in guarded, f"{parent} can be renamed from under the seal"

    def test_a_relocated_data_home_seals_the_resolved_dir(self, tmp_path, monkeypatch):
        relocated = tmp_path / "srv" / "crew"
        relocated.mkdir(parents=True)
        monkeypatch.setattr(sandbox, "config_dir", lambda: relocated)
        monkeypatch.setattr(credential_masks, "config_dir", lambda: relocated)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
        _install(relocated, "demo", ["notes"])

        plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "standard")
        assert str(relocated / "apps" / "demo" / "data" / "notes") in set(plan.readonly)

    def test_a_declared_core_leaf_stays_masked(self, home):
        """The negative case: declaring a core HIDDEN leaf read-only cannot unmask it."""
        _install(home, "meetings", ["edits"])
        assert "apps/meetings/data/edits" in sandbox._CREW_HIDDEN_LEAVES
        plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "standard")
        for target in _targets("apps/meetings/data/edits"):
            assert target in set(plan.sensitive_dirs)
            assert target not in set(plan.readonly)


@_POSIX_ONLY
class TestTheBackendGetsBackOnlyItsOwnDirs:
    def test_the_app_backend_lifts_its_declared_dirs(self, home):
        _install(home, "demo", ["notes"])
        lifted = sandbox.app_declared_readonly_targets("demo", builtin=False)
        assert set(lifted) == _targets("apps/demo/data/notes")
        plan = sandbox._spawn_plan(
            sandbox_plan.BACKEND_NAMESPACE, "standard", extra_visible_dirs=lifted
        )
        assert not set(lifted) & set(plan.readonly)
        # The staged spelling is not handed back, so it stays sealed for the backend too.
        assert _targets("apps/.demo-data-tmp/notes") <= set(plan.readonly)

    def test_a_lift_holds_through_a_linked_data_home_spelling(self, home):
        """The plan folds an alias onto its canonical root, so the lift is compared folded."""
        _install(home, "demo", ["notes"])
        canonical = os.path.join(str(Path.home()), ".kiro/crew")
        alias = os.path.join(str(Path.home()), ".kirocrew")
        lifted = (os.path.join(canonical, "apps/demo/data/notes"),)
        plan = sandbox._spawn_plan(
            sandbox_plan.BACKEND_NAMESPACE,
            "standard",
            extra_visible_dirs=lifted,
            crew_home_aliases=((alias, canonical, 0, 0),),
        )
        assert lifted[0] not in set(plan.readonly), "the alias spelling re-sealed the lift"
        agent = sandbox._spawn_plan(
            sandbox_plan.BACKEND_NAMESPACE,
            "standard",
            crew_home_aliases=((alias, canonical, 0, 0),),
        )
        assert lifted[0] in set(agent.readonly)

    def test_no_other_app_and_no_other_source_lifts_them(self, home):
        _install(home, "demo", ["notes"])
        _install(home, "other", [])
        assert sandbox.app_declared_readonly_targets("other", builtin=False) == ()
        assert sandbox.app_declared_readonly_targets("demo", builtin=True) == ()

    def test_a_declared_core_leaf_is_never_lifted(self, home):
        """The negative case: a declaration naming a core leaf hands back nothing for it."""
        _install(home, "meetings", ["edits", "edits/inner", "mine"])
        lifted = set(sandbox.app_declared_readonly_targets("meetings", builtin=False))
        assert lifted == _targets("apps/meetings/data/mine")
        plan = sandbox._spawn_plan(
            sandbox_plan.BACKEND_NAMESPACE, "standard", extra_visible_dirs=tuple(lifted)
        )
        for target in _targets("apps/meetings/data/edits"):
            assert target in set(plan.sensitive_dirs), "a lift unmasked a core leaf"

    def test_a_declared_dir_holding_a_core_leaf_is_never_lifted(self, home, monkeypatch):
        monkeypatch.setattr(
            sandbox, "_CREW_HIDDEN_LEAVES", (*sandbox._CREW_HIDDEN_LEAVES, "apps/demo/data/n/k")
        )
        _install(home, "demo", ["n"])
        assert sandbox.app_declared_readonly_targets("demo", builtin=False) == ()


@_POSIX_ONLY
class TestTheLinkRefusal:
    def test_a_symlinked_dir_refuses_the_spawn(self, home, tmp_path):
        data = _install(home, "demo", ["notes"])
        (tmp_path / "elsewhere").mkdir()
        (data / "notes").symlink_to(tmp_path / "elsewhere")
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="is a link"):
            sandbox._establish_app_readonly_dirs()

    def test_a_linked_component_below_the_data_home_refuses_the_spawn(self, home, tmp_path):
        app = home / "apps" / "demo"
        app.mkdir(parents=True)
        (app / "app.json").write_text(
            json.dumps({"name": "demo", "version": "1", "agentReadOnlyDirs": ["notes"]}),
            encoding="utf-8",
        )
        (tmp_path / "planted").mkdir()
        (app / "data").symlink_to(tmp_path / "planted")
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="is a link"):
            sandbox._establish_app_readonly_dirs()

    def test_an_absent_dir_is_created_to_be_sealed_on_linux(self, home):
        data = _install(home, "demo", ["notes"])
        established: list[str] = []
        assert sandbox._establish_app_readonly_dirs(established) == [str(data / "notes")]
        assert (data / "notes").is_dir()
        assert established == [str(data / "notes")]

    def test_nothing_is_created_at_or_beneath_a_declared_credential_mask(self, home):
        """A manifest that skipped validation must not make every spawn refuse."""
        app = home / "apps" / "demo"
        (app / "data").mkdir(parents=True)
        (app / "app.json").write_text(
            json.dumps(
                {
                    "name": "demo",
                    "version": "1",
                    "credentialMasks": ["private"],
                    "agentReadOnlyDirs": ["private", "private/inner", "notes"],
                }
            ),
            encoding="utf-8",
        )
        established: list[str] = []
        assert sandbox._establish_app_readonly_dirs(established) == [str(app / "data" / "notes")]
        assert not (app / "data" / "private").exists()
        sandbox._refuse_aliased_masked_leaves()

    def test_nothing_is_created_for_an_app_that_is_not_there(self, home):
        established: list[str] = []
        sandbox._establish_app_readonly_dirs(established)
        assert not (home / "apps" / "meetings").exists()
        assert not (home / "apps" / "spec-builder").exists()
        assert established == []

    def test_an_absent_data_dir_is_never_created(self, home):
        """The lifecycle reads a ``data/`` beside held-aside data as proof the copy is stale."""
        data = _install(home, "demo", ["notes"])
        data.rmdir()
        established: list[str] = []
        assert sandbox._establish_app_readonly_dirs(established) == []
        assert not data.exists()
        assert established == []

    def test_a_name_planted_at_the_move_aside_spelling_does_not_stop_the_seal(self, home):
        """An agent can create ``.<name>-data-tmp``; that must not switch the seal off."""
        data = _install(home, "demo", ["notes"])
        (home / "apps" / ".demo-data-tmp").mkdir()
        established: list[str] = []
        assert sandbox._establish_app_readonly_dirs(established) == [str(data / "notes")]
        assert established == [str(data / "notes")]


class TestTheDeclarationOutlivesTheManifestBesideTheData:
    def _install_app(self, tmp_path, dirs):
        from kiro_crew.apps.manager import install_app

        result = install_app(_make_app_source(tmp_path, name="demo", agentReadOnlyDirs=dirs))
        assert result.ok, result.error

    def test_an_install_records_the_declaration(self, app_home, tmp_path):
        self._install_app(tmp_path, ["notes"])
        record = app_home / "apps" / ".demo-agent-readonly-dirs.json"
        assert json.loads(record.read_text(encoding="utf-8")) == {
            "name": "demo",
            "agentReadOnlyDirs": ["notes"],
        }
        assert not (app_home / "apps" / ".demo-credential-masks.json").exists()

    def test_a_default_uninstall_keeps_the_kept_data_fenced(self, app_home, tmp_path):
        from kiro_crew.apps.manager import uninstall_app

        self._install_app(tmp_path, ["notes"])
        assert uninstall_app("demo").ok
        assert not (app_home / "apps" / "demo" / "app.json").exists()
        assert _installed_leaves() == ("apps/.demo-data-tmp/notes", "apps/demo/data/notes")

    @_POSIX_ONLY
    def test_a_spawn_after_an_interrupted_keep_data_uninstall_keeps_the_held_data(
        self, app_home, tmp_path, monkeypatch
    ):
        """A spawn between the crash and the reinstall must not make the only copy look stale."""
        from kiro_crew.apps import manager_uninstall
        from kiro_crew.apps.manager import install_app, uninstall_app

        self._install_app(tmp_path, ["notes"])
        kept = app_home / "apps" / "demo" / "data" / "kept.md"
        kept.write_text("user", encoding="utf-8")

        class _Killed(BaseException):
            pass

        def _killed_mid_removal(dest):
            for entry in dest.iterdir():
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            raise _Killed

        with monkeypatch.context() as patch:
            patch.setattr(manager_uninstall, "_rmtree_app_dir", _killed_mid_removal)
            with pytest.raises(_Killed):
                uninstall_app("demo")
        assert (app_home / "apps" / ".demo-data-tmp" / "kept.md").is_file()

        assert sandbox._establish_app_readonly_dirs([]) == []
        assert not (app_home / "apps" / "demo" / "data").exists()

        reinstall = install_app(
            _make_app_source(tmp_path / "again", name="demo", agentReadOnlyDirs=["notes"])
        )
        assert reinstall.ok, reinstall.error
        assert kept.read_text(encoding="utf-8") == "user"


_CHILD = r"""
import errno, json, os, sys

def attempt(path):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("agent")
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))

notes, sibling = sys.argv[1], sys.argv[2]
with open(os.path.join(notes, "kept.md"), encoding="utf-8") as fh:
    kept = fh.read()
print("REPORT " + json.dumps({
    "read": kept,
    "write": attempt(os.path.join(notes, "kept.md")),
    "create": attempt(os.path.join(notes, "new.md")),
    "sibling": attempt(sibling),
}), flush=True)
"""


@pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only")
class TestARealAgentSpawnCannotWriteTheDir:
    """Linux needs unprivileged user and mount namespaces; skipped where there are none."""

    @pytest.fixture
    def live_home(self, tmp_path, monkeypatch) -> Path:
        if not sandbox.userns_available():
            pytest.skip("this host cannot build the namespace sandbox")
        home = (tmp_path / "home").resolve()
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
        sandbox.reset_backend()
        yield home
        sandbox.reset_backend()

    def _run(self, home: Path, tier: str, **kwargs) -> dict:
        data = _install(home, "demo", ["notes"])
        (data / "notes").mkdir(exist_ok=True)
        (data / "notes" / "kept.md").write_text("user", encoding="utf-8")
        argv = [sys.executable, "-c", _CHILD, str(data / "notes"), str(data / "other.md")]
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
        done = subprocess.run(
            sandbox.namespace_argv(argv, tier, **kwargs),
            cwd=home,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=FALLBACK_TIMEOUT_SECS,
            check=False,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        (line,) = [ln for ln in done.stdout.splitlines() if ln.startswith("REPORT ")]
        return json.loads(line[len("REPORT ") :])

    @pytest.mark.parametrize("tier", _MODES)
    def test_the_declared_dir_is_read_only_to_an_agent(self, live_home, tier):
        report = self._run(live_home, tier)
        assert report == {"read": "user", "write": "EROFS", "create": "EROFS", "sibling": "ok"}
        assert (live_home / "apps/demo/data/notes/kept.md").read_text(encoding="utf-8") == "user"

    def test_the_apps_own_backend_keeps_write_access(self, live_home):
        _install(live_home, "demo", ["notes"])
        lifted = sandbox.app_declared_readonly_targets("demo", builtin=False)
        assert lifted
        report = self._run(live_home, "standard", extra_visible_dirs=lifted)
        assert report["write"] == "ok" and report["create"] == "ok"
