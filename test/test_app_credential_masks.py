"""App-declared credential masks: ``credentialMasks`` in ``app.json``.

An app lists files under its OWN data directory (``apps/<name>/data/``) and the sandbox
hides them exactly as it hides a core ``HIDDEN`` crew-home leaf: masked in every mode,
a symlink at the leaf or below the data home refuses the spawn, and a second hard link
is located and masked or refuses. These cases pin the entry grammar, the manifest
round trip and signature, where declarations are read from, the plan and Seatbelt
profile, the alias passes, the backend's own lift, and the negative half: a
declaration reaches nothing it does not name.
"""

from __future__ import annotations

import codecs
import json
import os
import sys
from pathlib import Path

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 -- requested by name
from app_manager_helpers import _make_app_source
from sandbox_absent_ceiling_seal_helpers import (  # noqa: F401 -- fixtures are requested by name
    _no_host_ssh_probe,
    crew_home_fixture,
)

from kiro_crew import sandbox, sandbox_plan
from kiro_crew.apps import credential_masks
from kiro_crew.apps.manifest import AppManifest

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
    """The scratch data home, seen by the mask reader as well as by the sandbox."""
    monkeypatch.setattr(credential_masks, "config_dir", lambda: crew_home)
    return crew_home


def _install(home: Path, directory: str, masks: object, *, name: str | None = None) -> Path:
    """Write ``apps/<directory>/app.json`` declaring *masks*; return the app's data dir."""
    app = home / "apps" / directory
    (app / "data").mkdir(parents=True, exist_ok=True)
    manifest = {"name": name or directory, "version": "1.0.0", "credentialMasks": masks}
    (app / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    return app / "data"


def _leaves(app: str, *entries: str) -> tuple[str, ...]:
    """*app*'s leaves for *entries* at both places its data can be, sorted."""
    return tuple(
        sorted(
            leaf
            for entry in entries
            for leaf in (f"apps/{app}/data/{entry}", f"apps/.{app}-data-tmp/{entry}")
        )
    )


def _targets(leaf: str) -> set[str]:
    home = str(Path.home())
    return {os.path.join(home, f"{prefix}/{leaf}") for prefix in _PREFIXES}


class TestEntryGrammar:
    @pytest.mark.parametrize(
        "entry", ["secrets.json", "tokens/oauth.json", "a/b/c", ".secret", "x" * 240]
    )
    def test_a_relative_path_under_the_data_dir_is_accepted(self, entry):
        assert credential_masks.entry_error(entry) is None

    @pytest.mark.parametrize(
        "entry",
        [
            "",
            "/etc/passwd",
            "../other-app/data/secrets.json",
            "a/../../b",
            "./secrets.json",
            "a//b",
            "dir/",
            "a\\b",
            "C:secrets",
            "a\x00b",
            "a\nb",
            "\ud800",
            "tokens/a\udfffb",
            "secrets.",
            " secrets",
            "secrets ",
            "x" * 241,
            "/".join(["a"] * 17),
            7,
            None,
        ],
    )
    def test_anything_that_could_leave_the_data_dir_is_refused(self, entry):
        assert credential_masks.entry_error(entry) is not None

    def test_a_declaration_must_be_a_bounded_list(self):
        assert credential_masks.declaration_errors(None) == []
        assert credential_masks.declaration_errors("secrets.json")
        too_many = [f"s{i}" for i in range(credential_masks.MAX_ENTRIES + 1)]
        assert credential_masks.declaration_errors(too_many)
        assert credential_masks.valid_entries(too_many) == ()

    def test_only_admissible_entries_are_taken(self):
        value = ["secrets.json", "../escape", "secrets.json", "keys/a.pem"]
        assert credential_masks.valid_entries(value) == ("secrets.json", "keys/a.pem")
        assert credential_masks.declaration_errors(value) == [
            "credentialMasks[1] must not contain an empty, '.' or '..' segment"
        ]


class TestManifest:
    def _manifest(self, **extra: object) -> AppManifest:
        return AppManifest.from_dict(
            {
                "name": "demo",
                "version": "1.0.0",
                "displayName": "Demo",
                "description": "d",
                **extra,
            }
        )

    def test_it_round_trips_and_validates(self):
        manifest = self._manifest(credentialMasks=["secrets.json"])
        assert manifest.credentialMasks == ["secrets.json"]
        assert manifest.validate() == []
        assert manifest.to_dict()["credentialMasks"] == ["secrets.json"]
        assert "credentialMasks" not in manifest.extra

    def test_an_escaping_entry_refuses_the_manifest(self):
        manifest = self._manifest(credentialMasks=["../other/data/secrets.json"])
        assert any("credentialMasks[0]" in error for error in manifest.validate())
        assert manifest.credentialMasks == []

    def test_the_signature_covers_a_declaration_and_no_other_manifest_changes(self):
        bare = self._manifest()
        declared = self._manifest(credentialMasks=["b.json", "a.json"])
        assert b"credentialMasks" not in bare.signing_payload()
        assert json.loads(declared.signing_payload())["credentialMasks"] == ["a.json", "b.json"]
        assert self._manifest(credentialMasks=[]).signing_payload() == bare.signing_payload()


class TestWhereDeclarationsAreRead:
    def test_no_shipped_built_in_declares_a_mask(self):
        """Every current user's sandbox is unchanged: the seam starts empty."""
        assert credential_masks.declared_masks(installed=False) == {}

    def test_an_installed_app_declares_leaves_under_its_own_data_dir(self, home):
        _install(home, "demo", ["secrets.json", "keys/a.pem"])
        assert credential_masks.declared_crew_leaves() == _leaves(
            "demo", "keys/a.pem", "secrets.json"
        )

    def test_a_manifest_saved_with_a_byte_order_mark_declares_its_leaves(self, home):
        # The installer accepts such a manifest, so the mask reader must too.
        _install(home, "demo", ["secrets.json"])
        manifest = home / "apps" / "demo" / "app.json"
        manifest.write_bytes(codecs.BOM_UTF8 + manifest.read_bytes())
        assert credential_masks.declared_crew_leaves() == _leaves("demo", "secrets.json")

    def test_a_manifest_naming_another_app_declares_nothing(self, home):
        _install(home, "evil", ["secrets.json"], name="victim")
        assert credential_masks.declared_masks() == {}

    def test_an_invalid_entry_is_dropped_and_the_rest_apply(self, home, caplog):
        _install(home, "demo", ["/etc/passwd", "../victim/data/secrets.json", "ok.json"])
        assert credential_masks.declared_crew_leaves() == _leaves("demo", "ok.json")
        assert "ignoring invalid credentialMasks" in caplog.text

    @pytest.mark.parametrize(
        "body",
        ["{not json", json.dumps(["list"]), "", "[" * 100_000],
        ids=["not-json", "not-an-object", "empty", "nested-past-the-parser-depth"],
    )
    def test_an_unparseable_manifest_declares_nothing(self, home, body):
        app = home / "apps" / "demo"
        app.mkdir(parents=True)
        (app / "app.json").write_text(body, encoding="utf-8")
        assert credential_masks.declared_masks() == {}

    def test_a_sidecar_directory_is_not_an_app(self, home):
        _install(home, ".demo-update-old-1-ab", ["secrets.json"], name="demo")
        assert credential_masks.declared_masks() == {}

    def test_a_changed_manifest_is_read_again(self, home):
        _install(home, "demo", ["a.json"])
        assert credential_masks.declared_crew_leaves() == _leaves("demo", "a.json")
        manifest = home / "apps" / "demo" / "app.json"
        manifest.write_text(
            json.dumps({"name": "demo", "credentialMasks": ["a.json", "bb.json"]}),
            encoding="utf-8",
        )
        os.utime(manifest, ns=(1, 1))
        assert credential_masks.declared_crew_leaves() == _leaves("demo", "a.json", "bb.json")

    def test_the_per_source_read_is_one_source(self, home):
        _install(home, "demo", ["secrets.json"])
        assert credential_masks.declared_crew_leaves_for("demo", builtin=False) == (
            "apps/demo/data/secrets.json",
        )
        assert credential_masks.declared_crew_leaves_for("demo", builtin=True) == ()


@_POSIX_ONLY
class TestThePlanHidesTheDeclaredLeaves:
    @pytest.mark.parametrize("mode", _MODES)
    def test_linux_masks_exactly_the_declared_leaf(self, home, mode):
        _install(home, "demo", ["secrets.json"])
        plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, mode)
        hidden, files = set(plan.sensitive_dirs), set(plan.sensitive_files)

        for target in _targets("apps/demo/data/secrets.json"):
            assert target in hidden and target in files
        for sibling in (
            _targets("apps/demo/data/other.json")
            | _targets("apps/demo/data")
            | _targets("apps/demo")
        ):
            assert sibling not in hidden, f"{sibling} masked though nothing declared it"

    def test_a_relocated_data_home_masks_the_resolved_leaf(self, tmp_path, monkeypatch):
        relocated = tmp_path / "srv" / "crew"
        relocated.mkdir(parents=True)
        monkeypatch.setattr(sandbox, "config_dir", lambda: relocated)
        monkeypatch.setattr(credential_masks, "config_dir", lambda: relocated)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
        _install(relocated, "demo", ["secrets.json"])

        plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "standard")
        target = str(relocated / "apps" / "demo" / "data" / "secrets.json")
        assert target in set(plan.sensitive_dirs) and target in set(plan.sensitive_files)

    def test_a_home_with_no_declaration_plans_as_before(self, home):
        before = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "standard")
        _install(home, "demo", [])
        after = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "standard")
        assert after.sensitive_dirs == before.sensitive_dirs

    def test_macos_denies_and_seals_the_declared_leaf_like_a_core_credential(self, home):
        """Read, write and link, by subpath and literal: the rules a core secret gets."""
        _install(home, "demo", ["secrets.json"])
        profile = sandbox._build_seatbelt_profile("standard")

        def rules(target: str) -> set[str]:
            return {line.replace(target, "<T>") for line in profile.splitlines() if target in line}

        core = rules(os.path.join(str(Path.home()), ".kiro/crew/token_signing.key"))
        assert len(core) == 6
        for target in _targets("apps/demo/data/secrets.json"):
            assert rules(target) == core, f"{target} is not fenced like a core credential"
        for target in _targets("apps/demo/data/other.json"):
            assert f'"{target}"' not in profile

    def test_macos_guards_every_parent_of_a_declared_leaf_against_rename(self, home):
        """A path rule does not follow a renamed parent, so no parent can be renamed."""
        before = sandbox._build_seatbelt_profile("standard")
        _install(home, "demo", ["tokens/oauth.json"])
        profile = sandbox._build_seatbelt_profile("standard")

        for prefix in _PREFIXES:
            for parent in (
                "apps",
                "apps/demo",
                "apps/demo/data",
                "apps/demo/data/tokens",
                "apps/.demo-data-tmp",
                "apps/.demo-data-tmp/tokens",
            ):
                path = os.path.join(str(Path.home()), f"{prefix}/{parent}")
                rule = f'(deny file-write* (literal "{path}"))'
                assert rule in profile, f"{parent} under {prefix} can be renamed"
                assert parent == "apps" or rule not in before
            # Only the entries: nothing below a parent is sealed by the guard.
            data = os.path.join(str(Path.home()), f"{prefix}/apps/demo/data")
            assert f'(deny file-write* (subpath "{data}"))' not in profile

    def test_macos_leaves_the_parents_unguarded_for_the_leafs_own_backend(self, home):
        """The backend that reads the leaf can create and remove the folder it lives in."""
        data = _install(home, "demo", ["tokens/oauth.json"])
        assert not (data / "tokens").exists()
        lifted = sandbox.app_declared_mask_targets("demo", builtin=False)
        assert lifted, "the declaration lifted nothing, so this pins nothing"
        backend = sandbox._build_seatbelt_profile("standard", extra_visible_dirs=lifted)
        agent = sandbox._build_seatbelt_profile("standard")

        for prefix in _PREFIXES:
            tokens = os.path.join(str(Path.home()), f"{prefix}/apps/demo/data/tokens")
            rule = f'(deny file-write* (literal "{tokens}"))'
            assert rule in agent, f"another spawn can rename {tokens}"
            assert rule not in backend, f"the app's own backend cannot create {tokens}"
            # The move-aside spelling is not handed back, so it stays guarded there too.
            staged = os.path.join(str(Path.home()), f"{prefix}/apps/.demo-data-tmp/tokens")
            assert f'(deny file-write* (literal "{staged}"))' in backend

    def test_namespace_plans_no_parent_guard_for_a_declared_leaf(self, home):
        """A mount moves with a renamed parent, so the namespace plan needs none."""
        before = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "standard")
        _install(home, "demo", ["tokens/oauth.json"])
        after = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "standard")
        assert after.runtime_ancestor_guards == before.runtime_ancestor_guards

    def test_the_lifecycle_staging_spelling_is_masked_too(self, home):
        _install(home, "demo", ["secrets.json"])
        plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "standard")
        for target in _targets("apps/.demo-data-tmp/secrets.json"):
            assert target in set(plan.sensitive_dirs)


