"""Who may read, append and publish on a unit's log.

One question per function, each answered from the app's own manifest
``contributions`` declaration, intersected for unit kinds with what an operator
approved. The HTTP handlers and the WebSocket hub both call these, so the answer
cannot differ between the two surfaces.

Four properties are deliberate:

* **Disabled means denied.** An app token survives a disable (the secret is not
  rotated), so every answer here first asks whether the app is enabled --
  mirroring ``ws_event_scope.app_events_revoked``, which exists for the same
  reason on the frame side.
* **Reading is per kind, not per event type** (contract §2). A subscriber sees
  every event of a unit it may subscribe to, which is why the contract puts
  sensitive data in the durable store an event points at rather than in the
  event.
* **The prefix rule is re-checked at use, not trusted from install.** A manifest
  is a file on disk that an app trusted to run code can rewrite, so a pattern
  that does not begin with the app's own name is ignored here even though
  ``AppManifest.validate`` would have refused it at install time.
* **A unit KIND is authority, so it is not taken from the manifest alone.** The
  rule above cannot guard ``units``: a kind is not namespaced -- ``member`` is the
  gateway's -- so there is no prefix for a pattern to fail. An app that rewrote
  its own manifest to add ``units: ["member"]`` would otherwise have granted
  itself read over a member's whole log. Declared kinds are therefore intersected
  with :func:`apps.manager.approved_unit_kinds`, which answers from a
  gateway-owned record OUTSIDE every app's own tree, so a kind must be both still
  declared AND already approved. The record's location is the point: an approval
  kept inside the app's directory would be writable by the very app it limits.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from collections.abc import Iterator
from fnmatch import fnmatchcase
from pathlib import Path

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

#: Cache of each app's declaration, keyed on :data:`_generation` rather than on
#: a clock. This sits on the append hot path and the manifest read has no cache
#: of its own, so it must not repeat per request; a declaration also changes only
#: on a lifecycle event, and every one of those bumps the counter. A timer was
#: doing two jobs badly against that: re-reading a declaration that had not
#: changed, and leaving a changed one answerable until the window closed.
_cache: dict[
    str, tuple[int, int | None, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]]
] = {}
#: A Condition rather than a plain Lock so :func:`revoke` can WAIT on it: every
#: ``with _cache_lock`` below is unchanged, and the waiting is what lets a
#: revocation drain the commits already past the fence (see :data:`_inflight`).
_cache_lock = threading.Condition()

#: Commits per app that have passed the fence and not yet finished their durable
#: write. A fence check and the write it authorizes are two statements, and a
#: revocation landing between them would let an unauthorized event or row persist
#: -- so the fence check and the registration happen together under the lock, and
#: :func:`revoke` waits here until the app's count reaches zero.
#:
#: The lock is deliberately NOT held across the write: :func:`_declaration` takes
#: it on every grant question, including from the serving path, so holding it
#: across file IO would queue every app's grant reads behind one app's write --
#: the event-loop stall this module is otherwise careful to avoid.
_inflight: dict[str, int] = {}

#: How long :func:`revoke` waits for an app's in-flight commits before it gives up
#: and proceeds anyway. Teardown must not hang on a wedged writer, so the wait is
#: bounded and exhausting it is logged at warning -- a commit that outlives this
#: can still land, which is the residual the bound buys.
_DRAIN_TIMEOUT_SECS = 5.0


# Apps whose grant is being torn down. `is_app_enabled` still returns true until
# the config write later in teardown lands, so without this a concurrent
# `may_publish` between `invalidate()` and that write would re-populate the cache
# with a live grant and reopen the very window teardown_contributions exists to
# close. A revoked app is denied here regardless of its enabled state until
# `unrevoke` clears it (a re-enable re-registers trust).
_revoked: set[str] = set()

#: Bumped by every change to the answers above. A request that checked a grant,
#: then suspended (resolving a unit off-loop), cannot tell on resumption whether
#: its grant still holds: re-asking is not enough, because a same-name app
#: re-installed during the suspension answers yes while being a DIFFERENT app.
#: Comparing this counter answers the question that matters -- "did anything
#: about any grant change while I was away" -- and a mutation carries the value
#: it checked at so the commit can refuse rather than recreate torn-down state.
#:
#: This is the CACHE generation: it gates the warm-hit cache, where a broader
#: invalidation is harmless (a stale entry is re-read). The FENCE that gates a
#: commit and a live subscription is per-APP (:data:`_app_generation`), because a
#: single global counter bumped by EVERY app's lifecycle would make one app's
#: enable/disable invalidate every other app's in-flight commit and permanently
#: silence every other app's live subscription -- an unrelated-lifecycle denial.
_generation = 0

#: Per-app in-process fence generation, bumped ONLY by that app's own lifecycle
#: (:func:`revoke` / :func:`unrevoke` / :func:`invalidate` for the named app; a
#: global :func:`invalidate` bumps the cache generation but not these, since a
#: cache reset is not a per-app authority change). The commit fence and the live
#: delivery fence read THIS counter, not :data:`_generation`, so only the
#: subject app's lifecycle invalidates its own commits and sockets. Absent key
#: reads as 0.
_app_generation: dict[str, int] = {}


#: DURABLE, cross-process disable epoch. ``_generation`` above is process-local:
#: it dislodges a warm cache entry only for a lifecycle event THIS process ran. A
#: file-only CLI ``disable`` runs in a DIFFERENT process (the ordinary Windows /
#: no-AF_UNIX configuration where the owner socket the live-apply path needs is
#: absent), so it flips ``enabled=False`` on disk but never bumps a running
#: gateway's ``_generation`` -- and a warm live grant would keep authorizing the
#: disabled app's token until the 15-second reconciler poll. This epoch closes
#: that window WITHOUT a new CLI->gateway authority channel (there is none when
#: the owner socket is gone) and WITHOUT per-request manifest I/O: ``disable_app``
#: bumps this small gateway-owned file before it returns, and the warm-hit gate
#: below re-reads it through a cheap mtime guard, so the running gateway denies
#: the disabled app on its very next request. The read fails CLOSED: an unreadable
#: or corrupt epoch file forces a cold re-read, which consults ``is_app_enabled``
#: on disk and denies the now-disabled app.
#:
#: It lives under the agent-fenced ``.vault`` directory (like the cron grant
#: epochs), NOT at the config-dir root: a CONFINED contributor app that could
#: write this file would rewrite its OWN epoch and un-disable itself, defeating
#: the gate. ``.vault`` is denylisted for read AND write to every app-mediated
#: channel (``security.paths._SENSITIVE_HOME_DIRS``), and the leaf is also named
#: in ``_WRITE_PROTECTED_HOME_PATHS`` so the write side is refused explicitly.
_GRANT_EPOCHS_FILENAME = "grant_epochs.json"
#: (mtime_ns, epochs-map) memo so the steady-state cost of the warm-hit gate is a
#: single ``os.stat`` rather than a JSON read. ``None`` = not yet read.
_epoch_memo: tuple[int, dict[str, int]] | None = None


def _grant_epochs_path() -> Path:
    from kiro_crew.config.loader import config_dir

    return config_dir() / ".vault" / _GRANT_EPOCHS_FILENAME


def _read_disable_epochs() -> dict[str, int] | None:
    """The durable per-app disable-epoch map, mtime-memoized. ``None`` = unreadable.

    A MISSING file is the definitive never-disabled state (``{}``); an existing
    file that cannot be read or parsed returns ``None`` so the caller fails CLOSED
    (a silent ``{}`` would resurrect a grant a disable had retired). Cheap in the
    steady state: an ``os.stat`` decides whether the memo still holds, and the JSON
    is re-read only when the file's mtime moved.
    """
    global _epoch_memo
    path = _grant_epochs_path()
    try:
        mtime = os.stat(path).st_mtime_ns
    except FileNotFoundError:
        _epoch_memo = (0, {})
        return {}
    except OSError:
        return None
    memo = _epoch_memo
    if memo is not None and memo[0] == mtime:
        return memo[1]
    try:
        raw = os.fspath(path)
        with open(raw, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    epochs: dict[str, int] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, int):
            epochs[k] = v
    _epoch_memo = (mtime, epochs)
    return epochs


def _durable_disable_epoch(app: str) -> int | None:
    """*app*'s durable disable epoch, or ``None`` when the epoch state is unreadable."""
    epochs = _read_disable_epochs()
    if epochs is None:
        return None
    return epochs.get(app, 0)


