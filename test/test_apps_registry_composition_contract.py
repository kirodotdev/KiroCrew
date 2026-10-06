"""The App Registry's composition: one facade over private pipeline owners.

``kiro_crew.apps.registry`` is the registry's only import path and patch surface, and
its responsibilities live in ``kiro_crew.apps.registry_pipeline``. For that split to
be invisible to every caller, each property below is pinned in the direction that
would catch a regression rather than in the direction that restates the code.

* Every name of the one-module registry resolves on the facade, to the object its
  owner holds (:data:`FROZEN_NAMES`, frozen rather than derived, because a list
  derived from the facade agrees with any facade).
* A name and the symbol it denotes cannot come apart: every module holding a name
  holds the same object, and a write through the facade reaches all of them -- the
  one-namespace behaviour several hundred ``monkeypatch.setattr(registry, ...)`` and
  ``mock.patch("kiro_crew.apps.registry....")`` sites rely on.
* The owners form one acyclic stack under the facade and none imports it. The three
  constructs repository guards pin to ``registry.py`` stay there, and exactly three
  call sites reach them through ``registry_pipeline._facade()`` at call time.
* No test patches a forwarded name with ``create=True``, the one spelling the
  facade cannot undo.
"""

from __future__ import annotations

import ast
import functools
import importlib
import importlib.util
import inspect
import itertools
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, NamedTuple
from unittest import mock

import pytest
import source_corpus

from kiro_crew.apps import registry
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.registry_pipeline import (
    caches,
    catalog,
    checkout,
    git_targets,
    indexes,
    install,
    manifests,
    recovery,
    sources,
    store_art,
    subprocess_env,
)

# One group per file: the patch-spelling guard parses every test module, and the
# production re-export scan parses part of the package, once per worker otherwise.
pytestmark = pytest.mark.xdist_group(name="tree_scan_apps_registry_composition")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PIPELINE_DIR = _REPO_ROOT / "src/kiro_crew/apps/registry_pipeline"
_PIPELINE_PACKAGE = "kiro_crew.apps.registry_pipeline"

#: The owners in the facade's declared layer order, lowest first.
PARTS: tuple[ModuleType, ...] = (
    subprocess_env,
    git_targets,
    caches,
    sources,
    recovery,
    checkout,
    store_art,
    indexes,
    manifests,
    catalog,
    install,
)

#: The registry's module-level surface: every name the one-module ``registry.py``
#: bound, dunders excluded. A name leaves this list only when the symbol it names is
#: deliberately deleted.
FROZEN_NAMES: tuple[str, ...] = (
    "Any",
    "AppManifest",
    "DESKTOP_BUILD_STEP_UNSUPPORTED",
    "FIRST_PARTY_AUTHORS",
    "IPv6Address",
    "InstalledTreeRefused",
    "Iterable",
    "Iterator",
    "Literal",
    "Path",
    "PinnedPathRefusal",
    "PlatformCompositionError",
    "RESERVED_APP_NAME_CODE",
    "SOURCE_REGISTRY_PREFIX",
    "StreamingLogLines",
    "_BUILD_TIMEOUT",
    "_CACHE_EXPIRY_BACKDATE_SLACK",
    "_CLONE_TIMEOUT",
    "_COMMIT_SHA_RE",
    "_DESKTOP_BUILD_REFUSAL",
    "_DESKTOP_LAYOUT_FILES",
    "_DETECT_PROBE_ENV_KEYS",
    "_EXTERNAL_REGISTRY_CACHE_TTL",
    "_GIT_AUTH_FAILURE_MARKERS",
    "_GIT_CLONE_LOCALE",
    "_GIT_CREDENTIAL_ENV_KEYS",
    "_GIT_FAILURE_CLASS_LABELS",
    "_HEAD_READ_LIMIT",
    "_InstallVerb",
    "_KILL_GRACE_PERIOD",
    "_MANIFEST_CACHE_GC_GRACE",
    "_MANIFEST_CACHE_TTL",
    "_MANIFEST_SOURCE_SUBDIR",
    "_MoveAsideUndoFailed",
    "_PACKED_REFS_READ_LIMIT",
    "_PUBLIC_GIT_HOSTS",
    "_REFUSAL_LINES",
    "_REGISTRY_FILE",
    "_REGISTRY_REVIEW_TIERS",
    "_REGISTRY_ROW_KEYS",
    "_REGISTRY_TRUST_TIERS",
    "_REVIEW_COMMUNITY",
    "_REVIEW_CURATED",
    "_REVIEW_UNSET",
    "_SAFE_ENV_KEYS",
    "_SCRIPT_TIMEOUT",
    "_STALE_CHECKOUT_PATTERN",
    "_STALE_CHECKOUT_RETENTION_DAYS",
    "_TRUST_INDEX",
    "_TRUST_OWNER",
    "_absent",
    "_app_sources_dir",
    "_append_external_registry_apps",
    "_apply_configured_branch",
    "_apply_trust_fields",
    "_catalog_installable_rows",
    "_catalog_row_supersedes_seed",
    "_clone_branch_matches",
    "_clone_build_app",
    "_clone_build_app_locked",
    "_clone_origin_matches",
    "_clone_origin_url",
    "_clone_sandbox_mode",
    "_communicate_with_timeout",
    "_configured_registry_hosts",
    "_contained_join",
    "_context_clone_sandbox_mode",
    "_credential_free_external_registry_entries",
    "_credential_free_external_registry_value",
    "_desktop_build_refusal",
    "_desktop_gate_probe",
    "_desktop_layout_present",
    "_detect_installed_probe",
    "_detect_probe_env",
    "_edition_registry_rows",
    "_effective_registries",
    "_enrich_with_install_status",
    "_entry_git_url",
    "_expire_cache_file",
    "_external_registry_app_by_repo",
    "_external_registry_cache_identity",
    "_external_registry_cache_path",
    "_external_registry_cache_path_for_identity",
    "_external_registry_repos",
    "_external_registry_row",
    "_fetch_and_cache_external_registry",
    "_fetch_app_manifest",
    "_fetch_external_registry_index",
    "_fold_author",
    "_gc_manifest_cache_dir",
    "_git_clone_or_pull",
    "_git_fetch_branch",
    "_git_fetch_commit",
    "_git_fetch_ref",
    "_git_output_is_auth_shaped",
    "_git_target_has_ambiguous_scp_prefix",
    "_git_target_has_ambiguous_ssh_userinfo",
    "_git_target_has_query_or_fragment",
    "_git_target_is_unsupported",
    "_git_transport_env",
    "_git_url_host",
    "_identity",
    "_install_coordinates",
    "_installed_tree_preview",
    "_is_catalog_row",
    "_is_external_row",
    "_is_owner_designated_repo",
    "_is_probe_env_key",
    "_is_safe_env_key",
    "_is_safe_registry_subdir",
    "_is_ssh_git_url",
    "_is_stale_candidate",
    "_is_supported_registry_transport",
    "_kill_process_group",
    "_layout_cleanup_escaped",
    "_legacy_external_registry_cache_path",
    "_load_external_registries",
    "_load_registry_file",
    "_loggable_git_transport_output",
    "_looks_like_git_url",
    "_manifest_cache_dir",
    "_manifest_cache_path",
    "_manifest_source_coordinates",
    "_merge_manifest",
    "_move_checkout_aside",
    "_normalize_git_target",
    "_normalized_ipv6_literal",
    "_official_entry",
    "_owner_designated_repo_target",
    "_owner_tier_confirmed",
    "_pinned_registries",
    "_pinned_registry_entry",
    "_platform",
    "_provisioning_declared",
    "_public_registry_name",
    "_read_clone_branch",
    "_read_external_registry_cache",
    "_read_git_metadata_bounded",
    "_read_manifest_cache",
    "_redact_url_userinfo",
    "_redacted_git_failure_class",
    "_refusal_line",
    "_refuse_identity_mismatch",
    "_registry_app_candidates",
    "_registry_identity_key",
    "_registry_trust_tier",
    "_registry_trust_tier_of",
    "_remote_controlled_url",
    "_remove_legacy_credential_registry_cache",
    "_remove_legacy_name_keyed_registry_cache",
    "_remove_new_layout_files",
    "_rename_and_refresh_mtime",
    "_report_retained_stale_checkouts",
    "_requirements_owned_by_the_runtime",
    "_resolve_install_entry",
    "_resolve_manifest",
    "_resolve_registry_row",
    "_resolved_clone_commit",
    "_restorable_or_none",
    "_restore_moved_aside",
    "_retained_startup_refusal",
    "_rmtree_force_settled",
    "_roll_back_post_script_refusal",
    "_run_app_build",
    "_safe_cache_stem",
    "_same_git_target",
    "_seed_row",
    "_sel_credential_decision",
    "_sel_credential_grant",
    "_sel_fn",
    "_set_aside_new_layout_files",
    "_stale_sibling",
    "_store_asset_path",
    "_strip_git_target_userinfo",
    "_sweep_stale_checkouts",
    "_sweep_stale_checkouts_sync",
    "_trust_repository_bindings",
    "_unpoison_rejected_checkout",
    "_valid_git_port",
    "_version_newer",
    "_write_external_registry_cache",
    "_write_manifest_cache",
    "annotations",
    "anonymous_git_env",
    "app_admission_denied",
    "app_execution_denied",
    "app_name_error",
    "app_source_dir",
    "asyncio",
    "atomic_write",
    "cgroup_scope_argv",
    "config_dir",
    "contextmanager",
    "copy_app_tree_as_installed",
    "create_subprocess_limited",
    "current_context",
    "datetime",
    "get_app",
    "get_registry_app",
    "get_registry_app_by_repo",
    "get_server_platform",
    "importlib",
    "install_app",
    "install_from_registry",
    "install_receipt",
    "is_clone_host_trusted",
    "is_module_style_entry_point",
    "is_registry_source",
    "is_reparse_point",
    "is_reserved_app_name",
    "json",
    "known_registry_repos",
    "list_catalog_apps",
    "list_installed_apps",
    "list_registry",
    "logger",
    "logging",
    "minimal_env",
    "official_catalog",
    "open_pinned_descendant_dir",
    "os",
    "platform_compat",
    "posixpath",
    "preserved_data_awaits",
    "re",
    "refresh_registries",
    "registry_name_from_source",
    "registry_source_repository",
    "repository_bound_grant_denied",
    "requirements_in_tree",
    "resolve_installed_trust_repository",
    "runtime_provisions_requirements",
    "sandboxed_spawn_argv",
    "sandboxed_spawn_argv_async",
    "scrub_env",
    "sel",
    "set_app_provenance",
    "spawn_launches_entry_point_as_python",
    "sha256",
    "shipped_builtin_names",
    "shutil",
    "stat",
    "sys",
    "tempfile",
    "threading",
    "time",
    "timezone",
    "trusted_app_repository",
    "unicodedata",
    "update_app",
    "uuid",
    "verified_signer",
    "wrap_argv",
    "wrap_argv_async",
)