@_POSIX_ONLY
class TestTheAliasPassesCoverTheDeclaredLeaves:
    def test_a_symlinked_leaf_refuses_the_spawn(self, home, tmp_path):
        data = _install(home, "demo", ["secrets.json"])
        (tmp_path / "elsewhere.json").write_text("x", encoding="utf-8")
        (data / "secrets.json").symlink_to(tmp_path / "elsewhere.json")

        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="SYMLINK"):
            sandbox._refuse_aliased_masked_leaves()

    def test_a_linked_component_below_the_data_home_refuses_the_spawn(self, home, tmp_path):
        data = _install(home, "demo", ["tokens/oauth.json"])
        (tmp_path / "planted").mkdir()
        (data / "tokens").symlink_to(tmp_path / "planted")

        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="LINK"):
            sandbox._refuse_aliased_masked_leaves()

    def test_a_second_hard_link_inside_the_home_is_masked(self, home):
        data = _install(home, "demo", ["secrets.json"])
        (data / "secrets.json").write_text("token", encoding="utf-8")
        alias = home / "copy-of-secrets"
        os.link(data / "secrets.json", alias)

        assert str(alias) in {mask.path for mask in sandbox._refuse_aliased_masked_leaves()}

    def test_a_second_hard_link_outside_every_home_refuses_the_spawn(self, home, tmp_path):
        data = _install(home, "demo", ["secrets.json"])
        (data / "secrets.json").write_text("token", encoding="utf-8")
        os.link(data / "secrets.json", tmp_path / "snapshot")

        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="find"):
            sandbox._refuse_aliased_masked_leaves()
        assert [row[0] for row in sandbox.masked_credential_leaf_aliases()] == [
            str(data / "secrets.json")
        ]

    @pytest.mark.parametrize("spelling", ["data", "staged"])
    def test_a_directory_at_a_declared_path_refuses_the_spawn(self, home, spelling):
        """An entry names a file: a directory there would hide files no pass judges."""
        data = _install(home, "demo", ["secrets"])
        parent = data if spelling == "data" else home / "apps" / ".demo-data-tmp"
        (parent / "secrets").mkdir(parents=True)
        (parent / "secrets" / "oauth.json").write_text("token", encoding="utf-8")

        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="DIRECTORY"):
            sandbox._refuse_aliased_masked_leaves()

    def test_a_directory_on_the_way_to_a_declared_file_is_fine(self, home):
        data = _install(home, "demo", ["tokens/oauth.json"])
        (data / "tokens").mkdir()
        (data / "tokens" / "oauth.json").write_text("token", encoding="utf-8")

        assert sandbox._refuse_aliased_masked_leaves() == ()

    def test_a_path_only_platform_refuses_any_second_name(self, home):
        from kiro_crew import sandbox_aliases

        data = _install(home, "demo", ["secrets.json"])
        (data / "secrets.json").write_text("token", encoding="utf-8")
        os.link(data / "secrets.json", home / "copy-of-secrets")

        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="path rule"):
            sandbox_aliases._refuse_multilinked_credential_leaves(masks_are_path_only=True)

    def test_an_undeclared_sibling_is_left_alone(self, home, tmp_path):
        data = _install(home, "demo", ["secrets.json"])
        (data / "notes.json").write_text("x", encoding="utf-8")
        os.link(data / "notes.json", tmp_path / "snapshot")
        (data / "cache.json").symlink_to(tmp_path / "snapshot")

        assert sandbox._refuse_aliased_masked_leaves() == ()