def _grant_epochs_lock_path() -> Path:
    p = _grant_epochs_path()
    return p.with_suffix(p.suffix + ".lock")


def bump_disable_epoch(app: str) -> None:
    """Advance *app*'s durable disable epoch and persist it, before the caller returns.

    Called by :func:`apps.manager.disable_app` -- the single durable disable
    primitive both the gateway's own handler and the CLI's file-only path run --
    so a disable in ANY process leaves a mark a running gateway observes on its
    next grant check, closing the poll-latency window a warm cache would otherwise
    keep open. Best-effort and never fatal to the disable: a bump that cannot be
    written is logged, and the reconciler poll remains the backstop.

    The whole read-modify-write is serialized under an EXCLUSIVE cross-process
    file lock (``platform_compat.file_lock``) on a dedicated lock file, and the
    read is done INSIDE the lock: two concurrent bumps -- two disables in flight,
    or the gateway's own disable racing a CLI ``disable`` in another process --
    would otherwise both read the same base and the second atomic replace would
    clobber the first's increment, losing a bump (an app that stayed authorized
    because its epoch never moved). The lock file, the epoch file's parent dir and
    the epoch file itself are all created owner-only (``0o700`` dir, ``0o600``
    files) so a non-owner cannot plant or rewrite the epoch that gates a grant.
    The write stays an atomic same-directory replace so a torn write never leaves
    a half-file the reader treats as unreadable.
    """
    import tempfile

    path = _grant_epochs_path()
    lock_path = _grant_epochs_lock_path()
    try:
        parent = os.path.dirname(os.fspath(path))
        os.makedirs(parent, mode=0o700, exist_ok=True)
        # Dedicated lock file, precreated owner-only. A separate file from the
        # data file so the lock survives the data file's atomic replace (which
        # swaps the inode out from under any lock held on it).
        lock_fd = os.open(os.fspath(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with platform_compat.file_lock(lock_fd, exclusive=True):
                # Re-read INSIDE the lock so the increment is against the latest
                # durable value, not a snapshot another writer has since bumped.
                # Read the file directly here rather than through the mtime memo,
                # which is a per-process read cache and could be stale relative to
                # another process's write the lock just let complete.
                data = _read_disable_epochs_locked(path)
                base = data.get(app, 0) if isinstance(data, dict) else 0
                merged = dict(data) if isinstance(data, dict) else {}
                merged[app] = base + 1

                fd, tmp = tempfile.mkstemp(dir=parent, prefix=".grant_epochs-", suffix=".tmp")
                try:
                    # fchmod_safe, not os.fchmod: os.fchmod does not exist on
                    # Windows (it would raise AttributeError, which escapes the
                    # OSError handler and crashes the bump). The wrapper is a
                    # documented Windows no-op -- owner-only there comes from the
                    # profile ACL, not POSIX bits.
                    platform_compat.fchmod_safe(fd, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(merged, fh)
                    os.replace(tmp, os.fspath(path))
                except OSError:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
        finally:
            os.close(lock_fd)
        global _epoch_memo
        _epoch_memo = None  # force the next reader to re-stat and pick up the write
    except OSError:
        logger.warning(
            "contributions: could not persist the disable epoch for %r; the running "
            "gateway falls back to the reconciler poll to drop its warm grant",
            app,
            exc_info=True,
        )


#: Protected disabled-latch: the set of app names whose contribution grants are
#: HELD disabled, persisted in the same ``.vault`` dir as the epoch map so a
#: confined contributor app cannot write it. The epoch map alone is not enough to
#: close the file-only-disable hole: it only INVALIDATES the warm cache (forces a
#: cold re-read), and the cold re-read then trusts ``is_app_enabled``, which reads
#: the app's OWN writable ``installed.json`` -- a still-running backend can rewrite
#: ``enabled: true`` there and the next cold read re-grants it. The latch is the
#: authority the app cannot forge: ``disable_app`` adds the name here (owner-only
#: file under ``.vault``), grant resolution denies while the name is present
#: regardless of what the app's manifest/metadata say, and only an operator
#: ``enable_app`` removes it. A missing file is the definitive never-latched state;
#: an unreadable one denies (fail closed), matching the epoch read's contract.
_DISABLED_LATCH_FILENAME = "contribution_disabled.json"


def _disabled_latch_path() -> Path:
    from kiro_crew.config.loader import config_dir

    return config_dir() / ".vault" / _DISABLED_LATCH_FILENAME


def _disabled_latch_lock_path() -> Path:
    p = _disabled_latch_path()
    return p.with_suffix(p.suffix + ".lock")


def is_disabled_latched(app: str) -> bool | None:
    """Whether *app*'s contribution grants are HELD disabled by the protected latch.

    Returns True when the app is in the protected disabled set, False when it is
    definitely absent (a missing file is the never-latched state ``{}``), and
    ``None`` when the latch file exists but cannot be read or parsed -- the caller
    MUST treat ``None`` as "deny" (fail closed), exactly as the epoch read does,
    because a silent False would resurrect a grant a disable had latched off.
    """
    path = _disabled_latch_path()
    try:
        with open(os.fspath(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    disabled = data.get("disabled")
    if not isinstance(disabled, list):
        return None
    return app in {d for d in disabled if isinstance(d, str)}


def set_disabled_latch(app: str, *, disabled: bool) -> None:
    """Add or remove *app* from the protected disabled-latch, before the caller returns.

    ``disable_app`` calls this with ``disabled=True`` and ``enable_app`` with
    ``disabled=False`` -- so a disable in ANY process (the no-AF_UNIX/Windows
    file-only path included) leaves a mark a running gateway honours on its next
    grant check, and an app rewriting its own ``installed.json`` cannot lift it:
    only an operator enable removes the name here. Serialized under the same
    exclusive cross-process file-lock discipline as ``bump_disable_epoch`` (a
    dedicated lock file, owner-only dir and files, atomic same-dir replace), so
    two concurrent lifecycle operations cannot clobber each other's edit. Best
    effort and never fatal to the lifecycle op: a write that cannot land is
    logged, and the reconciler poll remains the backstop for a live gateway.
    """
    import tempfile

    path = _disabled_latch_path()
    lock_path = _disabled_latch_lock_path()
    try:
        parent = os.path.dirname(os.fspath(path))
        os.makedirs(parent, mode=0o700, exist_ok=True)
        lock_fd = os.open(os.fspath(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with platform_compat.file_lock(lock_fd, exclusive=True):
                current: set[str] = set()
                try:
                    with open(os.fspath(path), encoding="utf-8") as fh:
                        data = json.load(fh)
                    if isinstance(data, dict) and isinstance(data.get("disabled"), list):
                        current = {d for d in data["disabled"] if isinstance(d, str)}
                    else:
                        # The file parsed but its shape is wrong. Do NOT treat that
                        # as "no apps latched" and rewrite it (GPT 6.1): that would
                        # silently drop every OTHER app's disable latch and re-grant
                        # each still-running disabled app. Abort the write so the
                        # existing record is preserved untouched.
                        raise ValueError("disabled-latch record is malformed")
                except FileNotFoundError:
                    # Genuine absence: no app has ever been latched.
                    current = set()
                except (OSError, ValueError) as exc:
                    # The file EXISTS but could not be read (I/O fault) or is
                    # corrupt/malformed. Rewriting it as {app} alone would wipe
                    # every OTHER latched app and re-grant them (GPT 6.1 F1). Abort
                    # the write -- raise so the existing record is left untouched
                    # (the fail-closed is_disabled_latched read still denies a
                    # latched app meanwhile). Caught and logged by the outer handler.
                    raise OSError(
                        f"disabled-latch record at {path} exists but is unreadable "
                        f"({exc}); refusing to rewrite it and drop other apps' latches"
                    ) from exc
                if disabled:
                    current.add(app)
                else:
                    current.discard(app)
                fd, tmp = tempfile.mkstemp(dir=parent, prefix=".contrib_disabled-", suffix=".tmp")
                try:
                    platform_compat.fchmod_safe(fd, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump({"disabled": sorted(current)}, fh)
                    os.replace(tmp, os.fspath(path))
                except OSError:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
        finally:
            os.close(lock_fd)
    except OSError:
        logger.warning(
            "contributions: could not persist the disabled latch for %r; the running "
            "gateway falls back to the reconciler poll to drop its warm grant",
            app,
            exc_info=True,
        )


#: Protected per-app INSTALLATION generation, in the same ``.vault`` dir an app
#: cannot write. An app token carries the generation current at mint; after an
#: uninstall bumps it, a token minted for the PRIOR installation does not match
#: and ``validate_token_with_app`` refuses it -- closing the hole where a retired
#: app's unexpired token inherits a same-name REINSTALL's grants (the reinstall is
#: an ordinary upgrade path, so the token survives and its ``app`` claim is still
#: signature-valid; only the generation tells the two installations apart). A
#: missing file is generation 0 (the never-uninstalled state); an unreadable one
#: answers ``None`` so the validator fails CLOSED.
_INSTALL_GEN_FILENAME = "app_install_gen.json"


def _install_gen_path() -> Path:
    from kiro_crew.config.loader import config_dir

    return config_dir() / ".vault" / _INSTALL_GEN_FILENAME


def _install_gen_lock_path() -> Path:
    p = _install_gen_path()
    return p.with_suffix(p.suffix + ".lock")


def app_installation_generation(app: str) -> int | None:
    """*app*'s durable installation generation, or ``None`` when it is unreadable.

    0 for a never-uninstalled app (missing file). Callers that gate a credential
    on this MUST treat ``None`` as a mismatch (deny), never as 0: a silently-zero
    answer on a transient read fault would admit a stale-generation token.
    """
    path = _install_gen_path()
    try:
        with open(os.fspath(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    val = data.get(app, 0)
    return val if isinstance(val, int) else None


def bump_installation_generation(app: str) -> None:
    """Advance *app*'s durable installation generation, before the caller returns.

    Called by :func:`apps.manager.uninstall_app` so every token minted for the
    installation being removed becomes stale: a same-name reinstall reads the
    bumped generation, mints tokens stamped with it, and the validator refuses any
    token still carrying the prior value. Same exclusive-lock, owner-only,
    atomic-replace discipline as :func:`bump_disable_epoch`.

    FAIL CLOSED (GPT 6.1 F1): this retirement is the ONLY thing that invalidates a
    prior installation's still-signature-valid tokens, so a persistence failure
    MUST propagate -- it is raised, not logged-and-swallowed. The caller runs this
    BEFORE it destroys anything and treats a raise as a hard refusal of the
    uninstall (nothing deleted, retryable), rather than completing an uninstall
    that silently leaves a retired token resolving a same-name reinstall's grants.
    """
    import tempfile

    path = _install_gen_path()
    lock_path = _install_gen_lock_path()
    parent = os.path.dirname(os.fspath(path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    lock_fd = os.open(os.fspath(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with platform_compat.file_lock(lock_fd, exclusive=True):
            data: dict[str, int] = {}
            try:
                with open(os.fspath(path), encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    data = {
                        k: v for k, v in loaded.items() if isinstance(k, str) and isinstance(v, int)
                    }
                else:
                    # Parsed, but the top-level shape is not the expected map. Do
                    # NOT coerce it to {} and rewrite (GPT 6.1 F2): that silently
                    # drops every app's retirement generation and revives their
                    # retired tokens. Abort so the existing record is untouched.
                    raise ValueError("retirement-generation record is malformed")
            except FileNotFoundError:
                # Genuine absence: nothing has ever been retired, so an empty map
                # is the truth and this app becomes generation 1.
                data = {}
            except (OSError, ValueError) as exc:
                # The map EXISTS but could not be read (I/O fault, or a corrupt or
                # malformed body). Do NOT fall through to rewrite it as {} (GPT 6.1
                # F2): that would wipe every OTHER app's retirement generation and
                # revive each of their still-signature-valid retired tokens across
                # a same-name reinstall. Fail closed -- raise so the uninstall
                # caller refuses with nothing destroyed, rather than silently
                # dropping the retirements of unrelated apps.
                raise OSError(
                    f"installation-generation map at {path} exists but is unreadable "
                    f"({exc}); refusing to rewrite it and drop other apps' retirements"
                ) from exc
            data[app] = data.get(app, 0) + 1
            fd, tmp = tempfile.mkstemp(dir=parent, prefix=".app_install_gen-", suffix=".tmp")
            try:
                platform_compat.fchmod_safe(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                os.replace(tmp, os.fspath(path))
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    finally:
        os.close(lock_fd)


def _read_disable_epochs_locked(path: Path) -> dict[str, int]:
    """Read the epoch map directly (no memo), for use INSIDE the write lock.

    A missing file is ``{}`` (never disabled); an unreadable/corrupt file returns
    ``{}`` here because the caller is about to REWRITE the whole map under the
    lock -- treating a corrupt file as empty and replacing it is the recovery, and
    the READ path (``_read_disable_epochs``) still fails closed for grant checks.
    """
    try:
        with open(os.fspath(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError,):
        return {}
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, int)}


#: The count and per-string length ceilings a manifest's ``contributions`` list
#: is validated against at install time (``apps.manifest._MAX_CONTRIBUTION_PATTERNS``
#: / ``_MAX_CONTRIBUTION_PATTERN_CHARS``). Mirrored here as the bound cold grant
#: resolution applies to what it RETAINS, because ``get_app_manifest`` reads the
#: on-disk manifest an enabled app may have rewritten after that validation.
_MAX_CONTRIBUTION_PATTERNS = 32
_MAX_CONTRIBUTION_PATTERN_CHARS = 200


def _bounded_patterns(patterns: object, prefix: str) -> tuple[str, ...]:
    """Own-namespace patterns from *patterns*, bounded in count and length.

    Drops anything not a ``str``, not prefixed with ``<app>/``, or naming only
    the bare prefix. A pattern longer than ``_MAX_CONTRIBUTION_PATTERN_CHARS`` is
    DROPPED (not truncated -- a truncated glob would silently mean something
    else), and the whole list is capped at ``_MAX_CONTRIBUTION_PATTERNS`` entries.
    The ceilings match the manifest parser, so a post-validation manifest edit can
    never buy authority -- or match cost -- the install itself could not.
    """
    if not isinstance(patterns, (list, tuple)):
        return ()
    kept: list[str] = []
    for p in patterns:
        if not isinstance(p, str):
            continue
        if not p.startswith(prefix) or len(p) <= len(prefix):
            continue
        if len(p) > _MAX_CONTRIBUTION_PATTERN_CHARS:
            continue
        kept.append(p)
        if len(kept) >= _MAX_CONTRIBUTION_PATTERNS:
            break
    return tuple(kept)


def is_cached(app: str) -> bool:
    """Whether *app*'s declaration is cached against the CURRENT generation.

    For a caller deciding whether it must pay an executor hop before asking a
    grant question on the loop. Cheap: a dict read under the cache lock, no
    filesystem access. A revoked app answers True because it is answered from
    the tombstone without reading anything.
    """
    with _cache_lock:
        if app in _revoked:
            return True
        entry = _cache.get(app)
        if entry is None or entry[0] != _generation:
            return False
        # A cross-process disable can invalidate a warm entry without moving
        # _generation, so the same durable-epoch gate _declaration applies decides
        # whether this entry would still be honoured. An unreadable epoch is not
        # cached, so a caller must pay the executor hop and re-read.
        current_epoch = _durable_disable_epoch(app)
        return current_epoch is not None and entry[1] == current_epoch


def warm_grant_apps() -> list[str]:
    """Apps that currently hold a cached (live) contribution declaration.

    The gateway reconciler reads this to catch an OUT-OF-PROCESS disable/uninstall
    of a CONTRIBUTOR that has no hooks: such an app is in neither the loaded-hook
    set nor the enabled-hook-declaring set, so without this list the reconciler
    would never examine it and its warm grant would keep authorizing a token the
    operator disabled from the CLI. Excludes already-revoked apps (their grant is
    a tombstone, nothing left to reconcile). A dict snapshot under the lock, no
    filesystem access.
    """
    with _cache_lock:
        return [app for app in _cache if app not in _revoked]


def warm(app: str) -> None:
    """Resolve *app*'s declaration into the cache. MUST be called OFF the loop.

    This is the off-loop half of the fix for manifest I/O on the serving loop.
    The grant questions below are sync, because one of their callers is a sync
    WebSocket frame filter that cannot become async; so rather than making the
    read async, the read is done HERE, in an executor, before the loop asks. The
    auth middleware calls this through ``asyncio.to_thread`` for the app it just
    authenticated, so by the time any handler asks a grant question the answer is
    a dict read.

    Deny-safe by construction: it only calls :func:`_declaration`, every failure
    of which resolves to the empty triple.
    """
    _declaration(app)


def _declaration(app: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """``(events, projections, units)`` for *app*, or empty triple.

    Empty on every failure -- app absent, disabled, manifest unreadable -- which
    denies by default. Patterns not prefixed with ``<app>/`` are dropped here, so
    a caller cannot be handed a grant over someone else's namespace even if the
    file on disk claims one.

    A cold cache still reads the manifest on the CALLING thread, which is why
    :func:`warm` exists: the auth middleware resolves an app's declaration in an
    executor right after authenticating it, so the serving loop meets a warm
    cache. The inline read stays as the correctness fallback for a caller that
    arrives before any warm -- answering empty there instead would 403 an app's
    first request, which is a worse trade than a rare read. Generation keying is
    what makes it rare: it is now reachable only after a lifecycle event, not
    every time a 30-second timer lapsed.
    """
    empty: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] = ((), (), ())
    with _cache_lock:
        if app in _revoked:
            # Being torn down: deny regardless of enabled state, and do not cache
            # (the tombstone, not the cache, is the source of truth until it lifts).
            return empty
        entry = _cache.get(app)
        # Keyed on the grant GENERATION rather than a timer. A declaration
        # changes only when an app is installed, enabled, disabled or torn down,
        # and every one of those bumps the counter -- so a warm entry stays warm
        # instead of being re-read every 30 seconds, and a lifecycle change
        # invalidates it at once instead of at the end of a window.
        #
        # ALSO gated on the DURABLE disable epoch: ``_generation`` moves only for
        # a lifecycle event THIS process ran, so a file-only disable in another
        # process (the no-AF_UNIX/Windows case) would leave this warm entry live
        # until the reconciler poll. The epoch, bumped on disk by ``disable_app``
        # before the CLI returns, moves cross-process; a warm hit is honoured only
        # when the app's epoch is unchanged since the entry was cached. An
        # UNREADABLE epoch (``None``) is treated as changed -> a cold re-read,
        # which consults ``is_app_enabled`` on disk and denies a disabled app.
        current_epoch = _durable_disable_epoch(app)
        if (
            entry is not None
            and entry[0] == _generation
            and current_epoch is not None
            and entry[1] == current_epoch
        ):
            return entry[2]
        generation_at_read = _generation
        epoch_at_read = current_epoch

    resolved = empty
    try:
        from kiro_crew.apps.manager import (
            approved_unit_kinds,
            get_app_manifest,
            is_app_enabled,
        )

        # Protected disabled-latch wins over the app's own enablement metadata.
        # ``is_app_enabled`` reads the app's writable ``installed.json``, which a
        # still-running backend can rewrite to ``enabled: true`` after a file-only
        # CLI disable -- the epoch bump only forces this cold re-read, it does not
        # make the re-read trustworthy. The latch lives under ``.vault`` (app
        # cannot write it) and is lifted ONLY by an operator ``enable_app``, so a
        # latched (or unreadable -> None) app is denied regardless of what its own
        # metadata claims. Checked BEFORE ``is_app_enabled`` so the forgeable
        # signal never even runs for a held app.
        latched = is_disabled_latched(app)
        if latched is None or latched:
            resolved = empty
        elif is_app_enabled(app):
            manifest = get_app_manifest(app)
            if manifest is not None:
                prefix = f"{app}/"
                decl = manifest.contributions
                # Intersected, not read: see the fourth property in the module
                # docstring. `approved_unit_kinds` is empty for an app with no
                # entry in the approval record, so such an install contributes to
                # no kind until an operator approves one -- the app keeps every
                # other grant, and `units_pending_approval` shows the difference.
                approved = approved_unit_kinds(app)
                # Bound what we RETAIN here, not only what the manifest parser
                # accepted at install. `get_app_manifest` reads the on-disk
                # manifest, which an enabled app can rewrite AFTER install-time
                # validation. Cold resolution then caches whatever it finds and
                # matches every append against it, so an unbounded events/projections
                # list is unbounded memory and match time on the serving path. Apply
                # the same count and length ceilings the parser enforces
                # (apps.manifest._MAX_CONTRIBUTION_PATTERNS /
                # _MAX_CONTRIBUTION_PATTERN_CHARS) so a post-validation edit cannot
                # buy authority the install could not.
                resolved = (
                    _bounded_patterns(decl.events, prefix),
                    _bounded_patterns(decl.projections, prefix),
                    tuple(k for k in decl.units if k and k in approved)[
                        :_MAX_CONTRIBUTION_PATTERNS
                    ],
                )
    except Exception:
        logger.warning(
            "contributions: could not resolve the declaration for %r; denying by default",
            app,
            exc_info=True,
        )
        resolved = empty

    with _cache_lock:
        # A revoke that landed while we resolved (outside the lock) must win: do
        # not seed the cache with a live grant for an app now being torn down.
        if app in _revoked:
            return empty
        # Stamped with the generation this read STARTED at, so a lifecycle event
        # that landed mid-read leaves the entry stale and the next call re-reads
        # rather than trusting a value resolved against the old world. The durable
        # disable epoch is stamped alongside it: an epoch that could not be read
        # (``None``) is stored as ``None`` so the warm-hit gate above never matches
        # it and every subsequent read stays cold (fail-closed) until the epoch
        # file is readable again.
        _cache[app] = (generation_at_read, epoch_at_read, resolved)
    return resolved


def revocation_generation() -> int:
    """The current grant-state generation; see :data:`_generation`.

    Read it BEFORE the grant check, pass it to the mutation, and the mutation
    refuses if it has moved. Read after the check instead and the window is
    still open: a revoke landing between the two is invisible.

    App-agnostic: it moves only for a lifecycle event THIS process ran. A
    per-app fence that must also catch a CROSS-PROCESS disable uses
    :func:`grant_fence`, which folds the durable epoch in.
    """
    with _cache_lock:
        return _generation


#: Sentinel fence value meaning "the durable epoch could not be read". It is NOT
#: compared with ``==``/``!=`` -- a fixed sentinel equals itself, so an unreadable
#: fence captured at subscribe would match an unreadable fence read at delivery
#: and FAIL OPEN. Comparison goes through :func:`fence_admits`, which treats this
#: value as UNAVAILABLE and denies every comparison touching it, including
#: unavailable-vs-unavailable.
_FENCE_EPOCH_UNREADABLE = -1

#: Multiplier separating the per-app generation from the durable epoch inside one
#: composite fence int. Far above any plausible per-app lifecycle-event count
#: between a request's fence read and its commit, so the two components never
#: collide.
_FENCE_EPOCH_STRIDE = 1 << 32


def grant_fence(app: str) -> int:
    """A per-app fence folding *app*'s in-process generation AND durable epoch.

    Two moving parts, both scoped to *app*: its per-app in-process generation
    (:data:`_app_generation`, bumped only by *app*'s own revoke/unrevoke/
    invalidate) and its durable disable epoch (moved by a file-only disable in
    another process). A commit barrier and a live-delivery check compare a fresh
    ``grant_fence`` against the one the request captured, and either part moving
    makes them differ -- so *app*'s own lifecycle, in this process OR another,
    invalidates *app*'s in-flight commit and its socket, while an UNRELATED app's
    lifecycle leaves both untouched (it bumps only that other app's per-app
    generation, never a shared counter).

    An UNREADABLE epoch yields :data:`_FENCE_EPOCH_UNREADABLE`. Callers MUST NOT
    compare the result with ``==``/``!=`` -- use :func:`fence_admits`, which denies
    on that sentinel (including sentinel-vs-sentinel), so an unreadable epoch fails
    CLOSED rather than matching another unreadable read.
    """
    with _cache_lock:
        generation = _app_generation.get(app, 0)
    epoch = _durable_disable_epoch(app)
    if epoch is None:
        return _FENCE_EPOCH_UNREADABLE
    return generation * _FENCE_EPOCH_STRIDE + epoch


def fence_admits(captured: int, current: int) -> bool:
    """Whether a commit/delivery captured under *captured* may proceed at *current*.

    The ONE place fence values are compared, so the unavailable-fails-closed rule
    lives in exactly one spot. Admits ONLY when both fences are real (neither is
    :data:`_FENCE_EPOCH_UNREADABLE`) AND equal. An unavailable fence on EITHER
    side denies -- including unavailable-vs-unavailable, which a bare ``==`` would
    wrongly admit because the sentinel equals itself. An unverifiable fence is
    never a licence to write or deliver.
    """
    if captured == _FENCE_EPOCH_UNREADABLE or current == _FENCE_EPOCH_UNREADABLE:
        return False
    return captured == current


def is_revoked(app: str) -> bool:
    """Whether *app* currently carries a teardown/replacement revocation tombstone.

    A cheap locked read for the fan-out path: even a subscription that registered
    inside a revoke window (between a grant fence check and the registration, or
    during an app replacement) must not be handed events while its app is revoked,
    and the socket registry cannot know that on its own. Checking HERE, at
    delivery, closes that race regardless of when the socket registered -- the
    tombstone is the single source of truth until `unrevoke` lifts it.
    """
    with _cache_lock:
        return app in _revoked


def begin_commit(app: str) -> int:
    """Register an in-flight commit for *app*; returns the FENCE it begins in.

    Atomic with the fence read, and that is the whole point. The caller compares
    the returned value against the fence it was authorized in, and because the
    registration happened under the same lock hold there are only two orderings:
    this commit registers before a concurrent :func:`revoke` bumps, so that revoke
    waits for it; or the bump happened first, the returned value differs, and the
    caller abandons the write. Nothing lands in between.

    The returned value is the per-app :func:`grant_fence`, NOT a global counter:
    a cross-process disable moves only the durable epoch, and an UNRELATED app's
    lifecycle must not invalidate this commit -- so the fence folds *app*'s own
    per-app generation and durable epoch. It differs from the request's captured
    fence exactly when *app*'s own revoke/unrevoke landed OR a cross-process
    disable moved *app*'s epoch since the grant check, so both are refused. The
    per-app generation is read inside the lock alongside the ``_inflight``
    registration, keeping that half atomic with the drain guarantee; an unreadable
    epoch yields the fail-closed sentinel, which :func:`fence_admits` denies.

    Always paired with :func:`end_commit` in a ``finally`` -- a slot never released
    makes the app's teardown wait out the full drain timeout.
    """
    epoch = _durable_disable_epoch(app)
    with _cache_lock:
        _inflight[app] = _inflight.get(app, 0) + 1
        if epoch is None:
            return _FENCE_EPOCH_UNREADABLE
        return _app_generation.get(app, 0) * _FENCE_EPOCH_STRIDE + epoch


def outstanding_commits(app: str) -> int:
    """How many of *app*'s commits are past the grant fence and still unwritten.

    Read by teardown BEFORE it deletes the app's rows. The drain in :func:`revoke`
    is bounded, so it can return with a commit still outstanding; deleting the rows
    then is what makes that commit's write permanent residue -- a row whose
    publisher is gone and which nothing remains to correct.

    Sound as a guard because a revoked grant admits no NEW commit: the generation
    moved, so :func:`begin_commit`'s caller compares and abandons. What this counts
    is therefore exactly the set authorized under the retired grant, and it only
    ever shrinks while teardown runs.
    """
    with _cache_lock:
        return _inflight.get(app, 0)


def end_commit(app: str) -> None:
    """Release a slot taken by :func:`begin_commit` and wake a waiting drain."""
    with _cache_lock:
        remaining = _inflight.get(app, 0) - 1
        if remaining > 0:
            _inflight[app] = remaining
        else:
            _inflight.pop(app, None)
        _cache_lock.notify_all()


class EpochFenceUnavailable(Exception):
    """The durable-epoch fence could not be honoured, so a commit fenced on it
    must FAIL CLOSED rather than proceed.

    Raised by :func:`commit_epoch_lock` for BOTH conditions -- the epoch lock/read
    could not be established, or the epoch moved while the lock was held (the app
    was disabled cross-process mid-commit). The distinct messages keep the log
    honest about which fired; the store maps both to the same ``app_revoked``
    refusal, because an unverifiable OR a moved epoch equally forbids the write.
    """


@contextlib.contextmanager
def commit_epoch_lock(app: str, expect_fence: int | None) -> Iterator[None]:
    """Hold the durable-epoch file lock across a commit, fence-checked on entry.

    :func:`begin_commit` reads the durable epoch to build its fence, but it reads
    it WITHOUT the epoch file lock -- so a cross-process ``bump_disable_epoch``
    (which holds the EXCLUSIVE lock) can land between that read and the commit's
    durable write, and the commit would then persist under an epoch the disable
    already retired. This closes that race: it takes the epoch file's SHARED lock
    for the whole commit, reads the epoch AFTER acquisition, and compares the
    resulting fence against the one the request captured. While this shared lock
    is held, ``bump_disable_epoch`` cannot acquire its exclusive lock, so no
    disable can move the epoch under an in-flight commit; a disable that already
    moved it is caught by the post-acquisition fence check.

    Fail-closed on EVERY failure -- the lock cannot be taken, the epoch cannot be
    read, or the fence moved -- because an unverifiable epoch must not authorize a
    write. ``None`` is the unfenced caller (a host-side write, a test) and takes no
    lock, exactly as the generation fence permits.
    """
    if expect_fence is None:
        yield
        return
    lock_path = _grant_epochs_lock_path()
    lock_fd: int | None = None
    try:
        parent = os.path.dirname(os.fspath(lock_path))
        os.makedirs(parent, mode=0o700, exist_ok=True)
        lock_fd = os.open(os.fspath(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        if lock_fd is not None:
            os.close(lock_fd)
        raise EpochFenceUnavailable(f"epoch lock unavailable for {app!r}") from exc
    try:
        with platform_compat.file_lock(lock_fd, exclusive=False):
            # Read the epoch UNDER the shared lock: a concurrent bump holds the
            # exclusive lock, so this observes either the pre-bump or the fully
            # committed post-bump value, never a torn one.
            epoch = _durable_disable_epoch(app)
            if epoch is None:
                raise EpochFenceUnavailable(f"epoch unreadable for {app!r}")
            with _cache_lock:
                current = _app_generation.get(app, 0) * _FENCE_EPOCH_STRIDE + epoch
            if not fence_admits(expect_fence, current):
                raise EpochFenceUnavailable(
                    f"epoch moved for {app!r} while the commit lock was held"
                )
            yield
    finally:
        os.close(lock_fd)


def revoke(app: str) -> bool:
    """Hard-deny *app*'s grant while it is torn down, then drop its cache.

    Returns whether the drain COMPLETED: ``True`` once no commit authorized under
    the retired grant is still unwritten, ``False`` if the bounded wait lapsed
    with commits still in flight. The caller must not lift this tombstone
    (:func:`unrevoke`) while this returned ``False`` -- doing so would let the
    narrowed manifest go live BESIDE an outstanding old-authority write, so that
    write persists against a manifest that does not authorize it. The revocation
    is RETAINED instead until the drain is confirmed clear (a re-check, or the next
    lifecycle event), which is the honest form of "retain revocation until
    outstanding commits drain".

    Set BEFORE the enabled state flips: `is_app_enabled` still answers true until
    the config write later in teardown, so the tombstone -- not the enabled flag
    -- is what closes the window. `unrevoke` lifts it when trust is re-granted.

    The generation is bumped HERE rather than after the rows are deleted, so a
    mutation already in flight sees the change before teardown removes anything.

    Then the commits ALREADY past the fence are drained. Bumping alone does not
    reach them: they were authorized in the generation just retired and their
    writes are still outstanding, so returning here would let teardown delete rows
    that a commit writes straight back. Waiting makes the guarantee whole -- once
    this returns True, no commit authorized under the old grant is still unwritten.
    """
    global _generation
    with _cache_lock:
        _revoked.add(app)
        _cache.pop(app, None)
        _generation += 1
        _app_generation[app] = _app_generation.get(app, 0) + 1
        if _inflight.get(app):
            # `wait_for` releases the lock while it waits, which is what lets those
            # commits finish and `end_commit` notify.
            drained = _cache_lock.wait_for(
                lambda: not _inflight.get(app), timeout=_DRAIN_TIMEOUT_SECS
            )
            if not drained:
                logger.warning(
                    "contributions: %r still has %d commit(s) in flight after %.1fs; "
                    "RETAINING the revocation so the tombstone stays raised until they "
                    "drain, rather than lifting it beside an outstanding old-authority "
                    "write",
                    app,
                    _inflight.get(app, 0),
                    _DRAIN_TIMEOUT_SECS,
                )
                return False
    return True


def unrevoke(app: str) -> None:
    """Lift a teardown tombstone so a re-enabled app can be granted again."""
    global _generation
    with _cache_lock:
        _revoked.discard(app)
        _generation += 1
        _app_generation[app] = _app_generation.get(app, 0) + 1


def invalidate(app: str | None = None) -> None:
    """Drop the cached declaration for *app*, or for every app.

    Called by teardown so a disable takes effect on the next request rather than
    at the end of the TTL -- the frames and rows are torn down synchronously
    there, and leaving the grant answerable for another 30 seconds would let an
    append land after the rows it would have folded into were deleted.
    """
    global _generation
    with _cache_lock:
        if app is None:
            _cache.clear()
            # A no-arg invalidate is the global reset; lift every tombstone too so
            # it is a true reset rather than leaving apps permanently denied. Bump
            # every known per-app fence generation as well, so an in-flight commit
            # or a live subscription for any app is re-validated after the reset
            # rather than trusting a fence taken against the pre-reset world.
            _revoked.clear()
            for key in list(_app_generation):
                _app_generation[key] += 1
        else:
            _cache.pop(app, None)
            _app_generation[app] = _app_generation.get(app, 0) + 1
        _generation += 1


def _matches(patterns: tuple[str, ...], value: str) -> bool:
    """Whether *value* matches any pattern, ``*`` globbing, case-sensitive.

    ``fnmatchcase`` rather than ``fnmatch``: the latter applies the platform's
    case rules, so ``Mochi/Ping`` would match ``mochi/*`` on a case-insensitive
    filesystem and not on Linux -- an authority answer must not depend on that.
    """
    return any(fnmatchcase(value, pattern) for pattern in patterns)


def declares_contributions(app: str) -> bool:
    """Whether *app* declared any contribution at all.

    This is what grants the ``/api/eventlog/`` path prefix and the ``eventlog_*``
    frames. It is not authority over any particular unit, type or key: each
    request re-derives that from the same declaration.
    """
    events, projections, units = _declaration(app)
    return bool(events or projections or units)


def may_use_kind(app: str, kind: str) -> bool:
    """Whether *app* may subscribe to and append to units of *kind*."""
    _events, _projections, units = _declaration(app)
    return kind in units


def may_append(app: str, kind: str, event_type: str) -> bool:
    """Whether *app* may append *event_type* to a unit of *kind*.

    A BUILT-IN event type is refused first, the same shape and for the same
    reason as :func:`may_publish`'s built-in-key check. The declaration rule
    alone does not cover it: ``types.is_contributed_event_type``'s contract says
    a contributor's type is ``<app>/<name>`` with a namespace the built-ins do
    not own **because "an app cannot be named for one of these"** -- but nothing
    enforces that premise, since ``app_name_error`` reserves no namespace names.
    So an app installed as ``member`` declaring ``events: ["member/*"]`` passes
    ``Contributions.validate`` (the prefix matches its own name, ``member`` is a
    known kind) and its declaration then matches ``member/binding``, which is in
    ``ALL_EVENT_TYPES`` and which ``RosterProjection`` folds AUTHORITATIVELY --
    letting a contributor overwrite gateway-owned roster fields it never owned.

    Checking the type here is what the helper's own docstring asks for: it is
    syntax only, and "WHETHER a given app may append it is authority, decided at
    the HTTP boundary against that app's declared ``contributions.events``".
    """
    from kiro_crew.eventlog import types

    if not types.is_contributed_event_type(event_type):
        return False
    if not may_use_kind(app, kind):
        return False
    events, _projections, _units = _declaration(app)
    return _matches(events, event_type)


def may_publish(app: str, kind: str, key: str) -> bool:
    """Whether *app* may publish the projection *key* on a unit of *kind*.

    A built-in key is refused by the prefix rule alone -- ``roster`` has no
    ``<app>/`` prefix, so no declaration can match it -- but the check is written
    explicitly because "built-in keys cannot be published from outside" is the
    contract's own sentence, and a future kind whose built-in key happened to
    contain a slash would otherwise turn a silent no into a silent yes.
    """
    if key in _builtin_keys():
        return False
    if not may_use_kind(app, kind):
        return False
    _events, projections, _units = _declaration(app)
    return _matches(projections, key)


def _builtin_keys() -> frozenset[str]:
    from kiro_crew.eventlog import types

    return frozenset(types.ALL_PROJECTION_KEYS)
