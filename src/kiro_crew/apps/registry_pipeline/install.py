"""The install transaction: gates, clone, identity, build, ``onInstall``, register.

``install_from_registry`` runs every consent and admission gate before repository
bytes run, clones and builds through ``_clone_build_app`` with the identity and
admission gates between clone and build, checks again after the build and after
``onInstall``, records provenance from the final state, and restores or reports
every moved-aside checkout from its single ``finally``.

Its steps live in sibling owners: ``install_clone`` (clone, identity and admission,
build), ``install_rollback`` (putting a refused checkout back) and
``install_desktop_gate`` (the bundled interpreter's build-step verdict).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps import install_receipt
from kiro_crew.apps.admission import app_admission_denied, verified_signer
from kiro_crew.apps.execution import (
    app_execution_denied,
    repository_bound_grant_denied,
    trusted_app_repository,
)
from kiro_crew.apps.manager import (
    InstalledTreeRefused,
    get_app,
    install_app,
    registry_source_repository,
    set_app_provenance,
    update_app,
)
from kiro_crew.apps.manifest import (
    RESERVED_APP_NAME_CODE,
    AppManifest,
    app_name_error,
    is_reserved_app_name,
)
from kiro_crew.apps.registry_pipeline import _FACADE
from kiro_crew.apps.registry_pipeline.catalog import (
    SOURCE_REGISTRY_PREFIX,
    _is_catalog_row,
    _resolve_install_entry,
    resolve_installed_trust_repository,
)
from kiro_crew.apps.registry_pipeline.checkout import (
    _COMMIT_SHA_RE,
    _communicate_with_timeout,
    _kill_process_group,
    _resolved_clone_commit,
)
from kiro_crew.apps.registry_pipeline.git_targets import (
    _entry_git_url,
    _git_target_is_unsupported,
    _strip_git_target_userinfo,
)
from kiro_crew.apps.registry_pipeline.indexes import _owner_tier_confirmed
from kiro_crew.apps.registry_pipeline.install_clone import (
    _clone_build_app,
    _InstallVerb,
    _refusal_line,
    _refuse_identity_mismatch,
    _report_retained_stale_checkouts,
)
from kiro_crew.apps.registry_pipeline.install_desktop_gate import (
    DESKTOP_BUILD_STEP_UNSUPPORTED,
    _desktop_build_refusal,
)
from kiro_crew.apps.registry_pipeline.install_rollback import (
    _desktop_layout_present,
    _roll_back_post_script_refusal,
    _unpoison_rejected_checkout,
)
from kiro_crew.apps.registry_pipeline.manifests import _contained_join, _fetch_app_manifest
from kiro_crew.apps.registry_pipeline.recovery import (
    _restorable_or_none,
    _restore_moved_aside,
    _sweep_stale_checkouts,
    app_source_dir,
)
from kiro_crew.apps.registry_pipeline.sources import (
    _owner_designated_repo_target,
    _sel_credential_grant,
)
from kiro_crew.apps.registry_pipeline.subprocess_env import _detect_probe_env, minimal_env
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    sandboxed_spawn_argv,
    sandboxed_spawn_argv_async,
    wrap_argv,
    wrap_argv_async,
)
from kiro_crew.sel import sel

logger = logging.getLogger(_FACADE)


class StreamingLogLines(list):
    """Drop-in replacement for ``list[str]`` that also pushes to an asyncio.Queue.

    Used by the streaming install endpoint to forward log lines in real-time
    without changing the signature of ``install_from_registry`` or any of its
    callees.  All existing ``log_lines.append()`` / ``.extend()`` calls work
    unchanged — the queue receives each line as it's added.
    """

    def __init__(self, queue: asyncio.Queue[str | None]) -> None:
        super().__init__()
        self._queue = queue

    def append(self, line: str) -> None:  # type: ignore[override]
        super().append(line)
        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            pass  # drop if consumer is too slow

    def extend(self, lines) -> None:  # type: ignore[override]
        for line in lines:
            self.append(line)


_SCRIPT_TIMEOUT = 300


def _remote_controlled_url(entry: dict[str, Any]) -> bool:
    """Whether *entry*'s clone URL came from content we do not control.

    Drives the CREDENTIAL posture: a True answer means the clone runs
    credential-free and strict-sandboxed (:func:`anonymous_git_env`), because the
    URL is not one the owner typed.

    Both markers qualify. ``_registry`` is an external index's row. ``_catalog`` is
    the official catalog's, whose URL arrives in a document fetched over the network
    whose signature this client does not yet verify -- so it is remote-controlled in
    exactly the same way. Reading "no ``_registry``" as "owner-designated" held only
    while the sole marker-less rows came from the wheel's bundled seed, which the
    owner installed deliberately.
    """
    return bool(entry.get("_registry")) or bool(entry.get("_catalog"))


def _official_entry(entry: dict[str, Any]) -> bool:
    """Whether *entry* is an app WE list, which decides install-receipt eligibility.

    Deliberately NOT the negation of :func:`_remote_controlled_url`. A catalog row
    is remote-controlled (credential-free) AND official (receipt fires); collapsing
    both onto one boolean is what made the catalog row take owner credentials.
    """
    return not entry.get("_registry")


async def _retained_startup_refusal(name: str, log_lines: list[str]) -> dict[str, Any] | None:
    """Return a retryable refusal while old-version startup code remains live."""
    # Deferred to avoid registry -> hooks_integration -> manager import cycles at
    # module load. The dispatcher exists only in the gateway process; without it
    # there is no in-process retained startup task to own.
    from kiro_crew.apps.hooks_integration import stop_retained_startup_hooks

    if await stop_retained_startup_hooks(name, bounded=True):
        return None
    message = (
        f"cannot reinstall {name!r} while its timed-out startup hook is still "
        "running; retry after it exits"
    )
    log_lines.append(message)
    return {
        "ok": False,
        "name": name,
        "error": message,
        "code": "startup_hook_still_running",
        "retryable": True,
    }


def _trust_grant_would_stay_local(name: str) -> bool:
    """Whether re-granting trust for *name* would record a local source again.

    The trust grant endpoint binds to the INSTALLED app's provenance. When that
    provenance is positively local, the dialog can only ever record a local
    grant, which never covers a registry clone. Any failure to tell answers
    False, which keeps the existing consent-dialog behaviour.
    """
    try:
        installed = get_app(name)
        if installed is None:
            return False
        resolved, repository = resolve_installed_trust_repository(
            installed, allow_registry_lookup=False
        )
    except Exception:  # noqa: BLE001 - unknown provenance keeps the old path
        logger.warning("installed app trust provenance could not be resolved")
        return False
    return resolved and not repository


async def install_from_registry(
    name: str,
    log_lines: list[str] | None = None,
) -> dict[str, Any]:
    """Clone an app from its git repo and install it.

    Source code is cloned to ``~/.kiro/crew/app-sources/{name}/`` (persistent,
    survives reboots, used by app update scripts).

    For self-managed apps (``managed: "self"`` in registry), only the clone +
    install script is run — Kiro Crew does NOT copy files to ``~/.kiro/crew/apps/``
    or register resources via bridges.  The app registers itself at runtime.

    For kirocrew-managed apps, files are copied to ``~/.kiro/crew/apps/{name}/``
    and resources are registered via bridges.py as usual.

    Args:
        name: Registry app name.
        log_lines: Optional list to collect log output.  Pass a
            :class:`StreamingLogLines` instance to stream logs in real-time
            via the SSE install endpoint.  If *None*, a plain ``list`` is used
            (original behaviour).

    Steps:
    1. Validate the app exists in the trusted registry JSON
    2. Clone the repo to ~/.kiro/crew/app-sources/{name}/ (timeout: 60s)
    3. Build it (npm/pip, auto-detected) then run the install script from
       app.json if any (timeout: 300s)
    4. For kirocrew-managed: call install_app() or update_app()
    5. Store ``registry:<name>`` plus structured provenance (source URL,
       originating registry, resolved commit, verified signer) for future updates

    Returns a dict with ok, name, message/error, and log output.
    """
    # An already-installed app that carries provenance may only be re-installed
    # (updated) from the source it came from; fresh installs and legacy records
    # keep the historical bare-name lookup. Blocking reads → off the loop.
    # Reject an inadmissible name BEFORE the registry lookup and any
    # clone/build/onInstall work. The manifest/self-registration gates repeat
    # this check, but for a self-managed app they only fire at runtime
    # self-registration — without this early refusal the install would clone,
    # build, and run onInstall, then report success while leaving an
    # unregisterable checkout behind. Name admissibility is independent of
    # registry contents, so this precedes _resolve_install_entry.
    name_error = app_name_error(name)
    if name_error:
        outcome_early: dict[str, Any] = {
            "ok": False,
            "name": name,
            "error": name_error,
            "log": "",
        }
        # `code` only for the reserved-name refusals — same contract as the
        # register_external_app path (is_reserved_app_name gates the code there).
        if is_reserved_app_name(name):
            outcome_early["code"] = RESERVED_APP_NAME_CODE
        return outcome_early

    entry, pin_error = await asyncio.to_thread(_resolve_install_entry, name)
    if pin_error:
        try:
            sel().log_api_access(
                caller="app_install_from_registry",
                operation="provenance_mismatch",
                outcome="rejected",
                resources=f"name={name!r}",
                error=pin_error,
            )
        except Exception as exc:  # an audit failure must never mask the refusal
            logger.debug("SEL audit failed for %s provenance mismatch: %s", name, exc)
        return {"ok": False, "name": name, "error": pin_error}
    if not entry:
        return {"ok": False, "error": f"app {name!r} not found in registry"}

    git_url = _entry_git_url(entry)
    if not git_url:
        return {"ok": False, "error": f"app {name!r} has no git URL configured"}
    if _git_target_is_unsupported(git_url):
        return {
            "ok": False,
            "name": name,
            "error": (
                "app registry clone URL contains an unsupported query or fragment or "
                "an ambiguous Git transport identity"
            ),
            "code": "invalid_registry_source",
        }
    persisted_git_url = _strip_git_target_userinfo(git_url)

    # A per-app execution grant is consent to the repository the operator saw,
    # not to whichever repository later claims the same app name. New grants
    # record that coordinate; a legacy name-only grant needs one-time re-consent
    # before repository-backed bytes can be fetched or executed. This gate runs
    # before manifest fetch, credential selection, clone, build, or setup code.
    granted_repository = trusted_app_repository(name)
    trust_denied = repository_bound_grant_denied(name, repository=git_url)
    if trust_denied:
        # The exact coordinates are comparison inputs, not log/API data. Clone
        # URLs can contain userinfo credentials; every copy of this reason is
        # audited or returned to the dashboard, so keep it credential-free.
        reason = trust_denied
        # A bound mismatch must first be revoked. A legacy unbound grant instead
        # needs the normal consent dialog, whose stable trigger is the execution
        # denial code. Keep both existing wire behaviours explicit.
        code = "app_trust_repository_mismatch" if granted_repository else "app_execution_denied"
        # An unbound grant over an app still installed from a local folder is
        # the one case the consent dialog cannot fix: the grant endpoint binds to
        # the INSTALLED source, so granting again re-records a local grant and
        # this refusal repeats forever. Answer it with its own code, so the
        # dialog does not open, and name the route that records a repository.
        if not granted_repository and _trust_grant_would_stay_local(name):
            code = "app_trust_local_only"
            reason = (
                "this app is trusted for its local source only, and granting trust "
                "again keeps it local-only, so it cannot be updated from a "
                "repository. To switch to the repository version, uninstall the "
                "app (its data is kept) and install it from the App Store, which "
                "asks you to trust that repository"
            )
        audit_operation = (
            "trust_repository_mismatch"
            if granted_repository
            else "trust_repository_binding_required"
        )
        try:
            sel().log_api_access(
                caller="app_install_from_registry",
                operation=audit_operation,
                outcome="rejected",
                resources=f"name={name!r}",
                error=reason,
            )
        except Exception as exc:  # an audit failure must never mask the refusal
            logger.debug("SEL audit failed for %s trust repository mismatch: %s", name, exc)
        return {
            "ok": False,
            "name": name,
            "error": reason,
            "code": code,
        }

    raw_repo = entry.get("repo", "")
    repo = _strip_git_target_userinfo(raw_repo) if isinstance(raw_repo, str) else ""
    branch = entry.get("branch", "main")
    subdirectory = entry.get("subdirectory", "")
    # Pinning is a CATALOG mechanism, so the pin is read only for a catalog row.
    #
    # `commit` is on the row-projection allowlist (`_REGISTRY_ROW_KEYS`), and that
    # projection also builds rows from an external registry's index -- untrusted,
    # index-controlled JSON. Reading it unconditionally would hand that index a
    # capability its `branch` field cannot express: a fetch BY SHA reaches objects
    # no branch contains (a commit force-pushed away, or one that only ever existed
    # on a side ref), while a branch clone can only ever reach what a ref points at.
    # The owner-configured `branch` would then stop bounding which code gets built
    # and runs `onInstall`.
    #
    # `_is_catalog_row` is the right test rather than a bare `_catalog` check,
    # because `_catalog` is index-settable while `_registry` is attached server-side
    # per configured registry and cannot be forged.
    if _is_catalog_row(entry):
        commit = str(entry.get("commit", "") or "")
    else:
        commit = ""
        if entry.get("commit"):
            # Not a refusal: `branch` is exactly the coordinate such a row is
            # entitled to, so honouring it is correct. But an index author who
            # believes they pinned deserves to see that they did not.
            logger.warning(
                "ignoring commit pin on non-catalog row %r: pinning is a catalog "
                "mechanism; installing from branch %r instead",
                name,
                branch,
            )

    # The pin is honoured or the install is refused -- there is no third option.
    #
    # `branch` above defaults to "main", and that default is what makes a quiet
    # failure possible: a catalog row carries a commit and no branch, so a path
    # that ignored `commit` would clone the tip of "main", SUCCEED, and record the
    # tip's commit as this app's provenance. The store would then look like it
    # installs pinned bytes while installing whatever the app's default branch
    # holds today. Refusing a malformed pin is the only safe answer, because the
    # alternative is inventing coordinates nobody signed.
    if commit and not _COMMIT_SHA_RE.match(commit):
        return {
            "ok": False,
            "name": name,
            "error": (
                f"app {name!r} carries a malformed pinned commit; refusing to "
                f"install rather than fall back to a branch"
            ),
        }

    # Confused-deputy defense on the INSTALL path (companion to the automatic
    # browse/refresh defense in ``anonymous_git_env``). An entry that came from
    # an owner-configured *external* registry index carries ``_registry`` (set
    # when the index is fetched/cached); its ``repo`` URL is index-controlled
    # content, not a repo the owner typed — the owner clicked Install on an
    # index-authored name/description. Because ``is_clone_host_trusted`` is
    # host-granular, such an entry can point at a private *sibling* repo on the
    # owner's own trusted forge; cloning it with the gateway's ambient git/ssh
    # identity would read that private repo as a confused deputy. So an
    # index-originated install clones credential-free + strict-sandboxed too.
    # Bundled (curated, shipped with Kiro Crew) entries have no ``_registry`` marker
    # and remain owner-designated → full credentials.
    #
    # Same-repo credential carve-out: when the entry's effective clone URL is
    # byte-identical to the owner-configured registry repo URL, the
    # confused-deputy argument does not apply — the owner explicitly designated
    # exactly that URL by adding the registry. The carve-out flips BOTH env
    # AND sandbox mode together (the strict sandbox hiding ~/.ssh is the
    # load-bearing enforcement on credential-helper setups, not the env alone).
    # Sibling repos on the same host remain anonymous+strict.
    # Credential posture and OFFICIALNESS are two different questions, and a
    # catalog row is the case that separates them: its URL arrives in a document
    # fetched over the network whose signature this client does not yet verify, so
    # it is remote-controlled content exactly like an external index's URL -- but
    # it IS an app we list, so its install receipt must still fire.
    #
    # Treating "no `_registry`" as "the owner designated this repo" was true while
    # the only marker-less rows came from the wheel's bundled seed, which the owner
    # installed deliberately. A catalog row is not that: nobody typed its URL, and
    # a repointed row on a trusted forge would otherwise be cloned with the
    # gateway's ambient git/ssh identity -- the confused-deputy read this posture
    # exists to prevent.
    index_originated = _remote_controlled_url(
        entry
    )  # OFFICIALNESS is decided from `_registry` ALONE, and BEFORE the
    # owner-designated carve-out below: that carve-out flips index_originated as a
    # CREDENTIAL decision (owner explicitly designated the repo), but an
    # external-index entry never becomes an official-catalog entry — install
    # receipts must not fire for it. A catalog row has no `_registry`, so it stays
    # official even though it takes the credential-free posture above.
    official_entry = _official_entry(entry)
    # The originating external registry id, recorded as provenance. Empty means
    # the bundled catalog shipped with Kiro Crew, which is itself a distinct source.
    # Captured BEFORE the owner-designated carve-out (same reasoning as above):
    # the entry still came from that external registry, and provenance must say so.
    source_registry = _strip_git_target_userinfo(str(entry.get("_registry", "") or ""))
    owner_designated_target = (
        await asyncio.to_thread(_owner_designated_repo_target, entry) if index_originated else ""
    )
    if index_originated and owner_designated_target:
        index_originated = False
        _sel_credential_grant("install_from_registry", _entry_git_url(entry) or "")
    elif index_originated and await _owner_tier_confirmed(entry):
        # An ``owner``-tier registry re-confirmed this exact clone URL in a fresh
        # fetch of its index. Install-only and never from the cache — see
        # `_owner_tier_confirmed`.
        index_originated = False
        _sel_credential_grant("install_from_registry_owner_tier", _entry_git_url(entry) or "")
    # Capture event kind before clone/build/install scripts can register or
    # otherwise change app state. The receipt describes this call's starting
    # state, not an intermediate side effect.
    was_installed = get_app(name) is not None

    # Fetch the app's manifest for platform info and install script. This is a
    # read-only metadata fetch (git archive of app.json), safe to do before the
    # admission gate so a correctly-signed manifest can be passed to it.
    # Same-repo carve-out: if the entry is from an external index but its clone
    # URL matches the owner-configured registry repo (index_originated was
    # flipped to False above), use owner credentials for the manifest fetch too.
    manifest_owner_designated = bool(entry.get("_registry")) and not index_originated
    manifest = await _fetch_app_manifest(
        repo,
        branch,
        subdirectory,
        app_name=name,
        git_url=owner_designated_target or git_url,
        owner_designated=manifest_owner_designated,
        commit=commit,
    )

    # Admission: gate AFTER the manifest fetch (so a signed manifest is verified)
    # but BEFORE the repo is cloned and setup.onInstall runs, so a banned /
    # non-allowlisted / unsigned app is never cloned nor its install script run.
    admission_manifest = AppManifest.from_dict(manifest) if manifest else None
    denied = app_admission_denied(name, manifest=admission_manifest, action="install_from_registry")
    if denied:
        sel().log_api_access(
            caller="app_install_from_registry",
            operation="admission",
            outcome="rejected",
            resources=f"name={name!r}",
            error=denied,
        )
        return {"ok": False, "name": name, "error": f"blocked by admission policy: {denied}"}

    # NOTE: the provenance signer is computed LATER, from the identity-checked
    # CLONED manifest — not from this pre-clone prefetch. An update can pull a
    # commit whose manifest is not signed (or is signed by someone else);
    # provenance must record the artifact actually installed, not the preview.

    # Platform compatibility check — if the app requires a specific OS and
    # Kiro Crew is running on an incompatible platform, return client install
    # instructions instead of attempting a server-side install.
    manifest_platform = (manifest or {}).get("platform", {})
    required_os = manifest_platform.get("os", ["macos", "linux"])
    install_mode = manifest_platform.get("installMode", "server")

    from kiro_crew.apps.manifest import PlatformConfig

    if install_mode == "client" and not PlatformConfig(os=required_os).supports_platform(
        sys.platform
    ):
        client_install = manifest_platform.get("clientInstall", {})
        os_label = ", ".join(o.capitalize() if o != "macos" else "macOS" for o in required_os)
        return {
            "ok": False,
            "needsClientInstall": True,
            "name": name,
            "clientInstall": client_install,
            "platform": {"required": required_os, "current": PlatformConfig.current_os()},
            "error": f"This app requires {os_label} and must be installed on your local machine.",
        }

    is_self_managed = entry.get("resources") == "app"
    if log_lines is None:
        log_lines = []

    startup_refusal = await _retained_startup_refusal(name, log_lines)
    if startup_refusal is not None:
        return startup_refusal

    # Validate minKiroCrewVersion if declared
    min_version = (manifest or {}).get("minKiroCrewVersion", "")
    if min_version:
        from kiro_crew.apps.version import check_min_version

        ver_err = check_min_version(min_version)
        if ver_err:
            return {
                "ok": False,
                "name": name,
                "error": ver_err,
            }

    # detectInstalled, clone/build, dependency setup, and onInstall are all
    # executable third-party surfaces and share the same explicit admission.
    execution_denied = app_execution_denied(
        name,
        action="registry_install",
        caller="registry",
        repository=git_url,
    )
    if execution_denied:
        return {
            "ok": False,
            "name": name,
            "error": f"blocked by execution policy: {execution_denied}",
            # Same wire contract as the openCommand denial in routes_enable.py: the
            # frontend keys its affordance off `code`, never off this prose.
            # Without it the App Store cannot tell "needs a trust grant" from
            # any other install failure and the consent modal never opens.
            "code": "app_execution_denied",
            "log": "\n".join(log_lines),
        }

    # Guard: check if already installed externally (e.g. user ran setup.sh manually)
    detect_cmd = entry.get("detectInstalled", "")
    if detect_cmd:
        try:

            base_cmd = ["/bin/sh", "-c", detect_cmd]
            # Through the single sandboxed-spawn chokepoint, not a hand-rolled
            # wrap + cgroup pair: it applies the strict launcher, the credential
            # scrub and the cgroup DoS ceiling, AND it forwards the systemd bus
            # locators that the ceiling's own `systemd-run --user` wrapper needs
            # to reach the user bus, dropping them again with an `env -u` shim
            # inside the scope so the probe itself never sees a live bus address.
            # A caller-built env that omits those locators makes `systemd-run`
            # exit 1 before the command runs, which with DEVNULL stderr reads as
            # "not installed" for every app on a cgroup-delegated host.
            #
            # `_detect_probe_env` is the credential-free base it scrubs on top of:
            # no agent socket, no git credential helper, no prompt, no toolchain
            # variable. The command string comes from a registry manifest, which
            # is untrusted content, and `strict` mode's own scrub only runs when
            # the launcher does -- not on Windows, and not on a host with no
            # sandbox backend plus agent.sandbox_allow_unsandboxed_exec -- so the
            # env handed over here is the only control left on those hosts.
            sandboxed_cmd, probe_env, _cleanup = await sandboxed_spawn_argv_async(
                base_cmd,
                mode="strict",
                env=_detect_probe_env(),
                _prepare=sandboxed_spawn_argv,
            )
            proc = await create_subprocess_limited(
                *sandboxed_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=probe_env,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            await _communicate_with_timeout(proc, timeout=5)
            if proc.returncode == 0:
                return {
                    "ok": False,
                    "name": name,
                    "error": f"{name} is already installed on this machine. "
                    f"Launch it to register with Kiro Crew automatically.",
                }
        except (asyncio.TimeoutError, OSError):
            pass

    build_result: dict[str, Any] = {}
    # Cleared only after the transaction durably succeeds; the `finally` below reads it.
    durable_success = False
    # Every `return` below assigns here first (named `outcome`, not `result` —
    # the kirocrew-managed path below already uses `result` for the
    # install_app/update_app return value). `"log"` is stamped from
    # `log_lines` at assignment time, but the `finally` backstop can append to
    # `log_lines` (a restore confirmation, or the restore-failed WARNING) AFTER
    # that value is already computed — a `return`'s expression is evaluated
    # before `finally` runs, and `str.join` produces an immutable copy, so a
    # later append never reaches an already-built "log" string. Because
    # dicts ARE mutable, holding the same object here and re-stamping
    # `outcome["log"]` at the end of `finally` (below) closes that gap instead
    # of the WARNING silently never reaching the log the user sees.
    outcome: dict[str, Any] | None = None
    try:
        # Best-effort sweep of aged .stale-* / .partial-* dirs before the
        # install — prevents unbounded accumulation without blocking.
        await _sweep_stale_checkouts()

        # The verb every refusal line of this run carries. The page titles its
        # log panel "Install log" or "Update log" by whether this app has an
        # installed record, and the copy step below chooses `update_app` or
        # `install_app` by that same record, so the streamed lines read it too
        # -- one fact, three readers. Blocking read -> off the loop. The route
        # handler holds app_lifecycle_lock(name) across the whole transaction,
        # so the answer cannot change between here and the copy step.
        verb: _InstallVerb = "update" if await asyncio.to_thread(get_app, name) else "install"

        # Step 1: Clone the app repo and build it (npm/pip auto-detected).
        # `git clone` handles fetch + branch checkout; a subsequent install
        # run fast-forwards the existing clone instead of re-cloning. The
        # cleanup state for later gates (_checkout_preexisted /
        # _pre_pull_commit) rides on build_result — it describes the ACTIVE
        # checkout, accounting for a move-aside re-clone.
        build_result = await _clone_build_app(
            owner_designated_target or git_url,
            name,
            log_lines,
            branch=branch,
            index_originated=index_originated,
            # Passed so the BUILD runs where the package is. The containment check
            # below is still authoritative for choosing app.json's directory.
            subdirectory=subdirectory,
            entry_repo=repo,
            commit=commit,
            # The registry entry's resource ownership feeds the desktop gate: the
            # runtime provisions only the apps it installs and spawns itself.
            self_managed=is_self_managed,
            verb=verb,
        )
        if not build_result["ok"]:
            # A pre-build refusal (identity/admission gate inside
            # _clone_build_app), a failed clone, or a failed build may have left
            # a non-restorable origin-mismatch checkout moved aside. Retained-stale
            # reporting and restorable-stale restoration are both owned by the
            # single `finally` below: it runs on every exit, knows durable_success,
            # and re-stamps outcome["log"], so no per-exit report or log join is
            # needed here.
            outcome = {**build_result}
            return outcome

        app_source = build_result["pkg_dir"]
        clone_root = app_source
        if subdirectory:
            # ``subdirectory`` is untrusted index-controlled content. Join it
            # under the cloned source root with symlink-resolving containment so
            # an absolute/``..``/symlink value cannot point app.json (and thus
            # setup.onInstall) at an attacker-selected path outside the clone.
            contained = _contained_join(app_source, subdirectory)
            if contained is None:
                # subdirectory FAILED containment here — by definition it is
                # an escaping value (absolute, "..", or a symlink pointing
                # outside app_source). It must never be joined onto pkg_dir
                # for a filesystem write; that is exactly what
                # _contained_join guards against. The manifest-restore step
                # of _unpoison_rejected_checkout writes to
                # ``pkg_dir / manifest_relpath`` when checkout_preexisted is
                # True, so passing the raw subdirectory as manifest_relpath
                # there would let a symlinked subdirectory redirect that
                # write outside the sandboxed checkout.
                #
                # A successful clone+build already ran (build_result["ok"] is
                # True), so any moved-aside checkout from a branch/origin
                # re-convergence must not be silently stranded by this
                # refusal — but only the delete-this-run's-checkout /
                # restore-previous-checkout branch of the helper (taken when
                # checkout_preexisted is False) is safe here: it never
                # touches manifest_relpath. When the checkout PRE-existed,
                # skip cleanup entirely and return the refusal as-is rather
                # than risk that write.
                if not build_result.get("_checkout_preexisted"):
                    # Restoring a moved-aside checkout here means giving the
                    # rejected clone's own pkg_dir back to the CALLER as the
                    # active checkout, even though the containment gate just
                    # refused it. That is only safe for a restorable
                    # (same-origin, branch-drift) stale, never for a
                    # non-restorable (origin-mismatch, different repository)
                    # one — restoring an origin-mismatched stale here is
                    # exactly the "hand the build the tree the gate refused"
                    # case _restorable_stale exists to prevent, so it must be
                    # filtered out the same way every other restoration site
                    # in this module filters it.
                    await _unpoison_rejected_checkout(
                        name,
                        app_source_dir(name),
                        log_lines,
                        checkout_preexisted=False,
                        pre_pull_commit="",
                        restore_from=_restorable_or_none(
                            build_result.get("_pending_stale_cleanup"),
                            build_result.get("_restorable_stale"),
                        ),
                    )
                # The _unpoison above restores only a restorable stale, so an
                # origin-mismatch one is stranded here — the `finally`-owned
                # reporter names it and re-stamps outcome["log"].
                outcome = {
                    "ok": False,
                    "name": name,
                    "error": f"unsafe subdirectory {subdirectory!r} escapes the app source root",
                }
                return outcome
            app_source = contained

        # NOTE: a missing app.json is handled by the identity gate below
        # (fail-closed: unreadable manifest == mismatch), so a build step that
        # DELETES the manifest still goes through the refusal path and its
        # checkout cleanup rather than returning early with a poisoned tree.

        # Read the cloned repo's app.json once: it decides both the app's
        # IDENTITY and its install script.
        # Trust model: curated registry entry → cloned repo → app.json
        # (maintained by the app author).  The install script has the same
        # trust level as any code you clone and build locally.
        manifest_data: dict[str, Any] | None = None
        try:
            manifest_raw = await asyncio.to_thread(
                (app_source / "app.json").read_text,
                "utf-8",
            )
            parsed = json.loads(manifest_raw)
            if isinstance(parsed, dict):
                manifest_data = parsed
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.debug("cloned app.json for %s is unreadable: %s", name, exc)

        # IDENTITY GATE, second pass: the primary gate already ran inside
        # _clone_build_app BEFORE the build (so a mismatched repo never executes
        # npm/pip lifecycle scripts). This re-check catches the remaining
        # window — a build step that REWRITES app.json to a different name —
        # and stays fail-closed: a missing or unparseable name is a mismatch,
        # not a pass. ``install_app``/``update_app`` derive the installed
        # identity from this manifest, so it must still match the entry here.
        if manifest_data is None or str(manifest_data.get("name", "") or "") != name:
            outcome = await _refuse_identity_mismatch(
                name,
                str((manifest_data or {}).get("name", "") or ""),
                _strip_git_target_userinfo(repo),
                clone_root,
                log_lines,
                created_this_run=not bool(build_result.get("_checkout_preexisted")),
                pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                manifest_snapshot=build_result.get("_pre_update_manifest"),
                restore_from=_restorable_or_none(
                    build_result.get("_pending_stale_cleanup"),
                    build_result.get("_restorable_stale"),
                ),
                verb=verb,
            )
            # Retained-stale reporting for this refusal is owned by the
            # `finally` below (it re-stamps outcome["log"] on every exit).
            return outcome

        # ADMISSION GATE, third pass — the post-build manifest is what
        # install_app/update_app will actually register, and a build step can
        # rewrite app.json; a manifest that does not satisfy the admission
        # policy (e.g. signature required but absent) must not install.
        denied = app_admission_denied(
            name,
            manifest=AppManifest.from_dict(manifest_data),
            action="install_from_registry",
        )
        if denied:
            log_lines.append(_refusal_line(verb, f"blocked by admission policy: {denied}"))
            try:
                sel().log_api_access(
                    caller="app_install_from_registry",
                    operation="admission_postbuild",
                    outcome="rejected",
                    resources=f"name={name!r}",
                    error=denied,
                )
            except Exception as exc:  # audit failure must never mask the refusal
                logger.debug("SEL audit failed for %s post-build admission: %s", name, exc)
            # Same retry-poisoning hazard as the cloned-admission gate: the
            # checkout sits at the rejected commit and the prefetch prefers it,
            # so clean up with the same delete-fresh/roll-back semantics.
            # _unpoison restores only the restorable subset; the `finally`-owned
            # reporter names any stranded non-restorable move-aside from
            # on-disk truth after this restore and re-stamps outcome["log"].
            await _unpoison_rejected_checkout(
                name,
                app_source_dir(name),
                log_lines,
                checkout_preexisted=bool(build_result.get("_checkout_preexisted")),
                pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                manifest_snapshot=build_result.get("_pre_update_manifest"),
                restore_from=_restorable_or_none(
                    build_result.get("_pending_stale_cleanup"),
                    build_result.get("_restorable_stale"),
                ),
            )
            outcome = {
                "ok": False,
                "name": name,
                "error": f"blocked by admission policy: {denied}",
            }
            return outcome

        # NOTE: the provenance commit AND signer are both resolved AFTER the
        # install-script block below — onInstall runs with write access to the
        # checkout and can advance it to another commit or swap the manifest;
        # provenance must record the state that actually registers.

        install_script = (manifest_data.get("setup") or {}).get("onInstall", "")

        # Which of the desktop gate's layout inputs exist BEFORE the install
        # script runs. The script has write access to the checkout, and a file it
        # creates is untracked, so the post-refusal `git reset --keep` leaves it in
        # a pre-existing checkout -- where a created `pyproject.toml` would make
        # every retry refuse at the BUILD gate, before the script that could be
        # fixed ever runs again. The refusal below removes what the script created
        # and nothing that was already there.
        layout_before = await asyncio.to_thread(_desktop_layout_present, app_source)

        # Step 2: Run install script
        if install_script:
            log_lines.append(f"Running install script: {install_script}")
            # Sandboxed via wrap_argv(); consider migrating to AcpClient._spawn() for full OS-level isolation.
            # SEL audit event emitted below for traceability.
            logger.info(
                "Executing sandboxed install script for app %s from repo %s",
                name,
                _strip_git_target_userinfo(repo),
            )

            def _audit_script(result: str, exit_code: object = None) -> None:
                resources = f"{name} repo={_strip_git_target_userinfo(repo)}"
                if exit_code is not None:
                    resources += f" exit={exit_code}"
                try:
                    sel().log_api_access(
                        caller="registry",
                        operation="app_install_script",
                        outcome=result,
                        resources=resources,
                    )
                except Exception as exc:
                    logger.debug("SEL audit failed for app %s install: %s", name, exc)

            _audit_script("started")
            # Wrap with safe defaults:
            #   set -e  — exit on first error
            #   set -u  — treat unset variables as errors (prevents rm -rf $EMPTY/)
            #   set -o pipefail — propagate pipe failures
            safe_script = f"set -euo pipefail\n{install_script}"

            base_cmd = ["/bin/bash", "-c", safe_script]
            sandboxed_cmd, _cleanup = await wrap_argv_async(
                base_cmd, mode="standard", _prepare=wrap_argv
            )
            sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
            proc = await create_subprocess_limited(
                *sandboxed_cmd,
                cwd=str(app_source),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=minimal_env(NONINTERACTIVE="1"),
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_SCRIPT_TIMEOUT)
            except asyncio.TimeoutError:
                # Kill the entire process group (shell + children), reap the
                # child, and escalate SIGTERM -> SIGKILL if it ignores the term.
                await _kill_process_group(proc)
                _audit_script("timed_out", proc.returncode)
                # Retained-stale reporting and restorable-stale restoration are
                # owned by the `finally` below (it re-stamps outcome["log"]).
                outcome = {
                    "ok": False,
                    "name": name,
                    "error": f"install script timed out after {_SCRIPT_TIMEOUT}s",
                }
                return outcome

            lines = stdout.decode(errors="replace").strip().split("\n")
            if len(lines) > 50:
                log_lines.append(f"... ({len(lines) - 50} lines truncated)")
                log_lines.extend(lines[-50:])
            else:
                log_lines.extend(lines)

            _audit_script("completed" if proc.returncode == 0 else "failed", proc.returncode)
            if proc.returncode != 0:
                # Retained-stale reporting and restorable-stale restoration are
                # owned by the `finally` below (it re-stamps outcome["log"]).
                outcome = {
                    "ok": False,
                    "name": name,
                    "error": f"install script failed (exit {proc.returncode})",
                }
                return outcome

            # Reap any SURVIVING descendants of the script's process group
            # before the final gates re-read app.json: a backgrounded child
            # (`nohup evil &`) outlives the shell's clean exit and could
            # rewrite the manifest AFTER the re-read below but before
            # install_app registers it — the exact TOCTOU the final pass
            # exists to close. The shell itself already exited, so anything
            # still in the group is a detached straggler with no legitimate
            # claim to keep running.
            #
            # POSIX: signal the KNOWN group id directly — the script was
            # spawned with start_new_session, so its pgid equals proc.pid by
            # construction, and the group outlives its (already-reaped)
            # leader. Resolving the group via getpgid(proc.pid) would raise
            # ProcessLookupError once the leader is reaped, silently skipping
            # the very stragglers this exists to kill. The pid>1 guard keeps
            # the killpg broadcast-safe (never signal group 0/1/self).
            # Windows: taskkill /T on the root pid via the platform shim.
            try:
                if platform_compat.IS_POSIX:
                    if type(proc.pid) is int and proc.pid > 1:
                        await asyncio.to_thread(os.killpg, proc.pid, platform_compat.SIGKILL)
                else:
                    await platform_compat.kill_process_tree_async(proc.pid, platform_compat.SIGKILL)
            except OSError:
                # Empty group (no stragglers) — the common case.
                pass

            # IDENTITY + ADMISSION, final pass — the install script just ran
            # with write access to the checkout and can rewrite app.json, and
            # install_app/update_app/register_external_app re-read that file
            # from disk. Whatever is on disk NOW is what gets registered, so it
            # must pass the same fail-closed gates as the post-build read.
            manifest_data = None
            try:
                parsed = json.loads(
                    await asyncio.to_thread((app_source / "app.json").read_text, "utf-8")
                )
                if isinstance(parsed, dict):
                    manifest_data = parsed
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                logger.debug("post-script app.json for %s is unreadable: %s", name, exc)
            if manifest_data is None or str(manifest_data.get("name", "") or "") != name:
                outcome = await _refuse_identity_mismatch(
                    name,
                    str((manifest_data or {}).get("name", "") or ""),
                    _strip_git_target_userinfo(repo),
                    clone_root,
                    log_lines,
                    created_this_run=not bool(build_result.get("_checkout_preexisted")),
                    pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                    manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                    manifest_snapshot=build_result.get("_pre_update_manifest"),
                    restore_from=_restorable_or_none(
                        build_result.get("_pending_stale_cleanup"),
                        build_result.get("_restorable_stale"),
                    ),
                    # The script ran: what it created by the layout names goes
                    # before the rollback, as on every gate after the script.
                    appeared_layout=(app_source, layout_before),
                    verb=verb,
                )
                # Retained-stale reporting for this post-script refusal is owned
                # by the `finally` below (it re-stamps outcome["log"]).
                return outcome
            denied = app_admission_denied(
                name,
                manifest=AppManifest.from_dict(manifest_data),
                action="install_from_registry",
            )
            if denied:
                log_lines.append(_refusal_line(verb, f"blocked by admission policy: {denied}"))
                try:
                    sel().log_api_access(
                        caller="app_install_from_registry",
                        operation="admission_postscript",
                        outcome="rejected",
                        resources=f"name={name!r}",
                        error=denied,
                    )
                except Exception as exc:  # audit failure must never mask the refusal
                    logger.debug("SEL audit failed for %s post-script admission: %s", name, exc)
                # onInstall ran with write access to the checkout, so this
                # denial leaves it poisoned exactly like the earlier gates --
                # the layout files it created go first, then the same
                # delete-fresh/roll-back cleanup, so a retry can pull a fixed
                # remote instead of re-rejecting at prefetch or at the build gate.
                # _unpoison restores only the restorable subset; the
                # `finally`-owned reporter names any stranded non-restorable
                # move-aside from on-disk truth and re-stamps outcome["log"].
                await _roll_back_post_script_refusal(
                    name,
                    app_source,
                    app_source_dir(name),
                    log_lines,
                    layout_before=layout_before,
                    checkout_preexisted=bool(build_result.get("_checkout_preexisted")),
                    pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                    manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                    manifest_snapshot=build_result.get("_pre_update_manifest"),
                    restore_from=_restorable_or_none(
                        build_result.get("_pending_stale_cleanup"),
                        build_result.get("_restorable_stale"),
                    ),
                )
                outcome = {
                    "ok": False,
                    "name": name,
                    "error": f"blocked by admission policy: {denied}",
                }
                return outcome

        # DESKTOP GATE, final pass — the build judged the requirements waiver on
        # the manifest that ENTERED the install script, and the script ran with
        # write access to the checkout: it can add `backend.hooks` (imported into
        # this process, which the app deps tree never reaches) or drop the entry
        # point / stdio server the waiver was granted for. Whatever is on disk
        # NOW is what registers and what the runtime later loads from, so the
        # same verdict is re-derived from it, by the same owner, with the same
        # ownership input (`is_self_managed`: a self-managed entry is registered
        # from its manifest alone and is never provisioned, so it keeps the
        # refusal) — and, being the FINAL pass, strictly: an entry file the
        # script was expected to create but did not, or a `requirements.txt`
        # link still dangling, is refused here (the build pass let absence
        # through because the script had not yet had its window). A refused
        # checkout is rolled back exactly like the post-script admission denial
        # above — nothing is enabled. Off-loop like the app.json read above:
        # the verdict stats the checkout.
        try:
            desktop_refusal = await asyncio.to_thread(
                _desktop_build_refusal,
                app_source,
                AppManifest.from_dict(manifest_data),
                self_managed=is_self_managed,
                final=True,
            )
            refusal_code = DESKTOP_BUILD_STEP_UNSUPPORTED
        except InstalledTreeRefused as exc:
            # The preview copy produced a tree `install_app` would refuse (a root
            # `data` that is not a directory): the install's own refusal, before
            # any transaction touched the app directory. Rolled back below like
            # the desktop refusal, reported WITHOUT its code -- a browser install
            # refuses the same tree, so the reader is not sent there.
            desktop_refusal, refusal_code = str(exc), ""
        except RuntimeError as exc:
            # The preview COPY itself failed (`_installed_tree_preview` wraps the
            # copy's `OSError`): not a verdict but a checkout the copy cannot read
            # -- and the script just ran with write access to it, so an unreadable
            # leaf, a FIFO or a symlink loop it left is the same third-party-script
            # write access every post-script gate rolls back. Left in the live
            # slot, a fresh clone so poisoned makes every retry fail at the build
            # pass before any script runs again, and nothing else removes it (the
            # `finally` restores a moved-aside checkout, which a first install
            # never has). So the failure takes the one post-script rollback too:
            # a fresh clone is deleted whole and the slot is empty for a clean
            # re-clone, a pre-existing checkout has its appeared layout files set
            # aside and is reset to its last-good state. Reported as the ordinary
            # error it is -- the cause sentence, no desktop code, retryable.
            preview_failure = str(exc)
            logger.warning("final desktop gate could not preview %s: %s", name, preview_failure)
            log_lines.append(preview_failure)
            await _roll_back_post_script_refusal(
                name,
                app_source,
                app_source_dir(name),
                log_lines,
                layout_before=layout_before,
                checkout_preexisted=bool(build_result.get("_checkout_preexisted")),
                pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                manifest_snapshot=build_result.get("_pre_update_manifest"),
                restore_from=_restorable_or_none(
                    build_result.get("_pending_stale_cleanup"),
                    build_result.get("_restorable_stale"),
                ),
            )
            # Through `outcome`, like every post-clone exit: the `finally` stamps
            # the log (the rollback's own lines included) onto this same dict.
            outcome = {"ok": False, "name": name, "error": preview_failure}
            return outcome
        if desktop_refusal:
            log_lines.append(_refusal_line(verb, desktop_refusal))
            try:
                sel().log_api_access(
                    caller="app_install_from_registry",
                    operation="desktop_build_gate_final",
                    outcome="rejected",
                    resources=f"name={name!r}",
                    error=desktop_refusal,
                )
            except Exception as exc:  # audit failure must never mask the refusal
                logger.debug("SEL audit failed for %s final desktop gate: %s", name, exc)
            # The layout inputs the script created go first, then the checkout is
            # rolled back -- the one post-script rollback every gate after the
            # script shares (why the order matters is on the helper).
            await _roll_back_post_script_refusal(
                name,
                app_source,
                app_source_dir(name),
                log_lines,
                layout_before=layout_before,
                checkout_preexisted=bool(build_result.get("_checkout_preexisted")),
                pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                manifest_snapshot=build_result.get("_pre_update_manifest"),
                restore_from=_restorable_or_none(
                    build_result.get("_pending_stale_cleanup"),
                    build_result.get("_restorable_stale"),
                ),
            )
            outcome = {"ok": False, "name": name, "error": desktop_refusal}
            if refusal_code:
                outcome["code"] = refusal_code
            return outcome

        # Provenance is pinned from the FINAL state — after the build, the
        # install script, and the last identity/admission gates: the exact
        # commit the checkout sits at, and whoever signed the manifest that
        # actually registers. Resolving either any earlier would let onInstall
        # advance the checkout or swap the manifest and have provenance record
        # a predecessor. Purely observational: never denies; unsigned yields "".
        source_commit = await asyncio.to_thread(_resolved_clone_commit, clone_root)
        source_signer = await asyncio.to_thread(
            verified_signer, AppManifest.from_dict(manifest_data)
        )

        # Step 3: Resolve dependencies (if declared in manifest)
        deps_data = manifest_data.get("dependencies")
        if deps_data and isinstance(deps_data, dict):
            from kiro_crew.apps.dependencies import resolve_dependencies as _resolve_deps
            from kiro_crew.apps.manifest import Dependencies as _Deps

            deps = _Deps.from_dict(deps_data)
            dep_result = await _resolve_deps(name, deps)
            if dep_result.installed:
                log_lines.append(f"Installed {len(dep_result.installed)} dependency(ies)")
            if dep_result.failed:
                log_lines.append(
                    f"Failed to install {len(dep_result.failed)} dependency(ies): {', '.join(dep_result.failed)}"
                )
            if dep_result.missing:
                log_lines.append(f"Missing commands: {', '.join(dep_result.missing)}")

        # A clone/build/install script can take minutes. Recheck at the shared
        # replacement boundary so startup execution that became retained during
        # that work cannot overlap either managed file replacement or
        # self-managed metadata replacement.
        startup_refusal = await _retained_startup_refusal(name, log_lines)
        if startup_refusal is not None:
            outcome = startup_refusal
            return outcome

        # Step 4: Register with Kiro Crew
        if is_self_managed:
            # Pre-register with manifest from the cloned repo so the app
            # appears in Installed tab immediately (with openCommand, icon, etc.)
            # The app will update its own registration on next launch.
            # ``manifest_data`` is the identity-checked read from above — reusing
            # it avoids a second read that could see different bytes.
            from kiro_crew.apps.manager import register_external_app

            display = manifest_data.get("displayName", name)
            version = manifest_data.get("version", "0.0.0")
            reg_result = register_external_app(
                name=name,
                version=version,
                display_name=display,
                source=f"{SOURCE_REGISTRY_PREFIX}{name}",
                manifest_data=manifest_data,
                origin="registry",
                source_repository=persisted_git_url,
            )
            if not reg_result.ok:
                # A refused registration leaves the prior record as it was, so the
                # install fails with the refusal and `durable_success` stays False:
                # the `finally` puts the previous checkout back beside that record,
                # and the caller sees the error that names the fix.
                log_lines.append(reg_result.error or "registration failed")
                outcome = {
                    "ok": False,
                    "name": name,
                    "message": "",
                    "error": reg_result.error or "registration failed",
                }
                return outcome
            # Set BEFORE the provenance bookkeeping below, because the registration
            # has landed: the clone is in place and the app updates its own
            # registration on next launch. Leaving it False would report success to
            # the caller while the `finally` rolled the source checkout back
            # underneath it.
            durable_success = True
            set_app_provenance(
                name,
                source=f"{SOURCE_REGISTRY_PREFIX}{name}",
                url=persisted_git_url,
                registry=source_registry,
                commit=source_commit,
                signer=source_signer,
            )

            log_lines.append("Pre-registered from cloned manifest (self-managed)")
            log_lines.append("App will update its own registration on next launch")
            # Retained moved-aside checkouts (the user can recover local edits;
            # swept after _STALE_CHECKOUT_RETENTION_DAYS) are reported by the
            # `finally`-owned reporter: durable_success is True, so it runs with
            # filter_restorable=False and names the genuinely-retained restorable
            # stale rather than letting it sit unlogged until the sweep.
            if official_entry:
                await install_receipt.dispatch_async(
                    name,
                    official=True,
                    kind=(
                        install_receipt.KIND_UPDATE if was_installed else install_receipt.KIND_FRESH
                    ),
                )
            outcome = {
                "ok": True,
                "name": name,
                "message": (
                    f"installed {name} from {_strip_git_target_userinfo(repo)} " "(self-managed)"
                ),
            }
            notice = getattr(reg_result, "notice", "")
            if isinstance(notice, str) and notice:
                outcome["notice"] = notice
            return outcome

        # Managed by Kiro Crew: copy to ~/.kiro/crew/apps/ and register resources
        log_lines.append("Installing app...")
        # Lock-free: the route handler holds app_lifecycle_lock(name) across
        # the whole transaction (clone/build → copy → register → backend
        # start); asyncio.Lock is not reentrant, so no acquisition here.
        existing = get_app(name)
        # Off-loop: install_app/update_app do a blocking filesystem copy
        # that can take minutes on large source trees — on the loop it
        # would trip the loop-stall watchdog and kill the gateway.
        # Preserve the long-standing one-positional-argument manager contract.
        # The scoped coordinate is copied into asyncio.to_thread's context, so
        # the manager still performs its final repository-binding check and
        # writes safe provisional provenance without trusting app.json.
        with registry_source_repository(persisted_git_url):
            if existing:
                result = await asyncio.to_thread(update_app, str(app_source))
            else:
                result = await asyncio.to_thread(install_app, str(app_source))
        log_lines.append(result.message or result.error or "done")

        # Record the source marker plus structured provenance, so a later update
        # resolves the source this install actually came from rather than
        # whichever entry happens to answer to the bare name. This is also what
        # self-heals a legacy record: its next successful update writes the full
        # provenance it was missing.
        if result.ok:
            # BEFORE the bookkeeping below, not after. `install_app`/`update_app` has
            # already copied the files into place, so the installed app IS updated. If
            # provenance persistence then raises, deciding "not durable" and rolling
            # the SOURCE checkout back would leave installed files from the new
            # version beside a source tree from the old one -- a torn state worse than
            # either outcome. A failed receipt is a bookkeeping problem to log; it does
            # not un-install what is installed.
            durable_success = True
            set_app_provenance(
                result.name,
                source=f"{SOURCE_REGISTRY_PREFIX}{name}",
                url=persisted_git_url,
                registry=source_registry,
                commit=source_commit,
                signer=source_signer,
            )
            # Retained moved-aside checkouts are reported by the `finally`-owned
            # reporter (durable_success is True, filter_restorable=False), so a
            # genuinely-retained restorable stale is named rather than sitting
            # unlogged at `.stale-*` until the sweep. NOTE set_app_provenance
            # above runs while durable_success is already True: if it raises, the
            # generic `except` catches it, the `finally` does NOT restore (durable
            # success), and it reports with filter_restorable=not durable_success
            # = False — so the restorable stale is reported, not stranded.
            if official_entry:
                # Detached best-effort telemetry runs only after durable success.
                await install_receipt.dispatch_async(
                    name,
                    official=True,
                    kind=(
                        install_receipt.KIND_UPDATE if was_installed else install_receipt.KIND_FRESH
                    ),
                )
        # Install/update failed AFTER a successful clone+build: durable_success
        # stays False, so the `finally` restores the restorable stale and its
        # reporter filters it out — no per-exit report is needed here.

        outcome = {
            "ok": result.ok,
            "name": name,
            "message": result.message,
            "error": result.error,
        }
        if result.notice:
            # e.g. ``session_approval_reconsent``: the app was left disabled on
            # purpose and the routes must neither start it nor report plain success.
            outcome["notice"] = result.notice
        return outcome

    except Exception as exc:
        logger.exception("Failed to install %s from registry", name)
        # Retained-stale reporting and restorable-stale restoration are owned by
        # the `finally` below. It reports with filter_restorable=not
        # durable_success, which is precisely why an exception raised AFTER
        # durable_success was set (e.g. set_app_provenance) still names the
        # genuinely-retained restorable stale instead of stranding it.
        outcome = {"ok": False, "name": name, "error": str(exc)}
        return outcome
    finally:
        # RESTORATION BELONGS TO THE LIFETIME, NOT TO THE LIST OF FAILURES.
        #
        # A pinned install moves the previous checkout aside on every reinstall, and
        # this function has seven post-clone exits (containment, identity mismatch,
        # two admission gates, onInstall, the install step, the happy path) plus an
        # exception path and cancellation. Restoring on the branches instead meant an
        # `onInstall` that exited non-zero returned early and left the user's only
        # edited copy as a `.stale-*` sibling for the retention sweep to delete.
        #
        # One site, reached by every exit. It is a no-op unless a moved-aside
        # checkout exists AND the transaction did not durably succeed, so the
        # pre-clone exits and the happy path both pass through untouched.
        if not durable_success:
            try:
                pending = build_result.get("_restorable_stale") or []
                if pending:
                    _restore_moved_aside(
                        Path(pending[0]),
                        # `app_source_dir(name)`, NOT `build_result["pkg_dir"]`: every
                        # post-clone FAILURE dict omits `pkg_dir`, so reading it raised a
                        # KeyError that the broad catch below swallowed -- the
                        # restoration silently did nothing on exactly the exits it
                        # exists for. The destination is a function of the app name, so
                        # derive it instead of depending on a key the failure paths do
                        # not carry. It is the clone ROOT either way: a `subdirectory`
                        # entry points `app_source` inside the tree, while the
                        # moved-aside sibling replaces the whole checkout.
                        app_source_dir(name),
                        log_lines,
                        "the install did not complete",
                    )
            except Exception:  # noqa: BLE001 - never mask the outcome being returned
                # WARNING, not debug: this catch is what hid the KeyError above for
                # four review rounds. A restoration that could not run is a possible
                # data loss, so it has to be visible in the log the user sees.
                logger.warning(
                    "could not restore the moved-aside checkout for %r", name, exc_info=True
                )
                log_lines.append(
                    "WARNING: the previous checkout could not be restored; recover it "
                    "from the .stale-* sibling directory"
                )

        # THE reporter, owned by this `finally` and nowhere else. Placed AFTER
        # the restore block above so it reports on-disk truth: a restorable stale
        # the restore just put back must not then be named as retained. The flag
        # is derived, not hand-mirrored at each exit — `not durable_success` is
        # exactly the restore condition above, so a failure exit (restored) files
        # its restorable stale out and a durable-success exit (never restored)
        # keeps it. This is the whole point of the consolidation: a new exit
        # added to this function cannot forget the report or pass the wrong flag,
        # because there are no per-exit reports left to forget. A no-op unless a
        # move-aside exists (pre-clone exits and the happy-path-with-no-stale
        # pass through untouched).
        _report_retained_stale_checkouts(
            build_result, log_lines, filter_restorable=not durable_success
        )

        # Re-stamp AFTER the restore and the report above: each `return` built its
        # `outcome` dict WITHOUT a "log" key, deferring it to here so the restore
        # confirmation, the restore-failed WARNING, and the retained-stale lines
        # just produced all reach the caller. `outcome` is the SAME dict object
        # being returned (dicts are mutable), so setting its "log" key here is
        # what the caller receives. Pre-clone exits return bare dicts that already
        # carry their own "log" and never set `outcome`, so they skip this
        # backstop and keep their join.
        if outcome is not None:
            outcome["log"] = "\n".join(log_lines)
            # Scrub the internal move-aside/transaction bookkeeping keys from
            # the dict that leaves this function. They are consumed ABOVE (the
            # restore block and the reporter both read them off `build_result`,
            # never off `outcome`), so removing them here deprives no consumer.
            # Two of them -- `_pending_stale_cleanup` and `_restorable_stale` --
            # are `list[Path]`, which is not JSON-serializable, so a build
            # refusal that spreads `{**build_result}` into `outcome` would make
            # the API/SSE layer raise `TypeError` when it serialized the refusal.
            # Scrubbing the CLASS (every `_`-prefixed key) rather than those two
            # names closes it at the single seam: `_checkout_preexisted`,
            # `_pre_pull_commit`, and `_pre_update_manifest` are internal gate
            # state too, and no current or future exit can leak any of them once
            # they are stripped here. Underscore keys are internal by
            # convention; a response field the caller needs is never named `_x`.
            for _internal_key in [k for k in outcome if k.startswith("_")]:
                outcome.pop(_internal_key, None)