#: Where each owner reaches a construct the facade keeps, and the one name it reads
#: there. Nothing else in the pipeline refers to the facade.
FACADE_CALL_SITES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("caches", "_read_external_registry_cache", "_admit_cached_index_entries"),
        ("indexes", "_fetch_and_cache_external_registry", "_admit_fetched_index_entries"),
        ("install", "_clone_build_app_locked", "_run_app_build"),
    }
)

#: The constructs repository guards read in ``registry.py`` by path: the internal
#: Python spawn site list (``test_internal_python_isolation.py``) and the two index
#: name gates (``test_kebab_gates_reject_trailing_newline.py``).
PINNED_RESIDENTS: tuple[str, ...] = (
    "_run_app_build",
    "_admit_cached_index_entries",
    "_admit_fetched_index_entries",
)

_ABSENT = object()


def _shared_names() -> list[str]:
    """Every non-dunder name held by the facade and an owner, or by two owners."""
    seen: dict[str, int] = {}
    for module in (registry, *PARTS):
        for name in vars(module):
            if not name.startswith("__"):
                seen[name] = seen.get(name, 0) + 1
    return sorted(name for name, count in seen.items() if count > 1)


def _holders(name: str) -> list[ModuleType]:
    """The modules whose own namespace binds ``name``."""
    return [module for module in (registry, *PARTS) if name in vars(module)]


def _bindings(name: str, holders: list[ModuleType]) -> list[object]:
    """What each of *holders* binds ``name`` to, ``None`` where it binds nothing."""
    return [vars(module).get(name) for module in holders]


def _put_back(name: str, original: object, holders: list[ModuleType]) -> None:
    """Write *original* straight into every holder.

    The cleanup of a case that drives the facade's undo: it goes around that undo, so
    a regression in it fails the one case instead of every later test in the process.
    """
    for module in holders:
        setattr(module, name, original)


@contextmanager
def _patched(kind: str, name: str, value: object) -> Iterator[None]:
    """Patch ``registry.<name>`` to *value* the way *kind* spells it, for one block."""
    if kind == "monkeypatch.setattr":
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(registry, name, value)
            yield
    else:
        with mock.patch.object(registry, name, new=value):
            yield


def _unwinds_like_a_flat_module(
    name: str, holders: list[ModuleType], steps: list[tuple[str, object]], label: str
) -> None:
    """Enter *steps* outermost first; every exit must restore what its enter replaced.

    That is what each spelling does on a module that binds the name itself, whatever
    the values: the same object written twice, or the original written back inside a
    patch.
    """
    if not steps:
        return
    (kind, value), rest = steps[0], steps[1:]
    before = _bindings(name, holders)
    with _patched(kind, name, value):
        assert _bindings(name, holders) == [value] * len(holders), label
        _unwinds_like_a_flat_module(name, holders, rest, label)
        assert _bindings(name, holders) == [value] * len(holders), label
    assert _bindings(name, holders) == before, label


def _source(module: ModuleType) -> str:
    return Path(module.__file__ or "").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The surface
# ---------------------------------------------------------------------------


