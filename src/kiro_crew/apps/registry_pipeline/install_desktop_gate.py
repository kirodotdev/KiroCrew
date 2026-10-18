"""The desktop build-step gate: whether the bundled interpreter can install a checkout.

``_desktop_build_refusal`` is the one owner of that verdict. The build step asks it
before planning a Python build command, through ``_desktop_gate_probe``, and the
install transaction asks it again on the final checkout after ``setup.onInstall``.
Its tree-reading half judges the copy the install would produce
(``_installed_tree_preview``), with the runtime's own provisioning predicates.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.apps.manager import copy_app_tree_as_installed, preserved_data_awaits
from kiro_crew.apps.manifest import (
    AppManifest,
    is_module_style_entry_point,
    requirements_in_tree,
    runtime_provisions_requirements,
    spawn_launches_entry_point_as_python,
)
from kiro_crew.apps.registry_pipeline.recovery import _app_sources_dir


def _provisioning_declared(manifest: AppManifest, *, self_managed: bool) -> bool:
    """The half of :func:`_requirements_owned_by_the_runtime` that reads the
    manifest and the registry entry alone: False for a self-managed entry
    (nothing of ours provisions or spawns it) and for an app declaring
    ``backend.hooks`` (imported INTO the gateway process, which the deps tree never
    reaches). Spelled once, here, and asked by :func:`_desktop_build_refusal`
    BEFORE it copies the tree: a refusal these two decide needs no preview.
    """
    if self_managed:
        return False
    # `hooks.to_dict()` omits every blank field, so an empty dict IS "declares no
    # in-gateway hook", in the same emission the loaders are fed.
    return not manifest.backend.hooks.to_dict()


def _requirements_owned_by_the_runtime(
    manifest: AppManifest,
    installed: Path,
    *,
    source: Path,
    self_managed: bool,
    final: bool,
) -> bool:
    """True when the runtime provisions this app's root ``requirements.txt`` out
    of process and nothing of the app's Python imports into the gateway.

    This is the RUNTIME'S OWN condition, not an approximation of it: the
    out-of-process half is :func:`runtime_provisions_requirements`, the predicate
    both provisioners themselves call (``apps/backend.py`` at the spawn of a
    FILE-style ``backend.entryPoint``, ``apps/bridges_mcp.py`` at the registration of
    a stdio ``mcpServers`` entry), so a shape they refuse -- a module-style,
    dotted entry point, which executes trusted package code and never has an
    app-dir requirements file installed for it -- fails here too instead of
    passing an install whose dependencies land nowhere.

    The in-process half is this gate's own: a declared ``backend.hooks`` field is
    imported INTO the gateway process (``lifecycle.py``, ``route_registry.py``),
    which the deps tree never reaches, so an app declaring one has Python that
    must import from the gateway's interpreter after all -- waiving for it would
    install the app "successfully" with its hook imports broken and its routes
    degraded, the silent-broken install the loud refusal exists to prevent.

    *self_managed* is the registry entry's resource ownership (``resources:
    "app"``), which no manifest field carries and which decides whether either
    provisioner ever RUNS for this app: ``install_from_registry`` registers a
    self-managed app from its manifest alone -- no source is copied into the app
    directory, ``bridges_register.py`` skips every registration for it, and the app
    launches itself -- so the runtime never sees its ``requirements.txt``. On a
    source install the build step's ``pip install -r`` was the only thing that
    installed that file, and that step is exactly what the bundled interpreter
    cannot run: waiving for a self-managed app would report a successful install
    whose dependencies landed nowhere, so it keeps the refusal regardless of what
    its manifest declares. Required rather than defaulted, for the same reason
    *manifest* is on :func:`_run_app_build`: the permissive value must be stated
    by a caller that knows the entry, never inherited.

    *final* says which pass is asking. The union is the provisioners' exact
    condition, and the backend half requires the entry FILE to exist -- but the
    build pass runs before ``setup.onInstall``, whose window is exactly where an
    app may generate that file, so with ``final=False`` a declared Python-script
    entry that is merely absent still counts (nothing is pip-installed either
    way); with ``final=True``, on the post-script checkout, the union is applied
    as is and an entry that never appeared is refused there.

    Answers from the TYPED manifest, which is what decides what actually loads:
    ``manager_lookup.py`` hands the loaders ``AppManifest.from_json_file(...).to_dict()``,
    so a declaration ``BackendConfig.from_dict`` normalizes away is a hook the
    loaders never see either, and ``bridges_mcp.py`` reads ``manifest.mcpServers`` from
    the same typed object. *installed* is the tree the entry-point shape is
    judged against -- the copy ``install_app`` would produce from *source*, the
    checkout, which the spawn later resolves the same name under; *source* is
    read by the BUILD pass alone, to tell a declared entry file that is merely
    absent from the checkout (the script's window may still create it) from a
    layout no script window lifts.
    """
    if not _provisioning_declared(manifest, self_managed=self_managed):
        return False
    # Asked of the INSTALLED tree, not the checkout: *installed* is what
    # `install_app`'s own copy produced from it (see `_installed_tree_preview`),
    # so an entry the copy does not carry into the app directory -- under a
    # dropped build-input dir, under `data/` (which an update replaces with the
    # preserved previous data), a link escaping the root, a link whose text
    # stops resolving once the tree stands elsewhere -- is missing HERE exactly
    # as it is missing where the spawn looks, and the predicate answers as the
    # spawn would: no file-style entry, so only a stdio server, which
    # `bridges_mcp.py` provisions for on its own, can be the reason to waive.
    if runtime_provisions_requirements(manifest, installed):
        return True
    if final:
        return False
    # The BUILD pass runs before `setup.onInstall`, whose documented window is
    # exactly where an app may generate its entry file. A declared PYTHON entry
    # that is merely ABSENT from the checkout -- nothing at the path, or a link
    # whose target is not there yet -- is therefore provisionable for now:
    # nothing is pip-installed either way, and the final pass -- on the
    # post-script checkout, with `final=True` -- refuses if the file never
    # appeared. A declared shell or node entry is not: the suffix is the
    # manifest's, no script window changes it, and the deps tree never reaches
    # that child (`spawn_launches_entry_point_as_python`). Judged on the SOURCE:
    # anything standing at the path there (a directory, a link escaping the
    # root, a file under a name the copy drops) is a refusal no script window
    # lifts, and stands here.
    entry_point = manifest.backend.entryPoint
    if (
        not entry_point
        or is_module_style_entry_point(entry_point, source)
        or not spawn_launches_entry_point_as_python(manifest, source)
    ):
        return False
    entry = source / entry_point
    return _absent(entry) or (os.path.islink(entry) and not entry.exists())


def _absent(path: Path) -> bool:
    """True when nothing stands at *path* (``ENOENT``) -- the one shape a script's
    window can fill. Any other failure to stat it (a symlink budget exceeded, a
    file where a directory should be) is a layout no script fixes, so it is not
    "absent" and the build pass refuses it as the spawn would."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


#: Machine code carried beside the desktop refusal on the install result. The
#: dashboard keys its plain-language copy on it: the condition is permanent for
#: this gateway (retrying cannot change what the bundled interpreter can install),
#: so the consent modal drops its retry instruction for exactly this code.
DESKTOP_BUILD_STEP_UNSUPPORTED = "desktop_build_step_unsupported"

_DESKTOP_BUILD_REFUSAL = (
    "Python apps that require a build step are not supported in Kiro Crew's desktop "
    "build: its bundled interpreter is inside the signed application bundle and cannot "
    "install packages"
)


def _desktop_build_refusal(
    build_dir: Path, manifest: AppManifest, *, self_managed: bool, final: bool
) -> str:
    """The desktop app's build-step refusal for this checkout, or ``""`` when it may install.

    The ONE owner of that verdict. :func:`_run_app_build` asks it before planning
    any Python build command (``final=False``), and :func:`install_from_registry`
    asks it again on the FINAL checkout (``final=True``) -- after the build and
    ``setup.onInstall`` have run with write access -- so the waiver the build
    granted is re-derived from what actually registers rather than trusted
    across a script that can add ``backend.hooks`` or drop the out-of-process
    consumer it was granted for. The two passes differ in one respect only:
    ABSENCE. The script's window is where an app may generate its entry file or
    the target of its ``requirements.txt`` link, so the build pass lets a
    declared entry that is merely missing, or a link that is merely dangling,
    through (nothing is pip-installed either way) and the final pass refuses
    them if they never appeared; every other refusal is the same in both.
    Both callers are coroutines and run it through ``asyncio.to_thread``: the
    layout probes below are filesystem stats and one small copy, and
    ``KIROCREW_HOME`` can sit on a network mount that stalls, which on the event
    loop would freeze the gateway and its liveness heartbeat with it.

    Judges the same layout the build detects, in the same order: nothing on a
    gateway that is not the bundled interpreter; nothing when ``package.json``
    selects the JavaScript build (the Python files are not examined then); the
    refusal for ``pyproject.toml`` / ``setup.py``, which install INTO this
    interpreter; then, for a ``requirements.txt`` that is present, the refusal
    for one the runtime does not provision (:func:`_requirements_owned_by_the_runtime`,
    which *self_managed* -- the registry entry's resource ownership -- feeds) or
    will not READ (:func:`~kiro_crew.apps.manifest.requirements_in_tree`, the
    provisioner's own acceptance rule: a link escaping the app root, a dangling
    one, a non-file, an oversized one); ``""`` for one it does, and for a
    checkout with no Python build files at all.

    Those two checks are asked of the tree the install PRODUCES, not of this
    checkout: :func:`_installed_tree_preview` runs ``install_app``'s own copy
    (:func:`~kiro_crew.apps.manager.copy_app_tree_as_installed`) into a temporary
    directory and the predicates read that. The provisioner and the spawn read
    the APP DIRECTORY, and the copy drops build-input names at any depth, omits a
    link that escapes the root, keeps an in-tree link as a link with its text as
    written, rewrites an absolute in-tree link, and -- on an update -- meets
    ``data/`` as the preserved previous directory; a layout that resolves in the
    checkout and not after all of that (a link into ``node_modules``, an entry
    under ``data/``, a link whose text climbs above the root and re-enters by
    naming the checkout) is missing in the preview exactly as it would be missing
    there, and the runtime's own predicates refuse it with no rule of this
    function's predicting what the copy does. A preview the copy itself cannot
    produce is an ordinary install error, retryable and without this code: the
    install would fail on the same copy, and the app's author has nothing to fix.
    A preview the copy produces and the INSTALL would refuse -- a root ``data``
    that is not a directory, which ``install_app`` turns away before writing its
    record -- raises that refusal (:class:`~kiro_crew.apps.manager.InstalledTreeRefused`)
    through this gate unchanged, so the transaction refuses before the copy into
    the app directory is ever made, and without this code: the layout is the
    author's to fix on every host.

    Presence is ``lexists``, the provisioner's own notion: a dangling
    ``requirements.txt`` link is "present but not a readable regular file" to
    ``provision_app_deps``, which records a provisioning FAILURE and lets the
    backend spawn without its deps (the failure written at the top of its log,
    the import error following) -- so with a consumer declared it is refused
    here rather than waived into an install whose backend cannot run. With NO
    consumer nothing of ours would
    ever read it, and the build's own detection (``is_file``, on every host)
    sees no file: it passes, as it does on a source install.
    """
    if not platform_compat.is_bundled_interpreter():
        return ""
    if (build_dir / "package.json").is_file():
        return ""
    if (build_dir / "pyproject.toml").is_file() or (build_dir / "setup.py").is_file():
        return _DESKTOP_BUILD_REFUSAL
    requirements = build_dir / "requirements.txt"
    if not os.path.lexists(requirements):
        return ""
    if not _provisioning_declared(manifest, self_managed=self_managed):
        # Decided by the manifest and the registry entry alone -- nothing here
        # reads the tree, so the preview copy is not produced for it. A readable
        # requirements.txt with nothing of ours to provision it is the
        # silent-broken install; a dangling link with no consumer is what the
        # build's detection already calls "no file".
        return _DESKTOP_BUILD_REFUSAL if requirements.is_file() else ""
    with _installed_tree_preview(build_dir, manifest.name) as installed:
        if not _requirements_owned_by_the_runtime(
            manifest, installed, source=build_dir, self_managed=self_managed, final=final
        ):
            # The tree-reading half of the same union: no consumer the copy carries.
            return _DESKTOP_BUILD_REFUSAL if requirements.is_file() else ""
        if requirements_in_tree(installed, installed / "requirements.txt") is None:
            if not final and os.path.islink(requirements) and not requirements.exists():
                # Dangling NOW, in the checkout, before `setup.onInstall` has had
                # its window to create the target; the final pass judges the
                # post-script checkout and refuses if it is still dangling there.
                return ""
            return _DESKTOP_BUILD_REFUSAL
    return ""


@contextmanager
def _installed_tree_preview(source: Path, name: str) -> Iterator[Path]:
    """The tree an install of *source* would leave for the runtime, produced by
    the install's own copy into a temporary directory and removed on exit.

    :func:`~kiro_crew.apps.manager.copy_app_tree_as_installed` is the same call
    ``install_app`` makes plus the gateway's own post-copy part -- told, as the
    install will find out, whether a preserved ``data/`` awaits
    (:func:`~kiro_crew.apps.manager.preserved_data_awaits`) -- so the verdict
    derived from this tree cannot drift from the copy: the copy IS the model.

    WHERE the copy lands is part of the model too. The runtime resolves the
    installed tree's links from ``app_dir(name)`` -- ``<data home>/apps/<name>``
    -- and a relative link text that climbs out of the tree and re-enters it by
    NAME resolves by the names actually around it: ``requirements.txt ->
    ../app/requirements/prod.txt`` is in-tree in a checkout whose leaf directory
    is ``app`` (a registry ``subdirectory`` of that name) and dangles, or lands in
    another app's directory, once installed under ``apps/<name>``. So the preview
    is written under the destination's own leaf name, *name* -- the manifest's
    validated app name, the single directory component ``app_dir`` joins -- and a
    text that re-enters by that name resolves here exactly as it will there. The
    leaf is what decides: a text that climbs further re-enters by the names ABOVE
    the leaf, and the copy judges those texts where it runs, in the checkout --
    one that names the checkout's own ancestors resolves outside the root from
    the app directory and from here alike, and one that names anything else
    escapes the checkout and is omitted by the copy before either place sees it.

    The holder is a ``<name>.partial-<8 hex>`` sibling of the checkout under
    app-sources: the filesystem the install writes to (the one the copy's
    case-folding probe must answer for), and the one name the retention sweep
    already retires, so a copy an unclean gateway exit leaves behind mid-install
    goes the way of any other abandoned partial tree after
    :data:`_STALE_CHECKOUT_RETENTION_DAYS` days instead of accumulating.

    A copy that fails (disk full, a permission) raises an ordinary error carrying
    the cause -- the transaction reports it as any other install failure, without
    the permanent ``desktop_build_step_unsupported`` code, because a retry may
    well succeed and the app's author has nothing to fix. A copy that SUCCEEDS
    into a tree the install refuses -- a root ``data`` that is not a directory,
    which ``install_app`` turns away before writing its record -- raises the
    install's own :class:`~kiro_crew.apps.manager.InstalledTreeRefused` through
    here unchanged: the author's to fix, permanent, and reported by the
    transaction as the refusal it is (not this gate's desktop code, which would
    tell the reader a browser install could succeed). Blocking filesystem
    work, for a worker thread: app trees are small (the copy drops ``.git``,
    ``node_modules``, ``.venv`` and the other build-input names), and the caller
    already runs off-loop.
    """
    holder = _app_sources_dir() / f"{name}.partial-{uuid.uuid4().hex[:8]}"
    try:
        holder.mkdir(parents=True)
        preview = holder / name
        copy_app_tree_as_installed(source, preview, data_preserved=preserved_data_awaits(name))
    except OSError as exc:
        shutil.rmtree(holder, ignore_errors=True)
        raise RuntimeError(
            f"could not preview the installed tree of {source.name} for the desktop gate: {exc}"
        ) from exc
    except BaseException:
        shutil.rmtree(holder, ignore_errors=True)
        raise
    try:
        yield preview
    finally:
        shutil.rmtree(holder, ignore_errors=True)


def _desktop_gate_probe(
    build_dir: Path, manifest: AppManifest, self_managed: bool
) -> tuple[str, bool]:
    """The build-time desktop verdict plus whether a readable ``requirements.txt``
    is present -- one worker-thread call, so neither stat runs on the event loop.

    The second value only selects the install-log line the build writes when the
    verdict is ``""``: a present requirements.txt is the runtime's to provision,
    an absent one (or an entry nothing reads) is "no build step".
    """
    refusal = _desktop_build_refusal(build_dir, manifest, self_managed=self_managed, final=False)
    return refusal, (build_dir / "requirements.txt").is_file()