class TestTheBackendGetsBackOnlyItsOwnLeaves:
    def test_the_app_backend_lifts_its_declared_leaves(self, home):
        _install(home, "demo", ["secrets.json"])
        lifted = set(sandbox.app_declared_mask_targets("demo", builtin=False))
        assert lifted == _targets("apps/demo/data/secrets.json")

    def test_no_other_app_and_no_other_source_lifts_them(self, home):
        _install(home, "demo", ["secrets.json"])
        _install(home, "other", [])
        assert sandbox.app_declared_mask_targets("other", builtin=False) == ()
        # A shipped package under the same name is different code: it gets nothing back
        # from the installed tree's declaration.
        assert sandbox.app_declared_mask_targets("demo", builtin=True) == ()

    def test_a_declaration_cannot_reach_another_apps_data(self, home):
        """The negative case: every lever an app has stays inside its own data dir."""
        victim = _install(home, "victim", ["secrets.json"])
        (victim / "secrets.json").write_text("token", encoding="utf-8")
        _install(home, "evil", ["../../victim/data/secrets.json", "/etc/shadow", "mine.json"])
        _install(home, "impostor", ["secrets.json"], name="victim")

        leaves = credential_masks.declared_crew_leaves()
        assert leaves == tuple(
            sorted((*_leaves("evil", "mine.json"), *_leaves("victim", "secrets.json")))
        )
        owned = ("apps/evil/data/", "apps/victim/data/", "apps/.evil-data-tmp/")
        assert all(leaf.startswith((*owned, "apps/.victim-data-tmp/")) for leaf in leaves)
        evil = set(sandbox.app_declared_mask_targets("evil", builtin=False))
        assert evil == _targets("apps/evil/data/mine.json")
        assert sandbox.app_declared_mask_targets("impostor", builtin=False) == ()

    def test_a_declared_core_leaf_is_never_lifted(self, home):
        """A declaration that names a core leaf adds no mask, so it gets nothing back."""
        _install(home, "meetings", ["edits", "mine.json"])
        assert "apps/meetings/data/edits" in sandbox._CREW_HIDDEN_LEAVES
        lifted = set(sandbox.app_declared_mask_targets("meetings", builtin=False))
        assert lifted == _targets("apps/meetings/data/mine.json")

    @_POSIX_ONLY
    def test_a_declared_file_hard_linked_to_a_core_credential_is_never_lifted(self, home):
        """The negative case: a second name makes the declared bytes a core leaf's bytes."""
        data = _install(home, "demo", ["secrets.json", "mine.json"])
        (home / "token_signing.key").write_text("core signing key", encoding="utf-8")
        os.link(home / "token_signing.key", data / "secrets.json")
        assert "token_signing.key" in sandbox._CREW_HARDLINK_REFUSED_LEAVES

        lifted = set(sandbox.app_declared_mask_targets("demo", builtin=False))
        assert str(data / "secrets.json") not in lifted
        assert str(data / "mine.json") in lifted
        # Every other spawn still masks the declared name, and the pass refuses nothing.
        assert sandbox._refuse_aliased_masked_leaves() == ()
        assert "apps/demo/data/secrets.json" in sandbox._hidden_leaves()

    @_POSIX_ONLY
    def test_a_declared_file_with_a_second_name_anywhere_is_never_lifted(self, home):
        data = _install(home, "demo", ["secrets.json"])
        (data / "secrets.json").write_text("token", encoding="utf-8")
        os.link(data / "secrets.json", data / "secrets.json.bak")
        assert str(data / "secrets.json") not in set(
            sandbox.app_declared_mask_targets("demo", builtin=False)
        )
        os.unlink(data / "secrets.json.bak")
        assert str(data / "secrets.json") in set(
            sandbox.app_declared_mask_targets("demo", builtin=False)
        )

    def test_a_directory_at_a_declared_path_is_never_lifted(self, home):
        data = _install(home, "demo", ["secrets"])
        (data / "secrets").mkdir()
        assert str(data / "secrets") not in set(
            sandbox.app_declared_mask_targets("demo", builtin=False)
        )

    def test_a_declared_directory_holding_a_core_leaf_is_never_lifted(self, home, monkeypatch):
        monkeypatch.setattr(
            sandbox,
            "_CREW_READONLY_LEAVES",
            (*sandbox._CREW_READONLY_LEAVES, "apps/demo/data/vault/key"),
        )
        _install(home, "demo", ["vault", "vault/key", "mine.json"])
        lifted = set(sandbox.app_declared_mask_targets("demo", builtin=False))
        assert lifted == _targets("apps/demo/data/mine.json")