class TestTheSurfaceSurvivesTheSplit:
    def test_the_frozen_inventory_is_not_empty(self) -> None:
        # An emptied list would make the case below pass while checking nothing.
        assert len(FROZEN_NAMES) > 200

    @pytest.mark.parametrize("name", FROZEN_NAMES)
    def test_every_name_the_module_bound_still_resolves_to_its_owners_object(
        self, name: str
    ) -> None:
        value = getattr(registry, name, _ABSENT)
        assert value is not _ABSENT, f"registry.{name} no longer resolves"
        for module in _holders(name):
            assert (
                vars(module)[name] is value
            ), f"registry.{name} answers a different object than {module.__name__} holds"

    def test_the_part_order_is_the_facades_and_covers_the_package(self) -> None:
        # The facade resolves a read from the first owner in this order that holds the
        # name, so an owner missing from it is one whose names the facade cannot reach.
        assert registry._PART_MODULES == tuple(part.__name__ for part in PARTS)
        on_disk = {path.stem for path in _PIPELINE_DIR.glob("*.py") if path.stem != "__init__"}
        assert on_disk == {part.__name__.rpartition(".")[2] for part in PARTS}

    def test_an_exported_name_is_read_from_its_owner_on_every_access(self) -> None:
        # Not bound in the facade, so a value written straight into the owner is what
        # the facade answers. (It is not what the owner's IMPORTERS see -- only a write
        # through the facade reaches them -- which is why tests patch the facade.)
        assert "_manifest_cache_dir" in registry._EXPORTS
        assert "_manifest_cache_dir" not in vars(registry)
        replacement = object()
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(caches, "_manifest_cache_dir", replacement)
            assert registry._manifest_cache_dir is replacement

    def test_no_module_rebinds_a_global(self) -> None:
        # A ``global`` rebind writes its module's namespace directly and never reaches
        # ``_Facade``, so a rebound name would freeze in every other holder. The
        # registry keeps no mutable module state, and this keeps it that way.
        rebound = []
        for module in (registry, *PARTS):
            for node in ast.walk(ast.parse(_source(module))):
                if isinstance(node, (ast.Global, ast.Nonlocal)):
                    rebound.append((module.__name__, node.names))
        assert rebound == []

    def test_a_missing_name_is_an_attribute_error(self) -> None:
        # ``hasattr``, ``getattr(..., default)`` and ``mock.patch`` all rely on it.
        assert not hasattr(registry, "_no_such_registry_name")
        with pytest.raises(AttributeError):
            registry.no_such_registry_name  # noqa: B018

    def test_dir_lists_the_exported_names(self) -> None:
        assert set(FROZEN_NAMES) <= set(dir(registry))

    def test_a_star_import_carries_exactly_the_public_names_of_the_inventory(
        self, tmp_path: Path
    ) -> None:
        # ``__all__`` is derived, and a star import consults it and never the module
        # ``__getattr__``. The machinery binds only private names, so nothing it needs
        # leaks into a star importer's namespace and nothing public goes missing. The
        # star import runs in a real module loaded from ``tmp_path``, the one place a
        # star import is legal.
        public = {name for name in FROZEN_NAMES if not name.startswith("_")}
        assert set(registry.__all__) == public
        probe_path = tmp_path / "registry_star_probe.py"
        probe_path.write_text("from kiro_crew.apps.registry import *\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("registry_star_probe", probe_path)
        assert spec and spec.loader
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        carried = {name for name in vars(probe) if not name.startswith("__")}
        assert carried == public
        for name in public:
            assert vars(probe)[name] is getattr(registry, name)

    def test_the_pinned_residents_stay_in_the_facade(self) -> None:
        # ``test_internal_python_isolation.py`` reads ``_run_app_build`` in
        # ``apps/registry.py`` by path, and ``test_kebab_gates_reject_trailing_newline.py``
        # counts the two ``KEBAB_RE.fullmatch(`` gates in the facade's source.
        for name in PINNED_RESIDENTS:
            assert name in vars(registry), f"{name} left registry.py"
            assert getattr(registry, name).__module__ == registry.__name__
            assert name not in registry._EXPORTS
            assert not any(name in vars(part) for part in PARTS)
        top_level = {
            node.name
            for node in ast.parse(_source(registry)).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert set(PINNED_RESIDENTS) <= top_level

    def test_no_owner_carries_an_index_name_gate(self) -> None:
        # ``test_kebab_gates_reject_trailing_newline.py`` reads only the facade's
        # source, so a name gate written in an owner would escape its bare-``match``
        # check. Both gates stay in the facade, and every owner reaches them there.
        for part in PARTS:
            source = _source(part)
            assert "KEBAB_RE" not in source, f"{part.__name__} gates index names itself"


# ---------------------------------------------------------------------------
# One symbol per name, and writes that reach every binding of it
# ---------------------------------------------------------------------------

#: An exported name several owners import: defined in ``git_targets``.
_MULTI_HOLDER = "_strip_git_target_userinfo"


class TestOneNamespaceForWrites:
    @pytest.mark.parametrize("name", _shared_names())
    def test_every_module_holding_a_name_holds_the_same_object(self, name: str) -> None:
        values = {id(vars(module)[name]) for module in _holders(name)}
        assert (
            len(values) == 1
        ), f"{name} names different objects in {[m.__name__ for m in _holders(name)]}"

    @pytest.mark.parametrize("name", _shared_names())
    def test_a_write_through_the_facade_reaches_every_holder_and_is_undone(self, name: str) -> None:
        holders = [module for module in _holders(name) if module is not registry]
        original = getattr(registry, name)
        sentinel = object()
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(registry, name, sentinel)
            assert getattr(registry, name) is sentinel
            for module in holders:
                assert vars(module)[name] is sentinel, f"{module.__name__}.{name} was missed"
        assert getattr(registry, name) is original
        for module in holders:
            assert vars(module)[name] is original, f"{module.__name__}.{name} not restored"

    def test_the_multi_holder_case_is_what_it_claims(self) -> None:
        holders = [module for module in _holders(_MULTI_HOLDER) if module is not registry]
        assert _MULTI_HOLDER in registry._EXPORTS and len(holders) > 3

    def test_mock_patch_of_an_exported_name_restores_every_holder(self) -> None:
        # ``mock.patch`` sees a name the facade does not bind as non-local, so its exit
        # deletes the name and writes the original back; both halves go through here.
        holders = [module for module in _holders(_MULTI_HOLDER) if module is not registry]
        original = getattr(registry, _MULTI_HOLDER)
        with mock.patch.object(registry, _MULTI_HOLDER) as fake:
            for module in holders:
                assert vars(module)[_MULTI_HOLDER] is fake
        for module in holders:
            assert vars(module)[_MULTI_HOLDER] is original

    def test_a_dotted_string_patch_restores_every_holder(self) -> None:
        # The spelling most of the suite uses: ``mock.patch("kiro_crew.apps.registry.X")``.
        holders = [module for module in _holders(_MULTI_HOLDER) if module is not registry]
        original = getattr(registry, _MULTI_HOLDER)
        with mock.patch(f"{registry.__name__}.{_MULTI_HOLDER}") as fake:
            for module in holders:
                assert vars(module)[_MULTI_HOLDER] is fake
        for module in holders:
            assert vars(module)[_MULTI_HOLDER] is original

    def test_every_nesting_of_the_patch_harnesses_unwinds_like_a_flat_module(self) -> None:
        # Four deep over ``mock.patch`` and ``monkeypatch.setattr``, with the original
        # and one fake as the values, so a patch writing back what an enclosing patch
        # replaced, or the same object twice, is covered. Each harness restores what it
        # read at its own enter, so no nesting depends on the facade pairing anything.
        holders = [module for module in _holders(_MULTI_HOLDER) if module is not registry]
        original = getattr(registry, _MULTI_HOLDER)
        fake = object()
        kinds = ("mock.patch", "monkeypatch.setattr")
        steps = [(kind, value) for kind in kinds for value in (original, fake)]
        try:
            for depth in range(1, 5):
                for sequence in itertools.product(steps, repeat=depth):
                    label = " > ".join(
                        f"{kind}({'original' if value is original else 'fake'})"
                        for kind, value in sequence
                    )
                    _unwinds_like_a_flat_module(_MULTI_HOLDER, holders, list(sequence), label)
        finally:
            _put_back(_MULTI_HOLDER, original, holders)

    def test_a_name_the_facade_also_binds_unwinds_through_every_holder(self) -> None:
        # ``create_subprocess_limited`` is bound here for ``_run_app_build`` AND imported
        # by the owners that spawn; ``mock.patch`` reads it as local and writes it back.
        name = "create_subprocess_limited"
        assert name in registry._ALSO_HELD and name in vars(registry)
        holders = _holders(name)
        assert len(holders) > 3
        original = getattr(registry, name)
        with mock.patch.object(registry, name, create=True) as fake:
            assert _bindings(name, holders) == [fake] * len(holders)
        assert _bindings(name, holders) == [original] * len(holders)

    def test_create_true_on_a_forwarded_name_deletes_it_from_every_holder(self) -> None:
        # The one patch spelling the facade cannot undo: ``mock.patch`` sees a forwarded
        # name as non-local, and under ``create=True`` its exit is the delete alone. The
        # guard in ``TestPatchSpellings`` keeps that spelling out of the suite, with this
        # case its one allowlisted site: it keeps the guard's premise true, and fails
        # the day the facade can undo it.
        holders = [module for module in _holders(_MULTI_HOLDER) if module is not registry]
        original = getattr(registry, _MULTI_HOLDER)
        try:
            with mock.patch.object(registry, _MULTI_HOLDER, create=True):
                pass
            assert _bindings(_MULTI_HOLDER, holders) == [None] * len(holders)
        finally:
            _put_back(_MULTI_HOLDER, original, holders)

    def test_shadowing_a_builtin_through_the_facade_reaches_every_part(self) -> None:
        # One namespace for writes includes the builtins a module can shadow.
        def fake_sorted(*args: Any, **kwargs: Any) -> list[Any]:
            return []

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(registry, "sorted", fake_sorted, raising=False)
            for module in (registry, *PARTS):
                assert vars(module)["sorted"] is fake_sorted
        for module in (registry, *PARTS):
            assert "sorted" not in vars(module)

    def test_patching_the_facades_sys_does_not_redirect_its_own_resolution(self) -> None:
        # ``registry.sys`` is part of the surface (``_GIT_CLONE_LOCALE`` and the install
        # platform check read ``sys.platform``), and patching it must not change where
        # the facade finds its owners: the machinery reads ``sys`` through an alias.
        def fake_host(*args: Any, **kwargs: Any) -> str:
            return "patched.example"

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(registry, "sys", mock.MagicMock(platform="win32"))
            assert registry._manifest_cache_dir is caches._manifest_cache_dir
            patched.setattr(registry, "_git_url_host", fake_host)
            assert sources._git_url_host is fake_host

    def test_a_delete_and_restore_through_the_facade_round_trips(self) -> None:
        # ``mock.patch`` undoes a name the facade does not bind by DELETING it and then
        # writing the original back, so both halves have to reach every holder.
        holders = [module for module in _holders(_MULTI_HOLDER) if module is not registry]
        original = getattr(registry, _MULTI_HOLDER)
        with pytest.MonkeyPatch.context() as patched:
            patched.delattr(registry, _MULTI_HOLDER)
            assert not hasattr(registry, _MULTI_HOLDER)
            for module in holders:
                assert _MULTI_HOLDER not in vars(module)
        for module in holders:
            assert vars(module)[_MULTI_HOLDER] is original

    def test_every_part_logs_through_the_facades_logger(self) -> None:
        # Log routing, filters and ``caplog.at_level(..., logger="kiro_crew.apps.registry")``
        # captures key on the facade's name.
        for part in PARTS:
            if "logger" in vars(part):
                assert part.logger is registry.logger
        assert registry.logger.name == registry.__name__ == "kiro_crew.apps.registry"


# ---------------------------------------------------------------------------
# A patch on the facade reaches the call site in whichever owner makes the call
# ---------------------------------------------------------------------------


class _SpawnRefused(OSError):
    """What the recording spawn stub raises, so no real process ever starts."""


def _record_spawns(patched: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Patch the sandbox and spawn seams on the facade; return the argv log."""
    spawned: list[tuple[str, ...]] = []

    async def fake_wrap(argv: list[str], **kwargs: Any) -> tuple[list[str], None]:
        return list(argv), None

    async def fake_sandboxed(argv: list[str], **kwargs: Any) -> tuple[list[str], dict, None]:
        return list(argv), dict(kwargs.get("env") or {}), None

    async def fake_spawn(*argv: str, **kwargs: Any) -> Any:
        spawned.append(tuple(argv))
        raise _SpawnRefused("spawn refused by the test")

    patched.setattr(registry, "wrap_argv_async", fake_wrap)
    patched.setattr(registry, "sandboxed_spawn_argv_async", fake_sandboxed)
    patched.setattr(registry, "cgroup_scope_argv", lambda argv: argv)
    patched.setattr(registry, "create_subprocess_limited", fake_spawn)
    patched.setattr(registry, "_sel_fn", None)
    return spawned


class TestAPatchOnTheFacadeReachesEveryCallSite:
    @pytest.mark.asyncio
    async def test_one_spawn_patch_reaches_the_facade_and_four_owners(self, tmp_path: Path) -> None:
        with pytest.MonkeyPatch.context() as patched:
            spawned = _record_spawns(patched)
            # checkout: the origin read of an existing checkout.
            (tmp_path / "clone" / ".git").mkdir(parents=True)
            assert await registry._clone_origin_url(tmp_path / "clone") is None
            # catalog: a detectInstalled probe (its spawn failure reads as absent).
            probe = [{"name": "demo-app", "detectInstalled": "true"}]
            patched.setattr(registry, "app_execution_denied", lambda *a, **k: None)
            assert await registry._detect_installed_probe(probe, {}) == set()
            # indexes: an external index fetch (its spawn failure reads as None).
            assert (
                await registry._fetch_external_registry_index(
                    "https://github.com/acme/registry.git", "main"
                )
                is None
            )
            # manifests: the throwaway manifest clone of a trusted-host URL.
            patched.setattr(registry, "is_clone_host_trusted", lambda url: True)
            assert (
                await registry._fetch_app_manifest("https://github.com/acme/demo-app.git", "main")
                is None
            )
            # the facade's own build step.
            (tmp_path / "build").mkdir()
            (tmp_path / "build" / "package.json").write_text("{}", encoding="utf-8")
            patched.setattr(registry.shutil, "which", lambda name: "/opt/test/npm")
            with pytest.raises(_SpawnRefused):
                await registry._run_app_build(
                    tmp_path / "build",
                    "demo-app",
                    [],
                    manifest=AppManifest.from_dict({}),
                    self_managed=False,
                )

        # The hooks/fsmonitor neutralizer splices a variable number of leading
        # ``-c KEY=VALUE`` pairs after ``git``, so ``argv[:3]`` of a git spawn is now
        # all neutralizer tokens and never shows the subcommand. Read the subcommand
        # positionally (skip ``git`` and every ``-c KEY`` pair) instead, and pin the
        # two git call sites (indexes' index clone, manifests' throwaway clone) at
        # their subcommand slot AND its ``--depth 1``.
        def _git_subcommand_index(argv: tuple[str, ...]) -> int:
            i = 1
            while i < len(argv) and argv[i] == "-c":
                i += 2
            return i

        # The non-git spawns keep their fixed leading shape.
        non_git = [argv for argv in spawned if not (argv and argv[0] == "git")]
        assert non_git == [
            ("/bin/sh", "-c", "true"),
            ("/opt/test/npm", "install"),
        ], non_git
        git_spawns = [argv for argv in spawned if argv and argv[0] == "git"]
        # ``git remote get-url`` (the checkout origin read) plus the two clone sites.
        assert len(git_spawns) == 3, git_spawns
        origin, *clones = git_spawns
        assert origin[_git_subcommand_index(origin) : _git_subcommand_index(origin) + 3] == (
            "remote",
            "get-url",
            "origin",
        ), origin
        # Both remaining git spawns are the anonymous ``git clone --depth 1`` of
        # indexes' and manifests' call sites: assert the subcommand slot and the
        # ``--depth`` argument right after it.
        assert len(clones) == 2, clones
        for clone in clones:
            sub = _git_subcommand_index(clone)
            assert clone[sub] == "clone", clone
            assert clone[sub + 1 : sub + 3] == ("--depth", "1"), clone
        # The order the call sites ran in is preserved (indexes before manifests).
        assert [argv[0] for argv in spawned] == [
            "git",
            "/bin/sh",
            "git",
            "git",
            "/opt/test/npm",
        ], [argv[0] for argv in spawned]

    def test_a_cache_reader_patch_reaches_the_catalog_lookups(self) -> None:
        # ``_effective_registries`` is defined in ``sources`` and
        # ``_read_external_registry_cache`` in ``caches``; both are called from
        # ``catalog`` through the bindings ``catalog`` imported.
        reg = SimpleNamespace(
            name="acme", repo="https://github.com/acme/registry.git", branch="main"
        )
        row = {"name": "demo-app", "repo": "https://github.com/acme/demo-app.git"}
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(registry, "_load_registry_file", lambda: [])
            patched.setattr(registry, "_effective_registries", lambda: [reg])
            patched.setattr(registry, "_read_external_registry_cache", lambda *a, **k: [dict(row)])
            assert registry.known_registry_repos() == {row["repo"]}
            found = registry.get_registry_app_by_repo(row["repo"])
        assert found is not None and found["name"] == "demo-app"
        assert found["branch"] == "main"

    @pytest.mark.asyncio
    async def test_the_build_step_is_resolved_on_the_facade_when_it_runs(
        self, tmp_path: Path
    ) -> None:
        # ``install._clone_build_app_locked`` calls the facade's ``_run_app_build``
        # through ``registry_pipeline._facade()``, so a patch of the facade's own
        # binding is what the install transaction runs.
        async def fake_clone(*args: Any, **kwargs: Any) -> None:
            return None

        async def fake_build(
            build_dir: Path, app_name: str, log_lines: list[str], **kwargs: Any
        ) -> dict:
            return {"ok": False, "name": app_name, "error": f"fake build in {build_dir.name}"}

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(registry, "_app_sources_dir", lambda: tmp_path / "app-sources")
            slot = tmp_path / "app-sources" / "demo-app"
            slot.mkdir(parents=True)
            (slot / "app.json").write_text(json.dumps({"name": "demo-app"}), encoding="utf-8")
            patched.setattr(registry, "_git_clone_or_pull", fake_clone)
            patched.setattr(registry, "app_admission_denied", lambda *a, **k: None)
            patched.setattr(registry, "_run_app_build", fake_build)
            result = await registry._clone_build_app(
                "https://github.com/acme/demo-app.git", "demo-app", []
            )
        assert result == {"ok": False, "name": "demo-app", "error": "fake build in demo-app"}

    @pytest.mark.asyncio
    async def test_the_index_gates_are_resolved_on_the_facade_when_they_run(
        self, tmp_path: Path
    ) -> None:
        good = {"name": "good-app", "subdirectory": "apps/good-app"}
        bad = {"name": "../escape", "subdirectory": ""}
        seen: list[str] = []
        real_cached = registry._admit_cached_index_entries
        real_fetched = registry._admit_fetched_index_entries

        def spy_cached(data: list[Any]) -> list[dict[str, Any]]:
            seen.append("cached")
            return real_cached(data)

        def spy_fetched(entries: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
            seen.append(f"fetched:{name}")
            return real_fetched(entries, name)

        async def fake_index(repo: str, branch: str) -> list[dict[str, Any]]:
            return [dict(good), dict(bad)]

        reg = SimpleNamespace(
            name="acme", repo="https://github.com/acme/registry.git", branch="main"
        )
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(registry, "_manifest_cache_dir", lambda: tmp_path)
            patched.setattr(registry, "_admit_cached_index_entries", spy_cached)
            patched.setattr(registry, "_admit_fetched_index_entries", spy_fetched)
            patched.setattr(registry, "_fetch_external_registry_index", fake_index)
            fetched = await registry._fetch_and_cache_external_registry(reg)
            cached = registry._read_external_registry_cache(
                registry._external_registry_cache_identity(reg)
            )
        assert [e["name"] for e in fetched or []] == ["good-app"]
        assert [e["name"] for e in cached or []] == ["good-app"]
        assert seen == ["fetched:acme", "cached"]


# ---------------------------------------------------------------------------
# Layering and placement
# ---------------------------------------------------------------------------


def _engine_imports(source: str) -> set[str]:
    """Every registry module an owner's source imports: a lower owner, or the facade.

    Walks the whole tree, so an import inside a function counts, and resolves a
    relative import against the pipeline package, so ``from .. import registry`` and
    ``from . import caches`` are seen for what they name.
    """
    engine = {registry.__name__, *(part.__name__ for part in PARTS)}
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, _PIPELINE_PACKAGE)
            for alias in node.names:
                for candidate in (f"{base}.{alias.name}", base):
                    if candidate in engine:
                        found.add(candidate)
                        break
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name in engine)
    return found


def _facade_reads(source: str) -> set[tuple[str, str]]:
    """``(enclosing function, attribute)`` for each ``_facade().<attr>`` in *source*."""
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_facade"):
            continue
        parent = parents.get(node)
        attribute = parent.attr if isinstance(parent, ast.Attribute) else "<bare>"
        scope = parents.get(node)
        while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parents.get(scope)
        found.add((scope.name if scope is not None else "<module>", attribute))
    return found


class TestLayering:
    def test_the_import_reader_sees_every_spelling_of_an_engine_import(self) -> None:
        # A reader that saw only absolute module strings would pass an owner importing
        # the facade lazily or relatively, which is the violation it exists to catch.
        source = (
            "from . import caches\n"
            "from .git_targets import _git_url_host\n"
            "def f():\n"
            "    from ..registry import _run_app_build\n"
            "    from .. import registry\n"
            f"import {_PIPELINE_PACKAGE}.install\n"
        )
        assert _engine_imports(source) == {
            f"{_PIPELINE_PACKAGE}.caches",
            f"{_PIPELINE_PACKAGE}.git_targets",
            f"{_PIPELINE_PACKAGE}.install",
            registry.__name__,
        }

    def test_an_owner_imports_only_owners_below_it_and_never_the_facade(self) -> None:
        order = [part.__name__ for part in PARTS]
        for index, part in enumerate(PARTS):
            imported = _engine_imports(_source(part))
            assert registry.__name__ not in imported, f"{part.__name__} imports the facade"
            later = imported - set(order[:index])
            assert later == set(), f"{part.__name__} imports an owner at or above it: {later}"

    def test_exactly_the_pinned_call_sites_reach_the_facade(self) -> None:
        # Each reaches one pinned resident, from one function, at call time. Nothing
        # else names the facade, so no other owner depends on it at all.
        found = {
            (part.__name__.rpartition(".")[2], function, attribute)
            for part in PARTS
            for function, attribute in _facade_reads(_source(part))
        }
        assert found == FACADE_CALL_SITES
        assert {attribute for _module, _function, attribute in found} == set(PINNED_RESIDENTS)
        for part in PARTS:
            constants = {
                node.value
                for node in ast.walk(ast.parse(_source(part)))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            assert registry.__name__ not in constants, f"{part.__name__} spells the facade"

    def test_the_facade_helper_is_only_ever_called_in_place(self) -> None:
        # An alias (``f = _facade``) or a stored module would hide a fourth call site
        # from the reader above, so every reference to ``_facade`` in an owner is its
        # import or the callee of a call.
        for part in PARTS:
            tree = ast.parse(_source(part))
            callees = {
                id(node.func)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            stray = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, ast.Name) and node.id == "_facade" and id(node) not in callees
            ]
            assert stray == [], f"{part.__name__} refers to _facade at lines {stray}"

    def test_the_call_site_reader_can_fail(self) -> None:
        assert _facade_reads("def f():\n    return _facade()._run_app_build(1)\n") == {
            ("f", "_run_app_build")
        }
        assert _facade_reads("x = _facade()\n") == {("<module>", "<bare>")}
        assert _facade_reads("def f():\n    return facade()._run_app_build(1)\n") == set()

    def test_the_pipeline_helper_answers_the_facade(self) -> None:
        from kiro_crew.apps import registry_pipeline

        assert registry_pipeline._FACADE == registry.__name__
        assert registry_pipeline._facade() is registry

    def test_the_facade_resolves_owners_by_name_not_by_module_object(self) -> None:
        # A table of module objects is a second place a module is stored; an owner
        # purged and imported again would then be reached through the stale copy.
        for table in (registry._EXPORTS, registry._ALSO_HELD):
            for holders in table.values():
                assert all(isinstance(holder, str) for holder in holders)
        assert inspect.getsource(registry._part).count("importlib.import_module(") == 1
        assert importlib.import_module(registry._PART_MODULES[0]) is subprocess_env


# ---------------------------------------------------------------------------
# The facade's own code, as a type checker and a patched importlib see it
# ---------------------------------------------------------------------------


def _bare_loads(source: str) -> list[tuple[int, str]]:
    """``(line, name)`` for every bare-global read of a forwarded name in *source*.

    Any ``ast.Name`` in Load context whose id is a key of ``_EXPORTS``, at any depth,
    outside an ``import`` / ``from ... import`` node -- so the ``TYPE_CHECKING``
    imports are allowed and every other bare use is not.
    """

    class _Loads(ast.NodeVisitor):
        def __init__(self) -> None:
            self.found: list[tuple[int, str]] = []

        def visit_Import(self, node: ast.Import) -> None:
            return

        visit_ImportFrom = visit_Import  # type: ignore[assignment]

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load) and node.id in registry._EXPORTS:
                self.found.append((node.lineno, node.id))

    loads = _Loads()
    loads.visit(ast.parse(source))
    return loads.found


def _type_checking_imports(source: str) -> dict[str, str]:
    """``name -> module`` for each ``from ... import`` under a module-level type guard."""
    found: dict[str, str] = {}
    for statement in ast.parse(source).body:
        if not isinstance(statement, ast.If):
            continue
        test = ast.unparse(statement.test)
        if test not in ("TYPE_CHECKING", "typing.TYPE_CHECKING", "_typing.TYPE_CHECKING"):
            continue
        for node in statement.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                found.update(dict.fromkeys((a.asname or a.name for a in node.names), node.module))
    return found


class TestTheFacadesOwnCode:
    def test_the_facade_reads_no_forwarded_name_as_a_bare_global(self) -> None:
        # A function defined here resolves a bare global through this module's own
        # namespace, which ``__getattr__`` never sees: a forwarded name read that way
        # is a ``NameError`` on whatever path reaches it.
        assert _bare_loads(_source(registry)) == []

    def test_the_bare_read_check_can_fail(self) -> None:
        assert _bare_loads("def f():\n    return _manifest_cache_dir()\n") == [
            (2, "_manifest_cache_dir")
        ]
        assert (
            _bare_loads(
                "if TYPE_CHECKING:\n"
                "    from kiro_crew.apps.registry_pipeline.caches import _manifest_cache_dir\n"
                "def f():\n"
                "    return _part('x')._manifest_cache_dir()\n"
            )
            == []
        )

    def test_the_type_checker_sees_every_exported_name(self) -> None:
        # ``__getattr__`` is hidden from the checker, so the names it serves at run
        # time are declared to it under ``TYPE_CHECKING``; a name missing there would
        # type as an error at a correct call site, and an extra one would hide a stale
        # name.
        declared = _type_checking_imports(_source(registry))
        assert declared == {name: holders[0] for name, holders in registry._EXPORTS.items()}

    def test_the_type_checking_reader_can_fail(self) -> None:
        source = _source(registry)
        assert source.count("        _manifest_cache_dir,\n") == 1
        dropped = _type_checking_imports(source.replace("        _manifest_cache_dir,\n", ""))
        assert "_manifest_cache_dir" not in dropped
        assert _type_checking_imports(
            "from a import x\nif DEBUG:\n    from b import y\n"
            "if typing.TYPE_CHECKING:\n    from c import z as w\n"
        ) == {"w": "c"}

    def test_a_loaded_owner_is_read_written_and_restored_without_import_module(
        self,
    ) -> None:
        owner = sys.modules[registry._EXPORTS[_MULTI_HOLDER][0]]
        original = vars(owner)[_MULTI_HOLDER]
        sentinel = object()
        with mock.patch.object(
            importlib, "import_module", side_effect=AssertionError("resolution imported")
        ) as refused:
            assert getattr(registry, _MULTI_HOLDER) is original
            with pytest.MonkeyPatch.context() as patched:
                patched.setattr(registry, _MULTI_HOLDER, sentinel)
                assert vars(owner)[_MULTI_HOLDER] is sentinel
                assert getattr(registry, _MULTI_HOLDER) is sentinel
            assert vars(owner)[_MULTI_HOLDER] is original
            assert refused.call_count == 0
            # The refusal sits on the binding the miss path calls.
            with pytest.raises(AssertionError, match="resolution imported"):
                registry._part("kiro_crew._registry_absent_probe")
            assert refused.call_count == 1

    @pytest.mark.asyncio
    async def test_the_facade_call_sites_resolve_without_import_module(
        self, tmp_path: Path
    ) -> None:
        # The name gates and the build step are reached through
        # ``registry_pipeline._facade()``. A test that stubs ``importlib.import_module``
        # for its own reasons must not be able to answer them.
        from kiro_crew.apps import registry_pipeline

        async def fake_clone(*args: Any, **kwargs: Any) -> None:
            return None

        async def fake_build(
            build_dir: Path, app_name: str, log_lines: list[str], **kwargs: Any
        ) -> dict:
            return {"ok": False, "name": app_name, "error": "fake build"}

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(registry, "_manifest_cache_dir", lambda: tmp_path)
            registry._write_external_registry_cache(
                "acme", [{"name": "good-app"}, {"name": "../escape"}]
            )
            patched.setattr(registry, "_app_sources_dir", lambda: tmp_path / "app-sources")
            slot = tmp_path / "app-sources" / "demo-app"
            slot.mkdir(parents=True)
            (slot / "app.json").write_text(json.dumps({"name": "demo-app"}), encoding="utf-8")
            patched.setattr(registry, "_git_clone_or_pull", fake_clone)
            patched.setattr(registry, "app_admission_denied", lambda *a, **k: None)
            patched.setattr(registry, "_run_app_build", fake_build)
            with mock.patch.object(
                importlib, "import_module", side_effect=AssertionError("facade imported")
            ) as refused:
                assert registry_pipeline._facade() is registry
                cached = registry._read_external_registry_cache("acme")
                built = await registry._clone_build_app(
                    "https://github.com/acme/demo-app.git", "demo-app", []
                )
            assert refused.call_count == 0
        assert [row["name"] for row in cached or []] == ["good-app"]
        assert built["error"] == "fake build"


# ---------------------------------------------------------------------------
# A reload of the facade re-executes the whole registry
# ---------------------------------------------------------------------------

_RELOAD_PROBE = """
import importlib, json, sys
from kiro_crew.apps import registry
from kiro_crew.apps.registry_pipeline import git_targets, subprocess_env
before = git_targets._strip_git_target_userinfo
sys.platform = "darwin"
importlib.reload(registry)
modules = [registry, *(sys.modules[m] for m in registry._PART_MODULES)]
report = {
    "source": registry.__file__,
    "locale": subprocess_env._GIT_CLONE_LOCALE,
    "facade_locale": registry._GIT_CLONE_LOCALE,
    "rebuilt": git_targets._strip_git_target_userinfo is not before,
    "facade_follows": registry._strip_git_target_userinfo is git_targets._strip_git_target_userinfo,
    "split": sorted(
        name
        for name in set().union(*(vars(m) for m in modules))
        if not name.startswith("__")
        and len({id(vars(m)[name]) for m in modules if name in vars(m)}) > 1
    ),
}
print(json.dumps(report))
"""


def test_a_reload_of_the_facade_reloads_every_owner(tmp_path: Path) -> None:
    # The one-module registry re-evaluated every module-level value on
    # ``importlib.reload``; ``test_external_registry.py`` reloads it to re-derive the
    # platform clone locale. Run in a child interpreter, so the reload's new objects
    # never reach this worker's other tests.
    import os
    import subprocess

    from kiro_crew.subprocess_utf8 import UTF8_TEXT

    # The child imports this checkout's package, as the parent does through pytest's
    # ``pythonpath``, not whichever ``kiro_crew`` its interpreter would otherwise find.
    source = _REPO_ROOT / "src"
    env = {
        **os.environ,
        "KIROCREW_HOME": str(tmp_path / "data"),
        "PYTHONPATH": os.pathsep.join([str(source), os.environ.get("PYTHONPATH", "")]),
    }
    completed = subprocess.run(
        [sys.executable, "-c", _RELOAD_PROBE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=90,
        check=False,
        **UTF8_TEXT,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert Path(report.pop("source")).resolve() == Path(registry.__file__).resolve()
    assert report == {
        "locale": "en_US.UTF-8",
        "facade_locale": "en_US.UTF-8",
        "rebuilt": True,
        "facade_follows": True,
        "split": [],
    }


# ---------------------------------------------------------------------------
# The patch spellings the suite may use on the facade
# ---------------------------------------------------------------------------

#: The patch callables by the dotted path they resolve to: which one, and the index of
#: ``create`` among its positional parameters.
_PATCH_CALLABLES = {
    "unittest.mock.patch": ("patch", 3),
    "unittest.mock.patch.object": ("object", 4),
    "unittest.mock.patch.multiple": ("multiple", 2),
}

#: ``patch.multiple`` keywords that configure the patch rather than name an attribute.
_MULTIPLE_OPTIONS = frozenset({"target", "spec", "create", "spec_set", "autospec", "new_callable"})

#: What a hit names when the patched attribute cannot be read off the source.
_DYNAMIC = "<dynamic>"

#: The one deliberate ``create=True`` patch of a forwarded name, keyed by file and the
#: test enclosing it: the premise case, which shows that such a patch still deletes the
#: name and puts every holder back itself.
_ALLOWED_CREATE_TRUE = frozenset(
    {
        (
            "test/test_apps_registry_composition_contract.py",
            "TestOneNamespaceForWrites."
            "test_create_true_on_a_forwarded_name_deletes_it_from_every_holder",
        )
    }
)

#: A value an expression may denote: a dotted ``path`` (a module, or an attribute reached
#: from one), a ``str``, or the known leading ``prefix`` of a string.
_Value = tuple[str, str]


class _Hit(NamedTuple):
    function: str
    name: str
    line: int


class _Resolver:
    """What the names in one module's source may denote, read off its AST alone.

    A name is bound by an import, or by a plain assignment in its function's scope or an
    enclosing one, followed to a fixed point; a name bound more than once may denote any
    of its values. An expression the reader cannot follow denotes nothing.
    """

    def __init__(
        self,
        tree: ast.Module,
        module: str | None,
        reexports: frozenset[str],
        *,
        is_package: bool = False,
    ) -> None:
        self._tree = tree
        # A package's ``__init__`` resolves ``from . import x`` against itself.
        self._package = (module if is_package else module.rpartition(".")[0]) if module else None
        self._reexports = reexports
        self._parents: dict[ast.AST, ast.AST] = {}
        self._bindings: dict[ast.AST, dict[str, list[ast.AST | frozenset[_Value]]]] = {}
        stack: list[ast.AST] = [tree]
        while stack:
            node = stack.pop()
            for child in ast.iter_child_nodes(node):
                self._parents[child] = node
                stack.append(child)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for name, path in self._imported(node):
                    self._bind(node, name, frozenset({("path", path)}))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._bind(node, target.id, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if isinstance(node.target, ast.Name):
                    self._bind(node, node.target.id, node.value)
        self._following: set[tuple[int, str]] = set()
        self._known: dict[tuple[int, str], set[_Value]] = {}
        # Set while resolving a name whose chain looped back on itself: a result
        # computed then is partial, so it is returned but not remembered.
        self._cut = False

    def _imported(self, node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
        if isinstance(node, ast.Import):
            return [
                (alias.asname, alias.name) if alias.asname else (alias.name.split(".")[0],) * 2
                for alias in node.names
            ]
        module = node.module or ""
        if node.level:
            if self._package is None:
                return []
            module = importlib.util.resolve_name("." * node.level + module, self._package)
        return [(alias.asname or alias.name, f"{module}.{alias.name}") for alias in node.names]

    def _bind(self, statement: ast.AST, name: str, value: ast.AST | frozenset[_Value]) -> None:
        self._bindings.setdefault(self.scope_of(statement), {}).setdefault(name, []).append(value)

    def scope_of(self, node: ast.AST) -> ast.AST:
        """The function whose body holds *node*, or the module; a decorator is outside."""
        child, parent = node, self._parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                decorators = getattr(parent, "decorator_list", [])
                if not any(child is decorator for decorator in decorators):
                    return parent
            child, parent = parent, self._parents.get(parent)
        return self._tree

    def function_of(self, node: ast.AST) -> str:
        """The dotted class and function names enclosing *node*, ``<module>`` for none."""
        names = []
        parent = self._parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(parent.name)
            parent = self._parents.get(parent)
        return ".".join(reversed(names)) or "<module>"

    def module_names(self) -> list[str]:
        return list(self._bindings.get(self._tree, {}))

    def is_facade(self, value: _Value) -> bool:
        kind, text = value
        return kind == "path" and (text == registry.__name__ or text in self._reexports)

    def values(self, expr: ast.AST | None, scope: ast.AST) -> set[_Value]:
        if expr is None:
            return set()
        if isinstance(expr, ast.Constant):
            return {("str", expr.value)} if isinstance(expr.value, str) else set()
        if isinstance(expr, ast.Name):
            return self._name(expr.id, scope)
        if isinstance(expr, ast.Attribute):
            found: set[_Value] = set()
            for value in self.values(expr.value, scope):
                if value[0] == "path":
                    found.add(("path", f"{value[1]}.{expr.attr}"))
                    if expr.attr == "__name__" and self.is_facade(value):
                        found.add(("str", registry.__name__))
            return found
        if isinstance(expr, ast.Call) and len(expr.args) == 1:
            if ("path", "importlib.import_module") in self.values(expr.func, scope):
                return {
                    ("path", text)
                    for kind, text in self.values(expr.args[0], scope)
                    if kind == "str"
                }
            return set()
        if isinstance(expr, ast.JoinedStr):
            return self._joined(expr.values, scope)
        if isinstance(expr, ast.FormattedValue):
            plain = expr.conversion == -1 and expr.format_spec is None
            return self.values(expr.value, scope) if plain else set()
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            return self._joined([expr.left, expr.right], scope)
        return set()

    def _joined(self, parts: list[ast.expr], scope: ast.AST) -> set[_Value]:
        text = ""
        for part in parts:
            values = self.values(part, scope)
            strings = {value for kind, value in values if kind == "str"}
            prefixes = {value for kind, value in values if kind == "prefix"}
            if len(strings) == 1 and not prefixes:
                text += strings.pop()
                continue
            if len(prefixes) == 1 and not strings:
                text += prefixes.pop()
            return {("prefix", text)} if text else set()
        return {("str", text)}

    def _name(self, name: str, scope: ast.AST) -> set[_Value]:
        key = (id(scope), name)
        if key in self._known:
            return self._known[key]
        if key in self._following:
            self._cut = True
            return set()
        self._following.add(key)
        outer_cut, self._cut = self._cut, False
        try:
            found: set[_Value] = set()
            for binding_scope in self._chain(scope):
                bound = self._bindings.get(binding_scope, {}).get(name)
                if bound is not None:
                    for value in bound:
                        found |= (
                            value
                            if isinstance(value, frozenset)
                            else self.values(value, binding_scope)
                        )
                    break
            if not self._cut:
                self._known[key] = found
            return found
        finally:
            self._following.discard(key)
            self._cut = outer_cut or self._cut

    def _chain(self, scope: ast.AST) -> Iterator[ast.AST]:
        while scope is not self._tree:
            yield scope
            scope = self.scope_of(scope)
        yield self._tree

    def hits(self, call: ast.Call) -> list[str]:
        """The forwarded names *call* patches with ``create`` not literally ``False``."""
        scope = self.scope_of(call)
        callable_ = next(
            (
                _PATCH_CALLABLES[text]
                for kind, text in self.values(call.func, scope)
                if text in _PATCH_CALLABLES
            ),
            None,
        )
        if callable_ is None:
            return []
        kind, create_at = callable_
        keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
        splat = any(keyword.arg is None for keyword in call.keywords)
        create = keywords.get(
            "create", call.args[create_at] if len(call.args) > create_at else None
        )
        if create is None and splat:
            # A ``**`` splat may carry ``create``: unknown is not literally False.
            create = ast.Name(id="<splat>")
        if create is None or (isinstance(create, ast.Constant) and create.value is False):
            return []
        target = keywords.get("target", call.args[0] if call.args else None)
        targets = self.values(target, scope)
        prefix = registry.__name__ + "."
        found: set[str] = set()
        if kind == "patch":
            for value_kind, text in targets:
                rest = text[len(prefix) :] if text.startswith(prefix) else None
                if rest is None or "." in rest:
                    continue
                found.add(rest if value_kind == "str" else _DYNAMIC)
        elif kind == "object":
            if any(self.is_facade(value) for value in targets):
                attribute = keywords.get("attribute", call.args[1] if len(call.args) > 1 else None)
                names = {
                    text
                    for value_kind, text in self.values(attribute, scope)
                    if value_kind == "str"
                }
                found = names if names else {_DYNAMIC}
        elif (
            any(self.is_facade(value) for value in targets) or ("str", registry.__name__) in targets
        ):
            found = {name for name in keywords if name not in _MULTIPLE_OPTIONS}
            if any(keyword.arg is None for keyword in call.keywords):
                found.add(_DYNAMIC)
        return sorted(name for name in found if name == _DYNAMIC or name in registry._EXPORTS)


def _module_name(path: Path) -> str | None:
    """The dotted import name of a file under ``src/``; a file elsewhere has none."""
    relative = path.relative_to(_REPO_ROOT)
    if relative.parts[0] != "src":
        return None
    parts = list(relative.with_suffix("").parts[1:])
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_package(path: Path) -> bool:
    return path.name == "__init__.py"


def _in_a_test_directory(path: Path) -> bool:
    return any(part.endswith("tests") for part in path.relative_to(_REPO_ROOT).parts[:-1])


def _python_files() -> Iterator[tuple[Path, str]]:
    """``(path, text)`` for every ``.py`` file the checkout holds that names the registry.

    Enumerated the way git sees the checkout (``source_corpus.repo_files``), so a
    nested worktree or scratch copy never stands in for the file it copies.
    """
    for path in source_corpus.repo_files_named(".py"):
        relative = path.relative_to(_REPO_ROOT).as_posix()
        if not relative.startswith(("test/", "src/")):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "registry" in text:
            yield path, text


@functools.lru_cache(maxsize=1)
def _facade_reexports() -> frozenset[str]:
    """Dotted paths that name the facade through a production module importing it.

    Read off the source the same way the guard reads a test, to a fixed point, so a
    module re-exporting another's alias is covered, and a new re-export needs no edit
    here.
    """
    candidates: list[tuple[str, bool, ast.Module]] = []
    for path, text in _python_files():
        if _module_name(path) is None or _in_a_test_directory(path):
            continue
        if "kiro_crew.apps" in text or "apps" in path.relative_to(_REPO_ROOT).parts:
            candidates.append((_module_name(path) or "", _is_package(path), ast.parse(text)))
    found: frozenset[str] = frozenset()
    while True:
        grown = set(found)
        for module, is_package, tree in candidates:
            resolver = _Resolver(tree, module, found, is_package=is_package)
            for name in resolver.module_names():
                if any(resolver.is_facade(v) for v in resolver.values(ast.Name(id=name), tree)):
                    grown.add(f"{module}.{name}")
        if grown == found:
            return found
        found = frozenset(grown)


def _may_pass_create(call: ast.Call) -> bool:
    """Whether *call* could hand a patch a ``create`` that is not literally ``False``.

    A ``create`` keyword that is not the literal ``False``, a ``**`` splat that could
    carry one, or three positional arguments or more -- where ``create`` could sit
    positionally under any alias of a patch callable. Only these calls are worth
    resolving; which callable a call names is the resolver's question.
    """
    for keyword in call.keywords:
        if keyword.arg == "create":
            return not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
    return any(keyword.arg is None for keyword in call.keywords) or len(call.args) >= 3


def _create_true_patches_of_forwarded_names(
    source: str,
    module: str | None = None,
    reexports: frozenset[str] | None = None,
    *,
    is_package: bool = False,
) -> list[_Hit]:
    """Every ``mock.patch`` of a forwarded name in *source* whose ``create`` is not
    literally ``False``.

    Any spelling the reader can resolve counts: ``patch``, ``patch.object`` and
    ``patch.multiple`` reached through any import alias, called or used as a
    decorator, with a positional or keyword target and attribute, a keyword
    ``create`` -- or a positional one to a callable spelled ``patch``, ``object`` or
    ``multiple`` -- and a string target built from the facade's name. A patch of the
    facade whose attribute it cannot resolve is a ``<dynamic>`` hit, never a pass. A
    name the facade binds itself is safe -- ``mock.patch`` sees it as local and
    writes the original back.
    """
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and _may_pass_create(node)
    ]
    if not calls:
        return []
    resolver = _Resolver(
        tree,
        module,
        _facade_reexports() if reexports is None else reexports,
        is_package=is_package,
    )
    return [
        _Hit(resolver.function_of(call), name, call.lineno)
        for call in calls
        for name in resolver.hits(call)
    ]


def _patch_sources() -> Iterator[tuple[Path, str]]:
    """``(path, text)`` for every test module of the repository that may patch the facade.

    ``test/`` and every directory under ``src/`` whose name ends in ``tests``, handed
    to the AST reader when the text names ``registry`` and a patch helper. The word
    ``create`` is not required: it can arrive positionally or through a ``**`` splat.
    """
    for path, text in _python_files():
        relative = path.relative_to(_REPO_ROOT).as_posix()
        if not (relative.startswith("test/") or _in_a_test_directory(path)):
            continue
        if "patch" in text or "mock" in text:
            yield path, text


#: Imports most reader cases share.
_CASE_IMPORTS = "from unittest import mock\nfrom kiro_crew.apps import registry\n"

#: ``(id, source, module, expected names)`` for the reader: each form the guard must
#: catch beside a spelling of it the guard must leave alone. ``@EXP@`` is a forwarded
#: name, ``@BOUND@`` one the facade binds itself, ``@FACADE@`` the facade's dotted name.
_READER_CASES: list[tuple[str, str, str | None, list[str]]] = [
    (
        "patch.object",
        _CASE_IMPORTS + "mock.patch.object(registry, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "create=False",
        _CASE_IMPORTS + "mock.patch.object(registry, '@EXP@', create=False)\n",
        None,
        [],
    ),
    ("no create", _CASE_IMPORTS + "mock.patch.object(registry, '@EXP@')\n", None, []),
    (
        "create positionally",
        _CASE_IMPORTS + "mock.patch.object(registry, '@EXP@', mock.DEFAULT, None, True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "False positionally",
        _CASE_IMPORTS + "mock.patch.object(registry, '@EXP@', mock.DEFAULT, None, False)\n",
        None,
        [],
    ),
    (
        "create not a literal",
        _CASE_IMPORTS + "flag = True\nmock.patch.object(registry, '@EXP@', create=flag)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a name the facade binds",
        _CASE_IMPORTS + "mock.patch.object(registry, '@BOUND@', create=True)\n",
        None,
        [],
    ),
    (
        "a name no module holds",
        _CASE_IMPORTS + "mock.patch.object(registry, '_no_such_registry_helper', create=True)\n",
        None,
        [],
    ),
    (
        "another module",
        _CASE_IMPORTS
        + "from kiro_crew import config\nmock.patch.object(config, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "another module's .registry",
        _CASE_IMPORTS
        + "from kiro_crew import config\n"
        + "mock.patch.object(config.registry, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "the facade module imported by its path",
        "from unittest import mock\nimport kiro_crew.apps.registry as reg\n"
        "mock.patch.object(reg, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "the facade module reached through its package",
        "from unittest import mock\nimport kiro_crew.apps\n"
        "mock.patch.object(kiro_crew.apps.registry, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "patch as an alias",
        "from unittest.mock import patch as P\nfrom kiro_crew.apps import registry\n"
        "P.object(registry, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a local function named patch",
        "from kiro_crew.apps import registry\ndef patch(*a, **k):\n    return None\n"
        "patch.object(registry, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "mock as an alias",
        "from unittest import mock as M\nfrom kiro_crew.apps import registry\n"
        "M.patch.object(registry, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "unittest.mock as an alias",
        "import unittest.mock as um\num.patch('@FACADE@.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "unittest.mock by its path",
        "import unittest.mock\nfrom kiro_crew.apps import registry\n"
        "unittest.mock.patch.multiple(registry, create=True, @EXP@=None)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a third-party mock",
        "import mock\nfrom kiro_crew.apps import registry\n"
        "mock.patch.object(registry, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "as a decorator",
        _CASE_IMPORTS
        + "@mock.patch.object(registry, '@EXP@', create=True)\ndef test_x(fake):\n    pass\n",
        None,
        ["@EXP@"],
    ),
    (
        "a decorator with create=False",
        _CASE_IMPORTS
        + "@mock.patch.object(registry, '@EXP@', create=False)\ndef test_x(fake):\n    pass\n",
        None,
        [],
    ),
    (
        "a dotted string",
        "from unittest import mock\nmock.patch('@FACADE@.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a deeper dotted string",
        "from unittest import mock\nmock.patch('@FACADE@.os.utime', create=True)\n",
        None,
        [],
    ),
    (
        "another dotted string",
        "from unittest import mock\nmock.patch('kiro_crew.config.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "an f-string of __name__",
        _CASE_IMPORTS + "mock.patch(f'{registry.__name__}.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an f-string of another __name__",
        _CASE_IMPORTS
        + "from kiro_crew import config\nmock.patch(f'{config.__name__}.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "an f-string of a constant",
        "from unittest import mock\nFACADE = '@FACADE@'\n"
        "mock.patch(f'{FACADE}.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an f-string of another constant",
        "from unittest import mock\nOTHER = 'kiro_crew.config'\n"
        "mock.patch(f'{OTHER}.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "a constant concatenated",
        "from unittest import mock\nFACADE = '@FACADE@'\n"
        "mock.patch(FACADE + '.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "__name__ concatenated",
        _CASE_IMPORTS + "mock.patch(registry.__name__ + '.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "another constant concatenated",
        "from unittest import mock\nOTHER = 'kiro_crew.config'\n"
        "mock.patch(OTHER + '.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "keyword target and attribute",
        _CASE_IMPORTS + "mock.patch.object(target=registry, attribute='@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "keyword target elsewhere",
        _CASE_IMPORTS
        + "from kiro_crew import config\n"
        + "mock.patch.object(target=config, attribute='@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "keyword string target",
        "from unittest import mock\nmock.patch(target='@FACADE@.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an unresolved attribute",
        _CASE_IMPORTS + "def test_x(attr):\n    mock.patch.object(registry, attr, create=True)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "an unresolved attribute, create=False",
        _CASE_IMPORTS + "def test_x(attr):\n    mock.patch.object(registry, attr, create=False)\n",
        None,
        [],
    ),
    (
        "a resolved local attribute",
        _CASE_IMPORTS
        + "def test_x():\n    name = '@EXP@'\n    mock.patch.object(registry, name, create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an f-string it cannot finish",
        _CASE_IMPORTS
        + "def test_x(attr):\n    mock.patch(f'{registry.__name__}.{attr}', create=True)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "an aliased patch with a positional create",
        "from unittest.mock import patch as P\nfrom kiro_crew.apps import registry\n"
        "P.object(registry, '@EXP@', None, None, True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an aliased dotted-string patch with a positional create",
        "from unittest.mock import patch as P\nP('@FACADE@.@EXP@', None, None, True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a splat that may carry create",
        _CASE_IMPORTS + "def test_x(opts):\n    mock.patch.object(registry, '@EXP@', **opts)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a splat on another module",
        _CASE_IMPORTS
        + "from kiro_crew import config\n"
        + "def test_x(opts):\n    mock.patch.object(config, '@EXP@', **opts)\n",
        None,
        [],
    ),
    (
        "an alias whose chain loops back on itself",
        _CASE_IMPORTS
        + "a = registry\na = b\nb = a\n"
        + "mock.patch.object(a, '@EXP@', create=True)\n"
        + "mock.patch.object(b, '@EXP@', create=True)\n",
        None,
        ["@EXP@", "@EXP@"],
    ),
    (
        "**kwargs in patch.multiple",
        _CASE_IMPORTS + "def test_x(kw):\n    mock.patch.multiple(registry, create=True, **kw)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "**kwargs elsewhere",
        _CASE_IMPORTS
        + "from kiro_crew import config\n"
        + "def test_x(kw):\n    mock.patch.multiple(config, create=True, **kw)\n",
        None,
        [],
    ),
    (
        "patch.multiple of a dotted string",
        "from unittest import mock\nmock.patch.multiple('@FACADE@', create=True, @EXP@=None)\n",
        None,
        ["@EXP@"],
    ),
    (
        "patch.multiple of another string",
        "from unittest import mock\n"
        "mock.patch.multiple('kiro_crew.config', create=True, @EXP@=None)\n",
        None,
        [],
    ),
    (
        "import_module",
        "import importlib\nfrom unittest import mock\n"
        "facade = importlib.import_module('@FACADE@')\n"
        "mock.patch.object(facade, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "import_module elsewhere",
        "import importlib\nfrom unittest import mock\n"
        "other = importlib.import_module('kiro_crew.config')\n"
        "mock.patch.object(other, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "a chain of assignments",
        _CASE_IMPORTS + "a = registry\nb = a\nmock.patch.object(b, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an assignment inside the test",
        _CASE_IMPORTS
        + "def test_x():\n    reg = registry\n    mock.patch.object(reg, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a relative import in a package",
        "from unittest import mock\nfrom ... import registry\n"
        "mock.patch.object(registry, '@EXP@', create=True)\n",
        "kiro_crew.apps.builtins.tests.test_case",
        ["@EXP@"],
    ),
    (
        "a relative import elsewhere",
        "from unittest import mock\nfrom ..crew import registry\n"
        "mock.patch.object(registry, '@EXP@', create=True)\n",
        "kiro_crew.apps.builtins.tests.test_case",
        [],
    ),
]

#: A production module that re-exports the facade under another name, for the two
#: cases below: the scan finds none today, so the reader is shown one.
_SYNTHETIC_REEXPORTS = frozenset({"kiro_crew.apps.routes.registry_mod"})

_REEXPORT_CASES: list[tuple[str, str, list[str]]] = [
    (
        "an assigned re-export",
        "from unittest import mock\nfrom kiro_crew.apps import routes\n"
        "def test_x():\n    reg = routes.registry_mod\n"
        "    mock.patch.object(reg, '@EXP@', create=True)\n",
        ["@EXP@"],
    ),
    (
        "an assigned other attribute",
        "from unittest import mock\nfrom kiro_crew.apps import routes\n"
        "def test_x():\n    reg = routes.registry_other\n"
        "    mock.patch.object(reg, '@EXP@', create=True)\n",
        [],
    ),
]


def _case(template: str) -> str:
    return (
        template.replace("@EXP@", _MULTI_HOLDER)
        .replace("@BOUND@", "_run_app_build")
        .replace("@FACADE@", registry.__name__)
    )


class TestPatchSpellings:
    def test_the_case_names_are_what_they_claim(self) -> None:
        assert _MULTI_HOLDER in registry._EXPORTS
        assert "_run_app_build" in vars(registry) and "_run_app_build" not in registry._EXPORTS
        assert "_no_such_registry_helper" not in registry._EXPORTS
        assert not hasattr(registry, "_no_such_registry_helper")

    def test_every_production_alias_the_scan_finds_is_the_facade(self) -> None:
        # Production code reaches the facade by name or by a function-local import
        # today (``platform/defaults.py``), so the scan finds no module-level alias; a
        # new one extends the guard's reach with no edit here, and whatever it finds
        # must really be the facade at run time.
        for dotted in _facade_reexports():
            module, _, name = dotted.rpartition(".")
            assert getattr(importlib.import_module(module), name) is registry, dotted

    @pytest.mark.parametrize(
        ("is_package", "expected"),
        [(True, [_MULTI_HOLDER]), (False, [])],
        ids=["a package __init__", "a plain module of the same name"],
    )
    def test_a_relative_import_resolves_against_the_right_package(
        self, is_package: bool, expected: list[str]
    ) -> None:
        # ``from ... import registry`` in ``kiro_crew/apps/builtins/tests/__init__.py``
        # names ``kiro_crew.apps.registry``; in a plain ``kiro_crew/apps/builtins/tests.py``
        # it names ``kiro_crew.registry``, which is not the facade.
        source = _case(
            "from unittest import mock\nfrom ... import registry\n"
            "mock.patch.object(registry, '@EXP@', create=True)\n"
        )
        hits = _create_true_patches_of_forwarded_names(
            source, "kiro_crew.apps.builtins.tests", is_package=is_package
        )
        assert [hit.name for hit in hits] == expected

    @pytest.mark.parametrize(
        ("source", "module", "expected"),
        [(case[1], case[2], case[3]) for case in _READER_CASES],
        ids=[case[0] for case in _READER_CASES],
    )
    def test_the_reader_flags_every_spelling_and_only_those(
        self, source: str, module: str | None, expected: list[str]
    ) -> None:
        # A reader that missed a spelling would pass a suite using it; one that flagged
        # a safe spelling would stop a patch that undoes cleanly.
        hits = _create_true_patches_of_forwarded_names(_case(source), module)
        assert [hit.name for hit in hits] == [_case(name) for name in expected]

    @pytest.mark.parametrize(
        ("source", "expected"),
        [(case[1], case[2]) for case in _REEXPORT_CASES],
        ids=[case[0] for case in _REEXPORT_CASES],
    )
    def test_the_reader_follows_a_production_re_export(
        self, source: str, expected: list[str]
    ) -> None:
        hits = _create_true_patches_of_forwarded_names(
            _case(source), None, reexports=_SYNTHETIC_REEXPORTS
        )
        assert [hit.name for hit in hits] == [_case(name) for name in expected]

    def test_no_test_patches_a_forwarded_name_with_create_true(self) -> None:
        # ``mock.patch`` undoes a name the facade forwards by deleting it, and under
        # ``create=True`` the delete is the whole undo: the name is gone from every
        # registry module for the rest of the run, and a later test fails far from
        # here. The scan must find exactly the allowlisted premise case, so the
        # allowlist can neither hide a second site nor outlive the one it names.
        hits = [
            (path.relative_to(_REPO_ROOT).as_posix(), hit)
            for path, text in _patch_sources()
            for hit in _create_true_patches_of_forwarded_names(
                text, _module_name(path), is_package=_is_package(path)
            )
        ]
        found = {(path, hit.function) for path, hit in hits}
        # One hit exactly: the premise case holds a single patch, so a second site
        # added inside the same function is not hidden by the allowlist.
        assert len(hits) == len(_ALLOWED_CREATE_TRUE), hits
        unexpected = [
            f"{path}:{hit.line} {hit.function} patches {hit.name}"
            for path, hit in hits
            if (path, hit.function) not in _ALLOWED_CREATE_TRUE
        ]
        assert found == _ALLOWED_CREATE_TRUE, (
            "mock.patch(..., create=True) of a name kiro_crew.apps.registry forwards to "
            "its owner deletes that name from every registry module when the patch "
            "exits. Drop create=True (the name exists) or patch it with "
            f"monkeypatch.setattr: {unexpected}; allowlisted but not found: "
            f"{sorted(_ALLOWED_CREATE_TRUE - found)}"
        )
