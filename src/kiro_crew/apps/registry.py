"""App registry — curated list of available KiroCrew apps.

The registry JSON (``app-registry.json``) is a minimal index: just app name,
git URL, branch, and install metadata.  All display information (description,
screenshots, highlights, tags, platform) comes from each app's own
``app.json``, fetched on demand and cached locally.

This "single source of truth" design means app authors only maintain their
own ``app.json`` — they never need to update the KiroCrew registry JSON
when changing descriptions, screenshots, or versions.

Each registry entry identifies the source repository via a ``gitUrl`` field
(any git-cloneable URL — ``https://github.com/...``, ``git@host:...``, etc.).
The legacy ``repo`` field is still accepted and, when no ``gitUrl`` is given,
is used as a clone target directly (so a full URL may be placed in ``repo``).

SECURITY — Trust model:
  registry JSON (gitUrl + branch) → ``git clone`` from the configured host →
  read app.json → execute setup.onInstall script.

The registry entry itself is curated/reviewed before being shipped, and the
install script in app.json has the same trust level as any code you clone
and build locally.  Install scripts run sandboxed via ``wrap_argv`` with a
minimal environment that excludes process secrets.

This module is the registry's only import path and patch surface. Its rules live
in private owners under :mod:`kiro_crew.apps.registry_pipeline`, one per
responsibility (``docs/system-specs/modules/app-kit-platform.md`` §20 has the
map), and every name they hold resolves here. Three constructs stay in this file
because repository guards read them here by path: the build step
(:func:`_run_app_build`) and the two index-entry name gates
(:func:`_admit_cached_index_entries`, :func:`_admit_fetched_index_entries`).
"""

from __future__ import annotations

import asyncio
import builtins as _builtins
import importlib as _importlib
import importlib.util
import json
import logging
import shutil
import sys as _sys
import typing as _typing
from pathlib import Path
from types import ModuleType as _ModuleType
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
    wrap_argv_async,
)

# ``importlib.reload`` of this module re-executes it in its existing namespace, and
# the one-module registry re-evaluated every module-level value when that happened --
# a platform-dependent constant such as ``_GIT_CLONE_LOCALE`` included. The owners
# hold those values now, so a reload (the only way ``_PART_MODULES`` is already
# bound at this line) reloads each owner, lowest layer first, and every owner rebinds
# its imports from the freshly executed owners below it.
if "_PART_MODULES" in globals():
    for _reloaded in globals()["_PART_MODULES"]:
        _importlib.reload(_sys.modules[_reloaded])
    del _reloaded

from kiro_crew.apps.manager import InstalledTreeRefused  # noqa: E402
from kiro_crew.apps.manifest import AppManifest  # noqa: E402
from kiro_crew.apps.registry_pipeline.checkout import _kill_process_group  # noqa: E402
from kiro_crew.apps.registry_pipeline.install import (  # noqa: E402
    DESKTOP_BUILD_STEP_UNSUPPORTED,
    _desktop_gate_probe,
    _InstallVerb,
    _refusal_line,
)
from kiro_crew.apps.registry_pipeline.manifests import _is_safe_registry_subdir  # noqa: E402
from kiro_crew.apps.registry_pipeline.subprocess_env import minimal_env  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The build step
# ---------------------------------------------------------------------------

_BUILD_TIMEOUT = 600  # 10 minutes — frontend bundlers / packagers can be slow


