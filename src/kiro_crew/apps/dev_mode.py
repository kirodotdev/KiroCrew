"""App dev mode — live-reload support for app UI development.

When an installed app has ``dev: true`` in its ``installed.json``:

* ``handle_app_ui_file`` serves its UI files with ``Cache-Control: no-store``
  (never cached, not even with revalidation), and
* a gateway-side watcher polls the app's ``ui/`` directory (symlinks followed —
  the recommended dev setup symlinks ``ui/`` to the developer's source tree)
  and broadcasts an ``app_reload`` WebSocket event whenever any file changes.
  The dashboard's AppHost reloads so edits appear without a manual refresh.

Link the whole ``ui/`` DIRECTORY, never individual files inside it: since
#6809 the UI route opens the final name with ``O_NOFOLLOW`` (the swap-resistant
open that closed the check-then-reopen window), so a per-file symlink like
``ln -s ~/src/app/dist/index.mjs ui/index.mjs`` answers 404 — indistinguishable
from "not built yet". The directory link keeps working because the route
resolves the ui root before validating against it.

Toggling dev mode is a metadata-only change (``installed.json``), picked up by
the watcher within one poll interval — no gateway restart needed.

Cost model (dev mode is off for essentially all production gateways):
  The watcher must NOT make every always-on gateway pay for a dev-only feature.
  ``set_dev_mode`` maintains a tiny sentinel file (:data:`_DEV_SENTINEL`) listing
  the app names currently in dev mode. Each tick the watcher ``stat()``s only
  that one file and re-reads it solely when it changes — so the zero-dev-apps
  steady state is a single ``stat()`` per second with no ``list_apps()`` call
  (which reads every app's manifest and can even *write* ``installed.json``) and
  no ``ui/`` walks. Only when at least one app is in dev mode does it walk that
  app's ``ui/`` tree. The set is also mirrored into an in-memory cache
  (:data:`_dev_apps_cache`) so ``handle_app_ui_file`` can decide the cache header
  without any per-request disk IO on the event loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from kiro_crew import platform_compat
from kiro_crew.apps.manager import (
    _check_path_safety,
    _read_installed,
    _write_installed,
    app_dir,
    apps_dir,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.security import is_sensitive_path, path_contains_sensitive
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECS = 1.0
#: Safety cap on files scanned per app per tick — a runaway ui/ dir (e.g.
#: node_modules symlinked in) must not stall the loop.
_MAX_SCAN_FILES = 2000

#: Mask folding each file hash into a fixed-width non-negative int so the
#: accumulated digest stays bounded regardless of file count.
_DIGEST_MASK = (1 << 63) - 1

#: Sentinel file (a JSON array of app names in dev mode) under ``apps_dir()``.
#: ``list_apps()`` skips non-directory entries, so this file is invisible to it.
_DEV_SENTINEL = ".dev-apps.json"

#: Operator grant record (a JSON object mapping app name -> the RESOLVED ui
#: root granted, as ``os.path.realpath`` of ``<install>/ui`` at toggle time)
#: under ``apps_dir()``, written ONLY by :func:`set_dev_mode` /
#: :func:`remove_dev_app` — never created by the startup reconcile. The
#: sentinel above is a WATCH/no-store convenience that
#: :func:`_reconcile_sentinel_from_installed` rebuilds from each app's own
#: (app-writable) ``installed.json``, so a sentinel entry can be laundered by
#: an app that writes ``dev: true`` to its own metadata and waits for a
#: restart. This record is the AUTHORIZATION half the UI route requires
#: (#6809). Binding the grant to the SPECIFIC resolved root (not a bare name)
#: is load-bearing: it makes the grant self-invalidating — repointing ``ui``
#: after the toggle (an app update, a swapped link, a reinstall under the same
#: name) yields a root that no longer equals the granted one, so any grant
#: left behind by a crash mid-revoke or an uninstall race authorizes at most
#: the exact tree the operator approved, never a new target. The two files
#: stay separate on purpose — merging them would either re-open the
#: laundering path (reconcile adds) or break the documented out-of-band
#: ``dev: true`` watch contract (reconcile stops adding).
_DEV_GRANTS = ".dev-grants.json"

_watch_task: asyncio.Task | None = None

#: In-memory mirror of the dev-app set, refreshed by the watcher and by
#: ``set_dev_mode`` (in-process). Lets the UI-serving hot path avoid a disk read
#: on every request. Empty until first seeded — a newly-started gateway may
#: serve a dev app cached for up to one poll interval, which is harmless.
_dev_apps_cache: set[str] = set()


def _sentinel_path() -> Path:
    return apps_dir() / _DEV_SENTINEL


@contextmanager
def _sentinel_lock() -> Iterator[None]:
    """Hold a cross-process exclusive lock guarding the sentinel read-modify-write.

    The lock is taken on a DEDICATED ``.lock`` file, never on the sentinel
    itself: :func:`_write_dev_sentinel` replaces the sentinel via
    ``atomic_write`` (write-temp + rename), so a lock held on the sentinel's own
    fd would guard a soon-to-be-orphaned inode and let a concurrent toggle race
    in. Concurrent gateway/CLI toggles of different apps otherwise read the same
    sentinel, mutate private copies, and last-writer-wins clobbers the others'
    entries — silently dropping an app from the watched/no-store set. Serializing
    the read → mutate → write → cache-update sequence closes that race.
    """
    lock_path = apps_dir() / (_DEV_SENTINEL + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as fh:
        with platform_compat.file_lock(fh.fileno(), exclusive=True):
            yield


def _read_dev_sentinel() -> set[str]:
    """Read the raw dev-app-name set from the sentinel file (empty on any error)."""
    try:
        data = json.loads(_sentinel_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(data, list):
        return {str(x) for x in data}
    return set()


def _load_dev_apps() -> set[str]:
    """Read the sentinel and drop stale entries not backed by a dev-mode install.

    Defense-in-depth against a stale sentinel: an app uninstalled (or its
    ``installed.json`` ``dev`` flag cleared) out-of-band may leave its name in
    the sentinel. Left unfiltered, a DIFFERENT app later reinstalled under that
    same name would silently inherit dev-mode ``no-store`` serving and file
    watching despite its own metadata saying ``dev: false``. Filtering each
    candidate against the authoritative on-disk metadata prevents that
    misattribution even if the uninstall-time cleanup was skipped (e.g. a crash
    mid-uninstall). Off the hot path — called only by the watcher when the
    sentinel changes and at seeding, never per UI request.
    """
    keep: set[str] = set()
    for name in _read_dev_sentinel():
        try:
            meta = _read_installed(name)
        except Exception:
            continue
        if meta is not None and meta.dev:
            keep.add(name)
    return keep


def _write_dev_sentinel(names: set[str]) -> None:
    """Atomically write the dev-app-name set to the sentinel file."""
    path = _sentinel_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(sorted(names), indent=2) + "\n")


def _grants_path() -> Path:
    return apps_dir() / _DEV_GRANTS


def _read_dev_grants() -> dict[str, str]:
    """Read the operator grant map (empty on any error — absent means no grants).

    Maps app name -> the resolved ui root granted. A legacy/foreign shape
    (anything but a str->str object) reads as empty: an unparseable grant
    record must fail closed, never open.
    """
    try:
        data = json.loads(_grants_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict):
        return {
            str(k): str(v)
            for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str)
        }
    return {}


def _write_dev_grants(grants: dict[str, str]) -> None:
    """Atomically write the operator grant map to the grants file."""
    path = _grants_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(grants, indent=2, sort_keys=True) + "\n")


def _grant_record_unwritable() -> str | None:
    """Reason the grant record cannot be written from THIS process, or ``None``.

    The STRUCTURAL half of the operator-vs-agent runtime check (#6907): the
    grant record is sealed read-only against agent-sandboxed processes at the
    OS level (``sandbox._CREW_READONLY_LEAVES``), so opening it for write
    succeeds only outside that confinement. Unlike the environment marker,
    this cannot be evaded by scrubbing the environment or synthesizing command
    text at runtime — the kernel answers, not the command's spelling.

    The probe opens without ``O_TRUNC`` (never alters existing content) and
    with ``O_CREAT`` (an operator's first-ever toggle creates the empty file,
    which :func:`_read_dev_grants` already reads as "no grants").
    """
    path = _grants_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
    except OSError as exc:
        return f"the dev-mode grant record is not writable from this process ({exc})"
    return None


def _operator_attestation_refusal() -> str | None:
    """Reason this process cannot carry operator attestation, or ``None``.

    The runtime human-vs-agent check for ``confirm_out_of_install_root``:
    the flag is meaningful only when a HUMAN at a host terminal supplied it,
    so a process showing any evidence of agent-shell confinement is refused
    regardless of how the flag reached ``sys.argv``. Two independent tiers,
    each unforgeable in the refusal direction:

    * :func:`kiro_crew.sandbox.agent_confinement_evidence` — the launcher-set
      marker plus (on macOS) the kernel's own Seatbelt verdict;
    * :func:`_grant_record_unwritable` — the sealed-record write probe, which
      holds even when the environment was scrubbed (``env -u``) or the flag
      text was synthesized at runtime (``$(printf ...)``), because the OS
      sandbox denies the write no matter what the command looked like.
    """
    from kiro_crew.sandbox import agent_confinement_evidence

    evidence = agent_confinement_evidence()
    if evidence is not None:
        return evidence
    return _grant_record_unwritable()


def _scan_installed_dev_apps() -> set[str]:
    """Return the dev-app set derived authoritatively from every ``installed.json``.

    Unlike :func:`_load_dev_apps` (which only *filters* the sentinel against
    on-disk metadata and can never *add* an entry the sentinel is missing), this
    walks the apps directory and reads each app's ``installed.json`` ``dev``
    flag directly. It is the source of truth for reconciling the sentinel at
    startup, so a ``dev: true`` written to ``installed.json`` out-of-band
    (snapshot restore, hand-edit, a crash between the metadata write and the
    sentinel write) is actually honored — the documented contract field, not
    the internal sentinel, decides. Blocking filesystem IO — offload to a
    thread; called only once at watcher init, never on the hot path.
    """
    keep: set[str] = set()
    root = apps_dir()
    if not root.is_dir():
        return keep
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            meta = _read_installed(entry.name)
        except Exception:
            continue
        if meta is not None and meta.dev:
            keep.add(entry.name)
    return keep


def _reconcile_sentinel_from_installed() -> set[str]:
    """Rebuild the sentinel from ``installed.json`` so the docs' field is authoritative.

    Runs the authoritative scan and, only when it diverges from the current
    sentinel, rewrites the sentinel to match. Returns the reconciled dev-app
    set. Blocking IO — offload to a thread.

    The scan MUST run *inside* the cross-process lock, atomic with the sentinel
    read → compare → write → cache-update. If the scan ran before the lock, a
    concurrent :func:`set_dev_mode` toggle (which itself holds the lock) could
    land between the scan and the lock acquisition: the toggle writes
    ``installed.json`` ``dev: true`` and adds the app to the sentinel, then
    reconcile acquires the lock with its *stale* scan (missing that app), sees a
    divergence, and rewrites the sentinel to exclude it — silently dropping the
    just-toggled app from the watched/no-store set until the next restart
    reconcile, even though the toggle reported success.
    """
    with _sentinel_lock():
        installed = _scan_installed_dev_apps()
        current = _read_dev_sentinel()
        if current != installed:
            logger.info(
                "app dev-mode: reconciling sentinel from installed.json "
                "(sentinel=%s -> installed=%s)",
                sorted(current),
                sorted(installed),
            )
            _write_dev_sentinel(installed)
        # The grant record is REMOVE-ONLY here: prune grants whose app no
        # longer has VALID installed metadata (a crash between uninstall and
        # :func:`remove_dev_app`'s revoke), so a later reinstall under the
        # same name cannot inherit the authorization. Existence is tested via
        # ``_read_installed`` rather than ``is_dir()`` because an uninstall
        # with ``keep_data`` can leave (or recreate) the directory for the
        # preserved data while the app itself — its ``installed.json`` — is
        # gone. Never ADD a grant from ``installed.json`` — that is
        # app-writable metadata, and deriving the grant from it is exactly
        # the laundering path #6809 closes.
        grants = _read_dev_grants()
        live: dict[str, str] = {}
        for gname, groot in grants.items():
            try:
                gmeta = _read_installed(gname)
            except Exception:
                gmeta = None
            if gmeta is not None:
                live[gname] = groot
        if live != grants:
            logger.info(
                "app dev-mode: pruning grants for absent apps (%s)",
                sorted(set(grants) - set(live)),
            )
            _write_dev_grants(live)
        elif not _grants_path().exists():
            # Materialize the (empty) record at gateway startup: the Linux
            # sandbox launcher can only seal an EXISTING target read-only
            # (bind-over-self + MS_RDONLY skips absent paths), so a host that
            # never granted dev mode would otherwise leave the record
            # creatable from inside an agent sandbox until the first operator
            # toggle. Seatbelt denies by path pattern and does not need this,
            # but the record's existence also keeps the operator-attestation
            # write probe (#6907) exercising the same open() the seal governs.
            _write_dev_grants(live)
        _set_dev_cache(installed)
    return installed


def _stat_sentinel() -> tuple[float, int] | None:
    """Return the sentinel's (mtime, size), or None if it doesn't exist."""
    try:
        st = _sentinel_path().stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def _set_dev_cache(names: set[str]) -> None:
    global _dev_apps_cache
    _dev_apps_cache = set(names)


def set_dev_mode(
    name: str, enabled: bool, *, confirm_out_of_install_root: bool = False
) -> dict[str, Any]:
    """Toggle dev mode for an installed app. Returns a result dict.

    ``confirm_out_of_install_root`` is the operator's explicit acknowledgement
    for a grant whose ui root resolves OUTSIDE the app's install directory
    (see the confirmation gate below). Only host-boundary callers (the CLI)
    may pass it — the HTTP toggle route must not, because a request-body flag
    from the dashboard origin is app-controllable, not operator attestation.
    It never overrides the sensitive-path refusal, and an in-install root
    does not need it.

    Blocking filesystem IO — callers on the event loop MUST offload this to a
    thread (``await asyncio.to_thread(set_dev_mode, ...)``).
    """
    if not _check_path_safety(name):
        return {"error": f"invalid app name {name!r}"}
    # Cheap validation read outside the lock — builtin/not-installed status does
    # not change under us. The authoritative read → mutate → write happens again
    # INSIDE the lock below so the installed.json write is atomic with the
    # sentinel update.
    meta = _read_installed(name)
    if meta is None:
        return {"error": f"app {name!r} is not installed"}
    if meta.origin == "builtin":
        return {"error": "builtin apps cannot be put in dev mode"}
    # The installed.json write AND the sentinel read-modify-write run under one
    # cross-process lock so a concurrent gateway POST + CLI toggle of the same
    # app cannot interleave (write-meta A, write-meta B, sentinel B, sentinel A)
    # and leave installed.json saying dev:true while the sentinel excludes it —
    # silently disabling watching + no-store serving until a restart reconcile.
    # meta is re-read inside the lock so we never clobber a concurrent writer's
    # other installed.json fields (e.g. an update_app version bump).
    with _sentinel_lock():
        meta = _read_installed(name)
        if meta is None:
            return {"error": f"app {name!r} is not installed"}
        granted_root: str | None = None
        out_of_install_confirmed = False
        # VALIDATE BEFORE ANY WRITE: every enable (and any disable of a granted
        # app) mutates the grant record, and that record is sealed read-only
        # against agent-sandboxed processes at the OS level (#6907 — see
        # ``sandbox._CREW_READONLY_LEAVES``). Probing writability up front
        # keeps the refusal atomic: without it, an enable would write
        # installed.json and the sentinel and then fail at the (deliberately
        # last) grant write, leaving dev metadata claiming a state the
        # authorization record never granted. Dev-mode toggles from a
        # sandboxed process go through the gateway (the dashboard toggle),
        # which owns the record; the CLI path is for processes on the host
        # outside agent confinement.
        if enabled or name in _read_dev_grants():
            unwritable = _grant_record_unwritable()
            if unwritable is not None:
                sel().log_api_access(
                    caller=f"app:{name}",
                    operation="dev_mode_grant_write",
                    outcome="denied",
                    source="apps",
                    resources=str(_grants_path()),
                    error=unwritable,
                )
                return {
                    "error": (
                        f"cannot toggle dev mode for app {name!r}: "
                        f"{unwritable} — the grant record is operator-owned "
                        "and sealed against agent-sandboxed processes; use "
                        "the dashboard toggle, or run the CLI from a host "
                        "terminal"
                    ),
                    "code": "dev_mode_grant_record_readonly",
                }
        if enabled:
            # VALIDATE BEFORE ANY WRITE: a refusal must leave prior state
            # exactly as it was — an already-enabled app whose `ui` was
            # repointed to a sensitive root and then re-toggled must not have
            # its existing dev mode silently torn down by the refusal (the
            # old shape wrote metadata/sentinel first and "rolled back" by
            # unconditionally disabling, destroying prior state). The grant
            # binds the ui root's CURRENT resolved path, so it authorizes
            # exactly the tree the operator saw when toggling — anything that
            # later repoints ``ui`` invalidates it (re-toggle after
            # re-pointing to re-bind). A root escaping the install dir into a
            # SENSITIVE location (credential stores, key material) is refused
            # outright — no dev workflow legitimately serves those, and the
            # unauthenticated UI route must never be grantable onto them
            # (#6809).
            granted_root = os.path.realpath(apps_dir() / name / "ui")
            try:
                # Anchor = resolved apps ROOT + literal name (same rule as the
                # route): re-resolving through the app's own entry would race
                # a concurrent swap of that entry.
                Path(granted_root).relative_to(
                    Path(os.path.realpath(apps_dir())) / name
                )
            except ValueError:
                # BOTH directions of the sensitivity test: a root that IS
                # sensitive (inside `~/.ssh`) and a root that CONTAINS
                # sensitive leaves (`~/.docker` is not itself on the list —
                # only its `config.json` is — and `~` contains everything).
                # Either shape would let the unauthenticated UI route serve
                # credential material out of an allowlisted extension.
                if is_sensitive_path(granted_root) or path_contains_sensitive(
                    granted_root
                ):
                    return {
                        "error": (
                            f"app {name!r} has a ui root resolving to a "
                            f"sensitive location ({granted_root}) — the "
                            "dev-mode grant is refused"
                        )
                    }
                # OUT-OF-INSTALL grants additionally require the operator's
                # EXPLICIT confirmation, and only a caller on the gateway
                # host can supply it. The toggle route carries no
                # app-vs-operator identity — an app's UI bundle runs as a
                # same-origin module with the dashboard's own credentials,
                # so any request-body flag is data the app controls, never
                # an attestation — which is why the HTTP endpoint NEVER
                # passes this parameter and answers every out-of-install
                # enable with the refusal below. The CLI flag
                # (--confirm-out-of-install-root) is the one way to supply
                # it, and it is operator-only through THREE independent
                # tiers: running the CLI requires a process on the host
                # (app page-code cannot cross that); an AGENT shell on
                # the host is refused by the builtin deny rule
                # ``self-protection-dev-mode-out-of-root-confirm`` plus its
                # argv-structural floor (text tiers, evadable by runtime
                # construction of the flag); and — decisive — the runtime
                # human-vs-agent check below, whose kernel-enforced half
                # (the sealed grant record) holds even against a command
                # that synthesizes the flag at runtime or scrubs its own
                # environment. Both outcomes of the decision are
                # SEL-audited (see the emissions below). The load-bearing
                # serving guarantees remain the resolved-root equality
                # binding and the sensitivity screen above; this gate
                # closes the self-grant path to them and makes the escape
                # explicit at the call site.
                # Validate-before-write: like the sensitivity refusal above,
                # this must leave prior state untouched. Both the refusal and
                # the confirmed grant are SEL-audited: the permission decision
                # on an out-of-install root is exactly the kind of authority
                # change the event log exists to record.
                if not confirm_out_of_install_root:
                    sel().log_api_access(
                        caller=f"app:{name}",
                        operation="dev_mode_out_of_install_grant",
                        outcome="denied",
                        source="apps",
                        resources=granted_root,
                        error="out-of-install ui root requires operator confirmation",
                    )
                    return {
                        "error": (
                            f"app {name!r} has a ui root resolving outside "
                            f"its install directory ({granted_root}) — "
                            "granting dev mode on it requires explicit "
                            "operator confirmation: run `kirocrew app dev "
                            f"{name} --confirm-out-of-install-root` on the "
                            "gateway host"
                        ),
                        "code": "dev_mode_out_of_install_confirmation_required",
                    }
                # RUNTIME human-vs-agent check (#6907): the flag is operator
                # ATTESTATION, so it is honoured only from a process showing
                # no evidence of agent-shell confinement. The deny-list tiers
                # above stop an agent SPELLING the flag; this stops an agent
                # RUNNING it — including via runtime flag synthesis
                # (``$(printf ...)``) and environment scrubbing, which no
                # command-text scan can see.
                attestation_refusal = _operator_attestation_refusal()
                if attestation_refusal is not None:
                    sel().log_api_access(
                        caller=f"app:{name}",
                        operation="dev_mode_out_of_install_grant",
                        outcome="denied",
                        source="apps",
                        resources=granted_root,
                        error=(
                            "confirmation flag refused — not an operator "
                            f"process: {attestation_refusal}"
                        ),
                    )
                    return {
                        "error": (
                            f"app {name!r}: --confirm-out-of-install-root "
                            "carries operator attestation and was refused "
                            f"because {attestation_refusal}; run the command "
                            "yourself from a host terminal"
                        ),
                        "code": "dev_mode_operator_attestation_required",
                    }
                out_of_install_confirmed = True
        else:
            # Revoke the AUTHORIZATION first: every write below narrows state,
            # so a crash after any prefix of them leaves the SAFER remainder
            # (grant gone, metadata/sentinel possibly stale — watching without
            # authorization). The old order (grant last) failed open: a crash
            # after the metadata write left a live grant for a still-installed
            # app, which the reconcile never expires because the app still
            # exists — and the app could then write ``dev: true`` back into
            # its own metadata and re-satisfy the grant check unaided.
            grants = _read_dev_grants()
            if name in grants:
                grants.pop(name)
                _write_dev_grants(grants)
        meta.dev = enabled
        _write_installed(name, meta)
        names = _read_dev_sentinel()
        if enabled:
            names.add(name)
        else:
            names.discard(name)
        _write_dev_sentinel(names)
        if enabled and granted_root is not None:
            # The AUTHORIZATION record, written LAST on enable (mirror of the
            # revoke-first rule above: a crash mid-toggle must always leave
            # the un-granted state) and only here / in :func:`remove_dev_app`.
            grants = _read_dev_grants()
            grants[name] = granted_root
            _write_dev_grants(grants)
            if out_of_install_confirmed:
                # Audit AFTER the grant record lands, so the event asserts an
                # authority change that actually happened (a decision-point
                # event could record a grant a later write failure undid).
                sel().log_api_access(
                    caller=f"app:{name}",
                    operation="dev_mode_out_of_install_grant",
                    outcome="granted",
                    source="apps",
                    resources=granted_root,
                )
        # Update the in-process cache immediately so a same-process POST toggle
        # takes effect on the very next UI request (no wait for a watcher tick).
        _set_dev_cache(names)
    return {"name": name, "dev": enabled}


def remove_dev_app(name: str) -> None:
    """Drop *name* from the dev sentinel + in-memory cache (idempotent, best-effort).

    Called from :func:`kiro_crew.apps.manager.uninstall_app` so an app removed
    while in dev mode does not leave a stale sentinel entry that a later app
    reinstalled under the same name would inherit. Runs under the same
    cross-process lock as :func:`set_dev_mode`; never raises.
    """
    try:
        with _sentinel_lock():
            names = _read_dev_sentinel()
            grants = _read_dev_grants()
            if name not in names and name not in grants:
                return
            if name in grants:
                # Revoke the operator grant with the uninstall: a DIFFERENT app
                # later reinstalled under the same name must not inherit the
                # authorization to serve an out-of-install ui root. (Even when
                # this best-effort cleanup is skipped by a crash, the grant is
                # bound to the OLD resolved root and pruned at the next
                # reconcile once the app's metadata is gone.)
                grants.pop(name)
                _write_dev_grants(grants)
            if name in names:
                names.discard(name)
                _write_dev_sentinel(names)
                _set_dev_cache(names)
    except Exception:
        logger.debug("dev-mode sentinel cleanup for %r failed", name, exc_info=True)


def is_dev_mode(name: str) -> bool:
    """Whether an app is in dev mode, read authoritatively from disk.

    Blocking IO — do NOT call on the event loop per-request; use
    :func:`is_dev_mode_cached` on hot paths.

    Reads only the app's own ``installed.json`` — a file inside the install
    directory the APP ITSELF can write — so this answers "does the metadata say
    dev" and must never AUTHORIZE anything security-relevant. For an
    authorization decision use :func:`dev_mode_granted_root`, which also requires
    the gateway-owned sentinel.
    """
    if not _check_path_safety(name):
        return False
    meta = _read_installed(name)
    return bool(meta and meta.dev)


def dev_mode_granted_root(name: str) -> str | None:
    """The RESOLVED ui root the operator's dev-mode grant covers, or ``None``.

    Requires BOTH the operator grant record (:data:`_DEV_GRANTS`, a file at
    the apps ROOT written only by :func:`set_dev_mode` — never created by the
    startup reconcile) AND the app's ``installed.json`` ``dev`` flag.
    ``installed.json`` alone is the app's own writable metadata — an app that
    edits it to ``dev: true`` must not thereby authorize itself (#6809: the UI
    route relaxes root containment only under this grant, and a self-granted
    app could point its ui root at a credential directory). The watch
    sentinel is deliberately NOT consulted: the reconcile rebuilds it from
    app-writable metadata at every startup, so it proves watching, not
    authorization.

    The returned path is the root that was RESOLVED AND BOUND at toggle time;
    the caller must require its current resolved root to EQUAL this value,
    which is what makes a stale or inherited grant harmless — it covers one
    exact tree the operator approved, never whatever ``ui`` points at now.
    (The toggle endpoint itself carries no app-vs-operator identity — a
    same-origin caller reaches it too — which is why the binding, the
    sensitive-path screen and the explicit out-of-install confirmation at
    grant time, and this equality check carry the guarantee rather than the
    file's authorship alone.)

    Blocking IO — do NOT call on the event loop; callers run it off-loop, and
    it sits on an exceptional path (an out-of-install ui root), never on
    normal serving.
    """
    if not _check_path_safety(name):
        return None
    try:
        granted = _read_dev_grants().get(name)
    except Exception:
        # An unreadable grant record means the grant cannot be proven: fail closed.
        return None
    if granted is None or not is_dev_mode(name):
        return None
    return granted


def is_dev_mode_cached(name: str) -> bool:
    """Whether an app is in dev mode, from the in-memory cache (no disk IO).

    Safe to call on the event loop per-request. The cache is seeded at watcher
    init and refreshed each tick, so it lags disk by at most one poll interval.
    """
    return name in _dev_apps_cache


def _scan_ui_mtimes(ui_dir: Path) -> tuple[int, int]:
    """Return (file_count, digest) summarizing the app's ui/ tree.

    Follows the ui/ symlink (Path.rglob resolves through it) so the
    symlink-to-source dev setup is watched at the real source. Bounded by
    _MAX_SCAN_FILES; errors count as "no change" rather than crashing the loop.

    ``digest`` folds every file's (relative path, mtime, size) into a single
    order-independent accumulator (XOR of per-file hashes). Incorporating each
    file's own mtime and size means any edit that changes a file's metadata
    changes the digest — a bare ``(count, max_mtime)`` signature would miss a
    rewrite whose new mtime stays below another file's mtime (``cp -p``/
    ``rsync -a`` of an older bundle, a clock skew, a future-dated pin), leaving
    both count and max unchanged. XOR keeps the digest insensitive to rglob's
    traversal order.
    """
    digest = 0
    count = 0
    try:
        for p in ui_dir.rglob("*"):
            if count >= _MAX_SCAN_FILES:
                break
            try:
                if p.is_file():
                    count += 1
                    st = p.stat()
                    rel = p.relative_to(ui_dir).as_posix()
                    digest ^= hash((rel, st.st_mtime_ns, st.st_size)) & _DIGEST_MASK
            except (OSError, ValueError):
                continue
    except OSError:
        return (0, 0)
    return (count, digest)


async def _watch_loop(broadcast_fn: Callable[[str, dict], None]) -> None:
    """Poll dev-mode apps' ui/ dirs; broadcast ``app_reload`` on change.

    Steady-state cost when NO app is in dev mode: one ``stat()`` of the sentinel
    per tick — no ``list_apps()``, no ``ui/`` walks, no writes. The dev-app set
    is re-read (off the event loop) only when the sentinel's stat changes.

    Per-app state is (file_count, digest): a changed count catches
    adds/deletes, a changed digest catches edits (each file's mtime + size is
    folded in). The first observation of an app only seeds state (no reload
    storm at startup or on dev-mode enable).
    """
    state: dict[str, tuple[int, int]] = {}
    sentinel_sig: tuple[float, int] | None = None
    dev_apps: set[str] = set()
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SECS)
            # Cheap change-detection: stat one small file off the event loop.
            sig = await asyncio.to_thread(_stat_sentinel)
            if sig != sentinel_sig:
                sentinel_sig = sig
                dev_apps = await asyncio.to_thread(_load_dev_apps)
                _set_dev_cache(dev_apps)
                # Drop state for apps that left dev mode so re-enabling re-seeds.
                for gone in set(state) - dev_apps:
                    state.pop(gone, None)
            # Walk ui/ trees ONLY for dev apps (inherent to the feature; zero
            # dev apps => zero walks). Off the event loop — the rglob/stat walk
            # is synchronous filesystem IO (no-blocking-call-on-event-loop rule).
            for name in dev_apps:
                sig2 = await asyncio.to_thread(_scan_ui_mtimes, app_dir(name) / "ui")
                prev = state.get(name)
                state[name] = sig2
                if prev is not None and sig2 != prev and sig2 != (0, 0):
                    logger.info("app dev mode: %s ui changed — broadcasting reload", name)
                    try:
                        broadcast_fn("app_reload", {"app": name, "ts": time.time()})
                    except Exception:
                        logger.debug("app_reload broadcast failed", exc_info=True)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("app dev-mode watcher error")
            await asyncio.sleep(POLL_INTERVAL_SECS)


async def init_dev_mode_watcher(broadcast_fn: Callable[[str, dict], None]) -> None:
    """Start the singleton watcher task (idempotent). Called at gateway startup.

    Async because the one-time startup seed touches the filesystem: it walks
    every app's ``installed.json`` to reconcile the sentinel (so the documented
    ``dev`` field is authoritative even after an out-of-band write) and seeds
    the UI-serving cache. That read is offloaded via ``asyncio.to_thread`` so it
    never blocks the gateway's event loop during startup
    (``no-blocking-call-on-event-loop``).
    """
    global _watch_task
    if _watch_task is not None and not _watch_task.done():
        return
    # Reconcile the sentinel from installed.json and seed the in-memory cache
    # once, off the event loop, so the UI hot path is correct immediately at
    # startup rather than after the first tick.
    await asyncio.to_thread(_reconcile_sentinel_from_installed)
    _watch_task = asyncio.get_running_loop().create_task(_watch_loop(broadcast_fn))
    logger.info("app dev-mode watcher started (%.0fs cadence)", POLL_INTERVAL_SECS)


async def stop_dev_mode_watcher() -> None:
    """Cancel the watcher task and await its teardown (shutdown / tests).

    Awaits the cancelled task so the coroutine has actually unwound before we
    return — an in-process restart can then start a fresh watcher without the
    old one lingering on the event loop with a stale ``broadcast_ws``.
    """
    global _watch_task
    task = _watch_task
    _watch_task = None
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