class TestTheMasksOutliveTheManifestBesideTheData:
    """The lifecycle moves ``data/`` aside and a default uninstall drops the manifest."""

    def _install_app(self, tmp_path, masks):
        from kiro_crew.apps.manager import install_app

        result = install_app(_make_app_source(tmp_path, name="demo", credentialMasks=masks))
        assert result.ok, result.error

    def test_an_install_records_the_declaration(self, app_home, tmp_path):
        self._install_app(tmp_path, ["secrets.json"])
        record = app_home / "apps" / ".demo-credential-masks.json"
        assert json.loads(record.read_text(encoding="utf-8")) == {
            "name": "demo",
            "credentialMasks": ["secrets.json"],
        }

    def test_a_default_uninstall_keeps_the_kept_data_masked(self, app_home, tmp_path):
        from kiro_crew.apps.manager import uninstall_app

        self._install_app(tmp_path, ["secrets.json"])
        (app_home / "apps" / "demo" / "data" / "secrets.json").write_text("t", "utf-8")
        assert uninstall_app("demo").ok

        assert not (app_home / "apps" / "demo" / "app.json").exists()
        assert (app_home / "apps" / "demo" / "data" / "secrets.json").is_file()
        assert credential_masks.declared_crew_leaves() == _leaves("demo", "secrets.json")

    @pytest.mark.parametrize(
        "body",
        [
            "{not json",
            "",
            "[" * 100_000,
            json.dumps({"name": "other", "version": "1.0.0", "credentialMasks": []}),
            json.dumps({"name": "demo", "version": "1.0.0", "credentialMasks": ["../x"]}),
        ],
        ids=["not-json", "empty", "nested-past-the-parser-depth", "misnamed", "invalid-entry"],
    )
    def test_a_manifest_that_does_not_read_cleanly_keeps_the_record(self, app_home, tmp_path, body):
        from kiro_crew.apps.manager import uninstall_app

        self._install_app(tmp_path, ["secrets.json"])
        record = app_home / "apps" / ".demo-credential-masks.json"
        kept = record.read_text(encoding="utf-8")
        (app_home / "apps" / "demo" / "app.json").write_text(body, encoding="utf-8")
        credential_masks.remember_declaration("demo")
        assert record.read_text(encoding="utf-8") == kept

        assert uninstall_app("demo").ok
        assert record.read_text(encoding="utf-8") == kept
        assert credential_masks.declared_crew_leaves() == _leaves("demo", "secrets.json")

    def test_a_clean_manifest_declaring_nothing_removes_the_record(self, app_home, tmp_path):
        self._install_app(tmp_path, ["secrets.json"])
        manifest = app_home / "apps" / "demo" / "app.json"
        body = json.loads(manifest.read_text(encoding="utf-8"))
        body.pop("credentialMasks")
        manifest.write_text(json.dumps(body), encoding="utf-8")
        credential_masks.remember_declaration("demo")
        assert not (app_home / "apps" / ".demo-credential-masks.json").exists()

    def test_an_uninstall_that_cannot_record_the_masks_refuses(
        self, app_home, tmp_path, monkeypatch
    ):
        from kiro_crew.apps.manager import uninstall_app

        self._install_app(tmp_path, ["secrets.json"])
        (app_home / "apps" / ".demo-credential-masks.json").unlink()

        def refuse(*_args, **_kwargs):
            raise OSError("read-only")

        monkeypatch.setattr(credential_masks, "atomic_write", refuse)
        assert not uninstall_app("demo").ok
        assert (app_home / "apps" / "demo" / "app.json").is_file()

    def test_an_update_keeps_the_staged_data_masked(self, app_home, tmp_path, monkeypatch):
        from kiro_crew.apps import manager
        from kiro_crew.apps.manager import update_app

        self._install_app(tmp_path / "v1", ["secrets.json"])
        (app_home / "apps" / ".demo-credential-masks.json").unlink()
        seen: list[tuple[bool, tuple[str, ...]]] = []
        real_copy = manager._copy_app_tree

        def copy_and_look(source, dest):
            staged = app_home / "apps" / ".demo-data-tmp"
            seen.append((staged.is_dir(), credential_masks.declared_crew_leaves()))
            real_copy(source, dest)

        monkeypatch.setattr(manager, "_copy_app_tree", copy_and_look)
        source = _make_app_source(tmp_path / "v2", name="demo", version="2.0.0")
        assert update_app(source).ok

        # While the old manifest is gone and the new one not yet copied, the data waits
        # aside and is masked there; the new manifest declares nothing, so the record goes.
        assert seen == [(True, _leaves("demo", "secrets.json"))]
        assert not (app_home / "apps" / ".demo-credential-masks.json").exists()
        assert credential_masks.declared_masks() == {}