async def _run_app_build(
    build_dir: Path,
    app_name: str,
    log_lines: list[str],
    *,
    manifest: AppManifest,
    self_managed: bool,
    verb: _InstallVerb = "install",
) -> dict[str, Any]:
    """Build a cloned app using a sensible default for its ecosystem.

    Detection (in order):
      - ``package.json``      → ``npm install`` (+ ``npm run build`` if a
                                 ``build`` script is declared)
      - ``pyproject.toml`` /
        ``setup.py`` /
        ``requirements.txt``  → ``pip install .`` (or ``-r requirements.txt``)
      - otherwise             → no build step (source is used as-is)

    *manifest* is the CLONED ``app.json``, typed — the same object the admission
    gate judged, and normalized the way the runtime's own loaders see it. It
    decides one thing only: whether a bundled interpreter may pass a
    requirements-only app through to the runtime's own provisioning (see
    :func:`_requirements_owned_by_the_runtime`). Required rather than
    defaulted, so a caller states what the app declares instead of inheriting a
    verdict; an empty ``AppManifest`` is the honest value for "declares nothing",
    and it refuses.

    *self_managed* is the registry entry's resource ownership (``resources:
    "app"``), the other input of that verdict: the runtime provisions only the
    apps it installs and spawns itself, and a self-managed app is registered from
    its manifest alone, so its ``requirements.txt`` keeps the refusal. Required
    for the same reason -- ``False`` is the permissive value and must be stated by
    the caller that read the entry.

    *verb* names the action in the streamed refusal line ("install" or
    "update", from the installed record), as :func:`_refuse_identity_mismatch`
    describes; it changes no verdict.

    The app's own ``setup.onInstall`` script (run later by
    ``install_from_registry``) can perform any additional steps.  A missing
    build toolchain (no npm / no pip) is treated as a soft failure: the step
    is skipped with a logged warning rather than aborting the install, so an
    app that needs no build still installs cleanly.
    """
    build_cmds: list[list[str]] = []

    if (build_dir / "package.json").is_file():
        # Resolve to a full path, mirroring the pip branch below: on Windows npm
        # is ``npm.CMD``, which shutil.which finds but CreateProcess cannot spawn
        # by the bare name "npm".
        npm = shutil.which("npm")
        if npm:
            build_cmds.append([npm, "install"])
            try:
                pkg = json.loads((build_dir / "package.json").read_text("utf-8"))
                if (pkg.get("scripts") or {}).get("build"):
                    build_cmds.append([npm, "run", "build"])
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                pass
        else:
            log_lines.append("npm not found on PATH — skipping JavaScript build step")
    elif platform_compat.is_bundled_interpreter():
        # The desktop gate decides for the bundled interpreter, ahead of the
        # `is_file` detection the pip branch below uses, because it decides on
        # the provisioner's own notion of presence (`lexists`): a dangling
        # requirements.txt link that a declared consumer would try to read is
        # refused here exactly as `provision_app_deps` refuses it at spawn,
        # where `is_file` would have called it "no build step" and installed an
        # app whose backend spawns without its deps and dies on import. What the
        # refusal is about, and why a
        # runtime-provisioned requirements.txt is waived, is the comment on the
        # pip branch below; `_desktop_build_refusal` owns the verdict, and
        # `install_from_registry` asks it again on the FINAL checkout, after
        # `setup.onInstall` has run, so a script that rewrites the manifest cannot
        # keep a waiver judged on the manifest that entered it. Off-loop: its
        # layout probes stat the checkout, which can sit on a stalling network
        # mount.
        try:
            refusal, has_requirements = await asyncio.to_thread(
                _desktop_gate_probe, build_dir, manifest, self_managed
            )
        except InstalledTreeRefused as exc:
            # The gate's preview copy produced a tree `install_app` would refuse
            # (a root `data` that is not a directory) -- the install's own refusal,
            # raised before any transaction touched the app directory. A build
            # failure like any other for the caller's rollback, and NOT the
            # desktop code: a browser install refuses the same tree, so the
            # reader must not be told to install there instead.
            log_lines.append(_refusal_line(verb, exc))
            return {"ok": False, "name": app_name, "error": str(exc)}
        if refusal:
            # Streamed like the sibling arm above and the final pass: the log the
            # page shows must hold the refusal on this, the commonest path (an
            # already-trusted app whose checkout carries a build step).
            log_lines.append(_refusal_line(verb, refusal))
            return {
                "ok": False,
                "name": app_name,
                "error": refusal,
                "code": DESKTOP_BUILD_STEP_UNSUPPORTED,
            }
        if has_requirements:
            log_lines.append(
                "requirements.txt is provisioned at runtime into this app's own deps "
                "directory (for its backend entry point or stdio MCP server), so no "
                "install-time pip step runs on the bundled interpreter"
            )
            return {"ok": True}
        # Nothing here that would build (or a requirements.txt entry nothing
        # reads): no build step, reported below like any source-as-is checkout.
    elif (
        (build_dir / "pyproject.toml").is_file()
        or (build_dir / "setup.py").is_file()
        or (build_dir / "requirements.txt").is_file()
    ):
        # `sys.executable -m pip`, NOT `shutil.which("pip")`.
        #
        # A Python app has to land in the interpreter that will IMPORT it — the one
        # running this gateway. `which("pip")` resolves to whatever pip is first on
        # PATH, which is routinely a different interpreter: `bin/kirocrew` execs
        # `.venv/bin/kirocrew` WITHOUT putting the venv's `bin/` on PATH, and
        # `service/common.py::service_path()` prepends `~/.local/bin` ahead of it. So the
        # build pip was whatever the user happened to have.
        #
        # The failure is SILENT, which is why it survived. Measured on a host whose first
        # pip was 3.7 and whose gateway venv was 3.12: with a version-incompatible pip the
        # install failed loudly, but with a *compatible-but-different* pip (3.10) it
        # reported "Successfully installed", the build step reported success, and the
        # package landed in `~/.local/lib/python3.10/site-packages` — invisible to the
        # gateway, with `ENABLE_USER_SITE = False` in a venv so there is no fallback. The
        # app installs, the entry point never appears, and nothing anywhere says why.
        #
        # Our own packages only fail loudly because they declare `requires-python`; a
        # third-party app without that constraint fails silently on EVERY mismatch.
        #
        # EXCEPTION: never run pip against the desktop app's bundled interpreter.
        # The desktop build ships a python-build-standalone runtime inside the
        # application bundle (`Resources/backend-dist/...`); on macOS that bundle is
        # code-signed, so a pip install writing into its site-packages invalidates
        # the signature and breaks subsequent launches/updates — and the write is
        # discarded on every app update anyway. This is a LOUD failure, not a
        # soft-skip: a Python app that declares a build step needs its packages
        # importable by the gateway, and skipping the install while reporting
        # success would recreate exactly the silent-broken-install shape this
        # function is written to prevent. Detection lives in
        # platform_compat.is_bundled_interpreter() — the single owner of the
        # packaging-layout sentinel — so a bundler rename breaks its pinned test
        # instead of silently un-matching an inline check here.
        #
        # What that refusal is ABOUT is the gateway's own import path, so it
        # applies to what would have to land there: `pyproject.toml` / `setup.py`
        # install INTO this interpreter (`pip install .`). A root requirements.txt
        # the RUNTIME provisions out of process is a different dependency:
        # `backend.py::provision_app_deps` (at the spawn of a `backend.entryPoint`)
        # and `bridges.py::_maybe_provision_backendless_deps` (at the
        # registration of a stdio `mcpServers` entry) both install that same file
        # with `pip install --target` into the app's own deps dir, which works on
        # the bundled interpreter and never touches the bundle. Refusing it here
        # would block exactly the app classes the runtime serves, so it passes
        # the gate and NOTHING is pip-installed at install time — the runtime owns
        # it. This is a capability check that matches what the runtime can do,
        # not a widening of any boundary: the same file, on the same
        # interpreter, is already provisioned by the runtime.
        #
        # The waiver is the runtime's own condition, mirrored in
        # `_requirements_owned_by_the_runtime`: the shared provisioning predicate
        # says an out-of-process consumer is declared in a shape the provisioners
        # actually serve (a FILE-style entry point, or a stdio server — one
        # without `url`; a module-style, dotted entry point is never provisioned
        # and keeps the refusal) AND no `backend.hooks` field is, because a hook
        # is imported INTO this process, which the app deps tree deliberately
        # never reaches — AND the registry entry is gateway-managed, because the
        # provisioners run only for apps the gateway installs and spawns itself;
        # a self-managed entry (`resources: "app"`) is registered from its
        # manifest alone, so nothing would ever install its file — AND the file
        # is one the provisioner will read: a regular file, or a link that
        # strictly resolves inside the app root (`requirements_in_tree`, the
        # provisioner's own fast-refusal rule, shared); a link escaping the root
        # is refused at spawn, so it is refused here.
        #
        # requirements.txt BESIDE pyproject.toml/setup.py keeps the refusal (the
        # non-bundled branch below runs `pip install .` for that layout, so the
        # gateway-import dependency is the one that decides), and a
        # requirements.txt with NO out-of-process consumer keeps it too — nothing
        # would provision it, so a pass would be the silent-broken install.
        #
        # All of that is decided by the bundled-interpreter branch ABOVE this one,
        # so this branch is the non-bundled build only, and pip runs here.
        #
        # A missing `pip` module is a soft skip, exactly like a missing npm
        # (see the docstring). `sys.executable` is the gateway interpreter, and a
        # venv created with `--without-pip` — or any minimal runtime — has no `pip`
        # module: running `-m pip` against it exits non-zero and would abort the
        # whole registry install. Probe with `find_spec` on THIS interpreter (no
        # subprocess: it is the interpreter that would run the build) and skip when
        # pip is absent, so an app that needs no Python build still installs cleanly.
        if importlib.util.find_spec("pip") is None:
            log_lines.append(
                "pip not available in the gateway interpreter — skipping Python build step"
            )
        else:
            pip_cmd = platform_compat.isolated_python_argv("-m", "pip")
            if (build_dir / "requirements.txt").is_file() and not (
                (build_dir / "pyproject.toml").is_file() or (build_dir / "setup.py").is_file()
            ):
                build_cmds.append([*pip_cmd, "install", "-r", "requirements.txt"])
            else:
                build_cmds.append([*pip_cmd, "install", "."])

    if not build_cmds:
        log_lines.append("No build step detected — using source as-is")
        return {"ok": True}

    for cmd in build_cmds:
        log_lines.append(f"Running {' '.join(cmd)} in {build_dir}...")
        sandboxed_cmd, _cleanup = await wrap_argv_async(cmd, mode="standard", _prepare=wrap_argv)
        sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
        proc = await create_subprocess_limited(
            *sandboxed_cmd,
            cwd=str(build_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            env=minimal_env(),
        )
        assert proc.stdout is not None

        async def _drain() -> None:
            async for raw_line in proc.stdout:  # type: ignore[union-attr]
                log_lines.append(raw_line.decode(errors="replace").rstrip())
            await proc.wait()

        try:
            await asyncio.wait_for(_drain(), timeout=_BUILD_TIMEOUT)
        except asyncio.TimeoutError:
            await _kill_process_group(proc)
            return {
                "ok": False,
                "name": app_name,
                "error": f"build timed out after {_BUILD_TIMEOUT}s ({' '.join(cmd)})",
            }

        if proc.returncode != 0:
            return {
                "ok": False,
                "name": app_name,
                "error": f"build failed (exit {proc.returncode}): {' '.join(cmd)}",
            }

    log_lines.append("build succeeded")
    return {"ok": True}


# ---------------------------------------------------------------------------
# The index-entry name gates
# ---------------------------------------------------------------------------
# An external registry index is untrusted input, and every entry it lists reaches a
# filesystem operation by name: ``app_source_dir(name)`` joins it under the
# app-sources root, and on a failed clone ``_git_clone_or_pull`` removes that path.
# A hostile or mistyped name such as ``/tmp/victim`` or ``../../victim`` would escape
# the root, so an entry whose name is not valid kebab-case (the same ``KEBAB_RE``
# gate install and register enforce) never reaches the cache or the listing. The
# untrusted ``subdirectory`` is gated the same way, as defense in depth beside
# ``_contained_join`` at each use. A fresh fetch filters before it writes the cache
# and every cache read filters again, so a cache written by an older build, or edited
# by hand, cannot reintroduce a traversing entry.


def _admit_cached_index_entries(data: list[Any]) -> list[dict[str, Any]]:
    """The entries of a cached index that a read may return, in cache order.

    Called on EVERY cache read, stale-fallback reads included, by
    :func:`~kiro_crew.apps.registry_pipeline.caches._read_external_registry_cache`.
    A non-object item is dropped silently; a bad name or subdirectory is dropped with
    a warning that names neither, because a cache file is not a place to trust text.
    """
    from kiro_crew.apps.manifest import KEBAB_RE

    safe: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        entry_name = entry.get("name")
        if not isinstance(entry_name, str) or not KEBAB_RE.fullmatch(entry_name):
            logger.warning(
                "Dropping cached external registry entry with invalid name "
                "(must be lowercase kebab-case)"
            )
            continue
        # ``subdirectory`` is untrusted index content joined to the clone /
        # app-source roots; drop any entry whose value is absolute or
        # traversing so it can never reach a filesystem op (same rationale
        # as the name gate above). Fresh fetches are filtered before write;
        # re-filter here so a cached/stale/hand-tampered file cannot
        # reintroduce a traversing subdirectory.
        if not _is_safe_registry_subdir(entry.get("subdirectory", "")):
            logger.warning(
                "Dropping cached external registry entry with unsafe subdirectory "
                "(must be a contained relative path)"
            )
            continue
        safe.append(entry)
    return safe


def _admit_fetched_index_entries(entries: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """The entries of a freshly fetched index that may be cached and listed.

    Called by
    :func:`~kiro_crew.apps.registry_pipeline.indexes._fetch_and_cache_external_registry`
    before anything is written. *name* is the registry's public name, which each
    warning carries so a dropped entry can be traced to the index that listed it.
    """
    from kiro_crew.apps.manifest import KEBAB_RE

    valid_entries: list[dict[str, Any]] = []
    for entry in entries:
        entry_name = entry.get("name")
        if not isinstance(entry_name, str) or not KEBAB_RE.fullmatch(entry_name):
            logger.warning(
                "Dropping external registry %s entry with invalid name %r "
                "(must be lowercase kebab-case)",
                name,
                entry_name,
            )
            continue
        # ``subdirectory`` is untrusted index content later joined to the clone
        # and persistent app-source roots; an absolute/``..`` value would escape
        # them and read/execute an attacker-selected app.json. Drop it before it
        # is cached or listed (defense-in-depth with _contained_join at use).
        if not _is_safe_registry_subdir(entry.get("subdirectory", "")):
            logger.warning(
                "Dropping external registry %s entry %r with unsafe subdirectory "
                "%r (must be a contained relative path)",
                name,
                entry_name,
                entry.get("subdirectory"),
            )
            continue
        valid_entries.append(entry)
    return valid_entries


# ---------------------------------------------------------------------------
# Composition: one import path and one patch surface over the owners
# ---------------------------------------------------------------------------
# The registry is split by responsibility across ``registry_pipeline``, and this
# module is its only import path. Callers and tests reach every name as
# ``registry.X`` -- private helpers included, because tests read and patch them -- so
# two properties hold.
#
# 1. A read answers with the object the owner holds. A name this module does not use
#    itself is NOT bound here: ``__getattr__`` reads it from its owner on each access,
#    through ``sys.modules``, which is the one-storage rule
#    ``test_mirrored_owner_storage.py`` enforces on every module of this shape. The
#    names this module's own functions use are bound here by ordinary imports, the
#    same way each owner binds what it imports from a lower owner.
# 2. A write reaches every binding of the name. An owner resolves a name through its
#    own globals, and so does every owner that imported it, so a patch that landed
#    only on this module would leave the code under test running the unpatched
#    object -- the test would pass while testing nothing. ``_Facade`` therefore
#    writes the value into every module that holds the name, which keeps the
#    registry one namespace for writes: shadowing a builtin reaches every owner as
#    well. Patch the facade, never an owner directly: a write into one owner reaches
#    no other holder.
#
# Every owner is imported here, at the facade's own import. An
# owner's ``from ... import`` bindings are therefore taken once, as the one-module
# registry took them, and never on a first use that could fall inside a test's
# patch.
#
# One consequence of (1) is visible to a patch harness. ``mock.patch`` undoes a name
# this module does not bind by deleting it and then writing the original back -- and
# under ``create=True`` it only deletes -- and the delete reaches every holder. So a
# patch of such a name never passes ``create=True``: the composition-contract test
# fails on any test module that patches a forwarded name with ``create=True``, apart
# from its one allowlisted premise case.
#
# The machinery below holds dotted module NAMES, never module objects, and reads
# ``sys``, ``importlib`` and ``builtins`` through private aliases a patch of
# ``registry.sys`` cannot redirect. The composition-contract test pins that every
# module holding a name holds the SAME object, so a name and the symbol it denotes
# cannot come apart.

#: The owners, lowest layer first. An owner imports only owners earlier in this
#: order, so for a name the registry defines, the first owner holding it is its
#: definer; a name an owner imports from outside the registry resolves from its first
#: importer, which holds the same object as every other holder.
_PART_MODULES: tuple[str, ...] = tuple(
    f"{__name__.rpartition('.')[0]}.registry_pipeline.{leaf}"
    for leaf in (
        "subprocess_env",
        "git_targets",
        "caches",
        "sources",
        "recovery",
        "checkout",
        "indexes",
        "manifests",
        "catalog",
        "install",
    )
)

#: Builtin names, which a module shadows by binding them in its own namespace.
_BUILTIN_NAMES = frozenset(name for name in vars(_builtins) if not name.startswith("__"))


def _part(module: str) -> _ModuleType:
    """Return one owner, read from where modules are stored.

    :data:`sys.modules` answers first, so a purged or replaced owner is seen at once.
    ``importlib.import_module`` answers only a miss: it is an attribute any test can
    patch, and resolving every read through it would reroute this whole surface to
    that patch while it is installed.
    """
    try:
        return _sys.modules[module]
    except KeyError:
        return _importlib.import_module(module)


def _holder_tables() -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """``(exported, also_held)``: the owners holding each name, lowest layer first.

    A name this module binds itself goes to the second table, and only the owners that
    hold the SAME object count as holders of it; every other name an owner holds goes
    to the first.
    """
    own = globals()
    exported: dict[str, list[str]] = {}
    also_held: dict[str, list[str]] = {}
    for module in _PART_MODULES:
        for name, value in list(vars(_part(module)).items()):
            if name.startswith("__"):
                continue
            if name in own:
                if own[name] is value:
                    also_held.setdefault(name, []).append(module)
            else:
                exported.setdefault(name, []).append(module)
    return (
        {name: tuple(holders) for name, holders in exported.items()},
        {name: tuple(holders) for name, holders in also_held.items()},
    )


_holder_split = _holder_tables()

#: Name -> the owners that hold it, lowest layer first, for every name an owner holds
#: and this module does not bind. A read resolves the first; a write reaches them all.
_EXPORTS: dict[str, tuple[str, ...]] = _holder_split[0]

#: Name -> the owners that hold a name this module ALSO binds for its own functions.
#: A read answers from the binding here; a write reaches this module and all of them.
_ALSO_HELD: dict[str, tuple[str, ...]] = _holder_split[1]

del _holder_split


def _holders(name: str) -> tuple[str, ...]:
    """The owners a write of ``name`` through this module has to reach."""
    held = _EXPORTS.get(name) or _ALSO_HELD.get(name)
    if held is not None:
        return held
    return _PART_MODULES if name in _BUILTIN_NAMES else ()


if _typing.TYPE_CHECKING:
    # The exported names, as the type checker sees them: every one of them resolves
    # from its owner at run time through ``__getattr__`` below, which a checker is not
    # shown, so a misspelled or mis-called ``registry.X`` stays a type error. The
    # composition-contract test pins this list equal to ``_EXPORTS``.
    from kiro_crew.apps.registry_pipeline.caches import (  # noqa: F401
        _CACHE_EXPIRY_BACKDATE_SLACK,
        _EXTERNAL_REGISTRY_CACHE_TTL,
        _FACADE,
        _MANIFEST_CACHE_GC_GRACE,
        _MANIFEST_CACHE_TTL,
        _MANIFEST_SOURCE_SUBDIR,
        _credential_free_external_registry_entries,
        _credential_free_external_registry_value,
        _expire_cache_file,
        _external_registry_cache_identity,
        _external_registry_cache_path,
        _external_registry_cache_path_for_identity,
        _facade,
        _gc_manifest_cache_dir,
        _legacy_external_registry_cache_path,
        _manifest_cache_dir,
        _manifest_cache_path,
        _manifest_source_coordinates,
        _read_external_registry_cache,
        _read_manifest_cache,
        _remove_legacy_credential_registry_cache,
        _remove_legacy_name_keyed_registry_cache,
        _safe_cache_stem,
        _write_external_registry_cache,
        _write_manifest_cache,
        atomic_write,
        config_dir,
        sha256,
        time,
    )
    from kiro_crew.apps.registry_pipeline.catalog import (  # noqa: F401
        FIRST_PARTY_AUTHORS,
        SOURCE_REGISTRY_PREFIX,
        _append_external_registry_apps,
        _apply_trust_fields,
        _catalog_installable_rows,
        _catalog_row_supersedes_seed,
        _detect_installed_probe,
        _enrich_with_install_status,
        _external_registry_app_by_repo,
        _external_registry_repos,
        _external_registry_row,
        _fold_author,
        _identity,
        _is_catalog_row,
        _is_external_row,
        _pinned_registry_entry,
        _platform,
        _registry_app_candidates,
        _resolve_install_entry,
        _resolve_registry_row,
        _seed_row,
        _trust_repository_bindings,
        _version_newer,
        app_execution_denied,
        datetime,
        get_app,
        get_registry_app,
        get_registry_app_by_repo,
        get_server_platform,
        is_registry_source,
        known_registry_repos,
        list_catalog_apps,
        list_installed_apps,
        list_registry,
        official_catalog,
        refresh_registries,
        registry_name_from_source,
        resolve_installed_trust_repository,
        sandboxed_spawn_argv,
        sandboxed_spawn_argv_async,
        shipped_builtin_names,
        timezone,
        unicodedata,
    )
    from kiro_crew.apps.registry_pipeline.checkout import (  # noqa: F401
        _CLONE_TIMEOUT,
        _COMMIT_SHA_RE,
        _HEAD_READ_LIMIT,
        _KILL_GRACE_PERIOD,
        _PACKED_REFS_READ_LIMIT,
        _clone_branch_matches,
        _clone_origin_matches,
        _clone_origin_url,
        _communicate_with_timeout,
        _git_clone_or_pull,
        _git_fetch_branch,
        _git_fetch_commit,
        _git_fetch_ref,
        _read_clone_branch,
        _read_git_metadata_bounded,
        _resolved_clone_commit,
        _rmtree_force_settled,
    )
    from kiro_crew.apps.registry_pipeline.git_targets import (  # noqa: F401
        _GIT_AUTH_FAILURE_MARKERS,
        _GIT_FAILURE_CLASS_LABELS,
        _PUBLIC_GIT_HOSTS,
        IPv6Address,
        _clone_sandbox_mode,
        _entry_git_url,
        _git_output_is_auth_shaped,
        _git_target_has_ambiguous_scp_prefix,
        _git_target_has_ambiguous_ssh_userinfo,
        _git_target_has_query_or_fragment,
        _git_target_is_unsupported,
        _git_transport_env,
        _git_url_host,
        _is_ssh_git_url,
        _loggable_git_transport_output,
        _looks_like_git_url,
        _normalize_git_target,
        _normalized_ipv6_literal,
        _public_registry_name,
        _redact_url_userinfo,
        _redacted_git_failure_class,
        _same_git_target,
        _strip_git_target_userinfo,
        _valid_git_port,
        re,
    )
    from kiro_crew.apps.registry_pipeline.indexes import (  # noqa: F401
        _apply_configured_branch,
        _fetch_and_cache_external_registry,
        _fetch_external_registry_index,
        _load_external_registries,
        _owner_tier_confirmed,
    )
    from kiro_crew.apps.registry_pipeline.install import (  # noqa: F401
        _DESKTOP_BUILD_REFUSAL,
        _DESKTOP_LAYOUT_FILES,
        _REFUSAL_LINES,
        _SCRIPT_TIMEOUT,
        RESERVED_APP_NAME_CODE,
        Iterator,
        Literal,
        StreamingLogLines,
        _absent,
        _clone_build_app,
        _clone_build_app_locked,
        _desktop_build_refusal,
        _desktop_layout_present,
        _installed_tree_preview,
        _layout_cleanup_escaped,
        _official_entry,
        _provisioning_declared,
        _refuse_identity_mismatch,
        _remote_controlled_url,
        _remove_new_layout_files,
        _report_retained_stale_checkouts,
        _requirements_owned_by_the_runtime,
        _retained_startup_refusal,
        _roll_back_post_script_refusal,
        _set_aside_new_layout_files,
        _unpoison_rejected_checkout,
        app_admission_denied,
        app_name_error,
        contextmanager,
        copy_app_tree_as_installed,
        install_app,
        install_from_registry,
        install_receipt,
        is_module_style_entry_point,
        is_reserved_app_name,
        preserved_data_awaits,
        registry_source_repository,
        repository_bound_grant_denied,
        requirements_in_tree,
        runtime_provisions_requirements,
        sel,
        set_app_provenance,
        spawn_launches_entry_point_as_python,
        trusted_app_repository,
        update_app,
        verified_signer,
    )
    from kiro_crew.apps.registry_pipeline.manifests import (  # noqa: F401
        _REGISTRY_ROW_KEYS,
        _contained_join,
        _fetch_app_manifest,
        _merge_manifest,
        _resolve_manifest,
        _store_asset_path,
        posixpath,
    )
    from kiro_crew.apps.registry_pipeline.recovery import (  # noqa: F401
        _STALE_CHECKOUT_PATTERN,
        _STALE_CHECKOUT_RETENTION_DAYS,
        _app_sources_dir,
        _is_stale_candidate,
        _move_checkout_aside,
        _MoveAsideUndoFailed,
        _rename_and_refresh_mtime,
        _restorable_or_none,
        _restore_moved_aside,
        _stale_sibling,
        _sweep_stale_checkouts,
        _sweep_stale_checkouts_sync,
        app_source_dir,
        uuid,
    )
    from kiro_crew.apps.registry_pipeline.sources import (  # noqa: F401
        _REGISTRY_FILE,
        _REGISTRY_REVIEW_TIERS,
        _REGISTRY_TRUST_TIERS,
        _REVIEW_COMMUNITY,
        _REVIEW_CURATED,
        _REVIEW_UNSET,
        _TRUST_INDEX,
        _TRUST_OWNER,
        PlatformCompositionError,
        _configured_registry_hosts,
        _context_clone_sandbox_mode,
        _edition_registry_rows,
        _effective_registries,
        _install_coordinates,
        _is_owner_designated_repo,
        _is_supported_registry_transport,
        _load_registry_file,
        _owner_designated_repo_target,
        _pinned_registries,
        _registry_identity_key,
        _registry_trust_tier,
        _sel_credential_decision,
        _sel_credential_grant,
        _sel_fn,
        current_context,
        is_clone_host_trusted,
    )
    from kiro_crew.apps.registry_pipeline.subprocess_env import (  # noqa: F401
        _DETECT_PROBE_ENV_KEYS,
        _GIT_CLONE_LOCALE,
        _GIT_CREDENTIAL_ENV_KEYS,
        _SAFE_ENV_KEYS,
        _detect_probe_env,
        _is_probe_env_key,
        _is_safe_env_key,
        anonymous_git_env,
        os,
        scrub_env,
        sys,
    )
else:

    def __getattr__(name: str) -> Any:
        """Read an exported name from the owner that holds it (:pep:`562`)."""
        holders = _EXPORTS.get(name)
        if holders is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(_part(holders[0]), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


class _Facade(_ModuleType):
    """Write a name into every module that holds it.

    An exported name is written to its owners only, so this module never holds a copy
    that would shadow the owner and go stale on the owner's next write. ``monkeypatch``
    and ``mock.patch`` restore by writing the remembered value back through here, so a
    patch and its undo reach the same bindings.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        for module in _holders(name):
            setattr(_part(module), name, value)
        if name not in _EXPORTS:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        for module in _holders(name):
            part = _part(module)
            if name in vars(part):
                delattr(part, name)
        if name not in _EXPORTS:
            super().__delattr__(name)


# ``from ... import *`` consults this list and never reaches ``__getattr__``, so it
# is derived to carry the public names a star import of the one-module registry
# would: the names bound here plus the exported ones, minus the private names a
# star import never carries.
__all__ = sorted(name for name in set(globals()) | set(_EXPORTS) if not name.startswith("_"))

# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
_sys.modules[__name__].__class__ = _Facade
