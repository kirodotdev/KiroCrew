"""The write path every channel settings route commits through.

``_LockedSectionWrite`` merges a channel's staged settings into its ``config.json``
section under the config lock and undoes them when the paired ``.env`` write fails;
``_write_env_or_roll_back`` and the ``_write_env_updates*`` helpers write ``.env``,
off the event loop through the facade's ``_write_env_off_loop``; ``_mask_secret``,
``_clean_id_list`` and ``_threshold_pct_rejection`` shape what a settings route
echoes and check what it accepts.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
from collections.abc import Awaitable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        _ABSENT,
        _loader,
        _write_env_off_loop,
        drained_to_thread,
        logger,
        platform_compat,
        publish_env,
    )


class _LockedSectionWrite:
    """One channel's ``config.json`` write, through ``update_config_locked``.

    Every per-channel settings saver of the handlers merges a staged dict into
    its own top-level section and, when the paired ``.env`` write then fails,
    undoes that merge. This is that step, written once, because each saver is a
    hand-copied credential-write skeleton and the copy is how an unlocked write
    arrives (see ``TestTheAtomicJsonWriteConfigFamilyIsRatcheted``).

    ``apply`` merges ``changes`` into -- and drops ``drop_keys`` from -- the named
    section of the config as re-read INSIDE the sidecar lock, so a concurrent
    edit to an unrelated key is preserved instead of being replaced by the
    caller's older snapshot. The advisory lock on ``<path>.lock`` is what keeps
    another PROCESS (``kirocrew config set``) from landing between the read and
    the write; the in-process ``_get_config_lock()`` the caller holds only
    serializes writers inside this one.

    ``restore`` undoes only the keys this write touched, and only where the
    stored value is still the one it wrote. Rewriting the file from a whole-file
    snapshot would revert whatever a concurrent writer landed; restoring the
    whole section would still discard a concurrent ``kirocrew config set
    <channel>.*`` that arrived between our write and the rollback. A key whose
    stored value differs from what we wrote has been changed by someone else
    since, and reverting it would destroy their edit to undo ours.

    ``seed_from`` names a legacy section (WeCom's ``wechat``) whose COPY becomes
    the starting point when the target section is absent, so an install still on
    the old key keeps its allow-list and thresholds across its first save instead
    of having them shadowed by a bare new section.

    ``drop_keys`` and ``blank_keys`` are the legacy plaintext-credential purge
    (``bot_token`` / ``app_password`` stored in ``config.json`` before the
    ``.env`` slot existed). They are decided against the document the write
    lands on, not the snapshot: a copy a concurrent writer landed after the
    handler's read is purged too, so clearing the ``.env`` credential cannot
    leave a fallback behind that a restart would authenticate with. ``drop``
    removes the key; ``blank`` sets it to ``""`` where present. Both are undone
    by ``restore``.

    ``finalize`` runs on the MERGED section, inside the lock, for a rule that
    couples the written keys to their stored counterparts (a soft/hard threshold
    pair): the pre-lock snapshot the handler validated against may not be the
    document the write lands on, so the coupled check has to be re-decided
    against the fresh one. It may adjust the section or raise; a raise aborts
    the write and propagates to the caller.

    Both run off the event loop through ``drained_to_thread``: the locked
    read-modify-write does file IO and may block on another process holding the
    lock, neither of which belongs on the loop, and a thread cannot be
    cancelled, so a cancelled request must not unwind (releasing
    ``_get_config_lock()`` and skipping its paired ``.env`` write) while the
    worker is still rewriting the file.
    """

    def __init__(
        self,
        path: Path,
        section: str,
        changes: dict[str, object],
        *,
        drop_keys: tuple[str, ...] = (),
        blank_keys: tuple[str, ...] = (),
        seed_from: str | None = None,
        finalize: Callable[[dict], None] | None = None,
    ) -> None:
        self._path = path
        self._section = section
        self._changes = dict(changes)
        self._drop_keys = drop_keys
        self._blank_keys = blank_keys
        self._seed_from = seed_from
        self._finalize = finalize
        # The values actually stored under the keys this write moved -- the
        # requested keys as ``finalize`` may have adjusted them, plus any other
        # key ``finalize`` changed; ``restore`` compares against these.
        self._written: dict[str, object] = dict(changes)
        # The pre-mutation section, captured inside ``apply`` because that is the
        # only point at which the pre-mutation state is known to be current: a
        # copy of the object, the raw non-object value that was stored under the
        # key, or ``_ABSENT`` when the key was not there.
        self._prior: Any = _ABSENT
        # What ``apply`` started a MISSING section from: the legacy copy, or ``{}``.
        # ``restore`` uses it to recognise a section that exists only because this
        # write created it, seeded contents included.
        self._created_from: dict | None = None

    def apply(self, fresh: dict) -> dict | None:
        """Merge into *fresh*; ``None`` when the section would be unchanged.

        ``None`` tells ``update_config_locked`` to skip the write, so a save
        whose every key already holds the requested value (or whose purge finds
        nothing to purge) rewrites nothing and wakes no watcher.
        """
        section = fresh.get(self._section)
        if self._section not in fresh:
            self._prior = _ABSENT
        else:
            self._prior = dict(section) if isinstance(section, dict) else section
        if not isinstance(section, dict):
            legacy = fresh.get(self._seed_from) if self._seed_from else None
            # A COPY: the legacy block is never mutated in place.
            section = dict(legacy) if isinstance(legacy, dict) else {}
            self._created_from = dict(section)
            fresh[self._section] = section
        for key in self._drop_keys:
            section.pop(key, None)
        for key in self._blank_keys:
            if key in section:
                section[key] = ""
        section.update(self._changes)
        touched = set(self._changes)
        if self._finalize is not None:
            pre = dict(section)
            self._finalize(section)
            # Every key ``finalize`` moved is ours to undo as well -- a soft
            # threshold it pulled down to a lowered hard one was never requested,
            # and a rollback that left it lowered would lose it silently.
            touched |= {
                k for k in set(pre) | set(section) if pre.get(k, _ABSENT) != section.get(k, _ABSENT)
            }
        self._written = {k: section[k] for k in touched if k in section}
        if isinstance(self._prior, dict) and section == self._prior:
            return None
        return fresh

    def restore(self, fresh: dict) -> dict:
        section = fresh.get(self._section)
        if not isinstance(section, dict):
            # Nothing of ours left to undo (the section is gone or was replaced
            # wholesale by another writer).
            return fresh
        # What each key held before we wrote it: the stored object, or -- for a
        # section we created -- the seed it was created from, so a key we wrote
        # OVER a seeded legacy value goes back to that value rather than away.
        if isinstance(self._prior, dict):
            before = self._prior
        else:
            before = self._created_from if self._created_from is not None else {}
        for key, written in self._written.items():
            if section.get(key) != written:
                continue  # not ours any more
            if key in before:
                section[key] = before[key]
            else:
                section.pop(key, None)
        for key in self._drop_keys:
            if key in section:
                continue  # someone re-set it since; theirs
            if key in before:
                section[key] = before[key]
        for key in self._blank_keys:
            if section.get(key) == "" and key in before:
                section[key] = before[key]
        # A section that only exists because we created it -- once our keys are
        # undone it is back to exactly what we seeded it with -- goes back to what
        # was there: nothing, or the non-object value (a hand-edited ``null``)
        # that was stored under the key. So a failed first-time save leaves
        # neither an empty scaffold, nor a copy of the legacy section shadowing
        # the original, nor a deleted value. A key someone else added to the
        # section since makes it theirs, and it stays.
        if not isinstance(self._prior, dict) and section == self._created_from:
            if self._prior is _ABSENT:
                fresh.pop(self._section, None)
            else:
                fresh[self._section] = self._prior
        return fresh

    async def commit(self) -> None:
        """Merge ``changes`` into the file. Raises ``ConfigReadError`` on a corrupt file."""
        # Looked up on the module at call time, not bound at import: the loader is
        # what tests patch to observe or interleave with this write.
        await drained_to_thread(
            functools.partial(_loader.update_config_locked, self._path, mutate=self.apply)
        )

    async def rollback(self, channel: str) -> None:
        """Undo ``commit`` after the paired ``.env`` write failed. Never raises."""
        try:
            await drained_to_thread(
                functools.partial(_loader.update_config_locked, self._path, mutate=self.restore)
            )
        except Exception:
            # A rollback that cannot run must not mask the original failure the
            # caller is already raising; the mismatch is logged instead.
            logger.exception("%s config rollback failed; config may lead .env", channel)


def _mask_secret(val: str) -> str:
    """Return a masked preview keeping the token prefix + last 4 chars.

    e.g. "xoxb-1234-abcd…wxyz" → "xoxb-••••wxyz". Empty string for no value.
    """
    if not val:
        return ""
    prefix = f"{val.split('-', 1)[0]}-" if "-" in val else ""
    tail = val[-4:] if len(val) >= 4 else ""
    return f"{prefix}••••{tail}"


def _clean_id_list(raw: object, is_valid: Callable[[str], bool], label: str) -> list[str]:
    """Validate and normalize a list of ID strings, dropping blanks.

    Raises ``ValueError`` (message safe to surface) when *raw* is not a list or
    an entry fails *is_valid*. Shared by the channel / enterprise-org fields.
    """
    if not isinstance(raw, list):
        raise ValueError(f"{label}s must be a list")
    out: list[str] = []
    for item in raw:
        s = str(item).strip()
        if not s:
            continue
        if not is_valid(s):
            raise ValueError(f"invalid {label}: {s}")
        out.append(s)
    return out


async def _write_env_or_roll_back(
    env_updates: dict[str, str | None],
    rollback: Callable[[], Awaitable[None]] | None,
) -> None:
    """Write a channel save's ``.env`` half, undoing its config half on failure.

    Every channel saver commits ``config.json`` first and then calls this, so the
    second half of the transaction exists once and a new saver cannot copy only
    part of it. *rollback* undoes the config commit; pass ``None`` when the save
    wrote no config.

    A failed write runs *rollback* and re-raises, so a refused save (the wide
    ``.env`` 409 included) changes nothing. A cancellation drains the write
    first and runs *rollback* only when the write itself failed: a write that
    landed before the cancel arrived already pairs with the committed config,
    and rolling the config back would split the pair it means to keep. The
    cancellation is re-raised either way.

    On success the process environment is updated to match: ``load_credentials()``
    lets ``os.environ`` win over ``.env``, so a replaced or cleared token would
    otherwise keep reading as installed until restart, and spawned children
    would inherit the stale value.
    """
    write = asyncio.ensure_future(_write_env_off_loop(env_updates))
    try:
        await asyncio.shield(write)
    except asyncio.CancelledError:
        await asyncio.gather(write, return_exceptions=True)
        failed = not write.cancelled() and write.exception() is not None
        if failed and rollback is not None:
            await rollback()
        raise
    except BaseException:
        if rollback is not None:
            await rollback()
        raise
    for key, new_val in env_updates.items():
        # The value the .env line now holds: a new channel token reads back as
        # its secret:// reference, the same value a restart propagates.
        line_val = _loader.vault_channel_env_value(key, new_val)
        if line_val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = line_val


def _wide_env_refusal(exc: _loader.EnvFileWideEncodingError) -> web.HTTPConflict:
    """The response for a channel save refused because ``.env`` is wide-encoded."""
    message = (
        f"{_loader.env_path()} is saved as {exc.wide_encoding}, which Kiro Crew "
        "cannot read. Re-save it as UTF-8 and save again. The .env was not changed."
    )
    return web.HTTPConflict(
        text=json.dumps({"error": message}),
        content_type="application/json",
    )


def _write_env_updates(updates: dict[str, str | None]) -> None:
    """Update select keys in config_dir/.env, preserving comments and order.

    A value of ``None`` deletes the key; new keys are appended. A new channel
    secret (:data:`~kiro_crew.config.loader.VAULT_CHANNEL_CREDENTIAL_KEYS`) is
    stored in the vault and its line written as the ``secret://KEY`` reference
    (:class:`~kiro_crew.secrets.migrate.ChannelSecretStore`). The write goes
    through :func:`kiro_crew.hidden_leaf_staging.publish_env`, which stages
    the temp inside the masked staging directory (never beside ``.env``, at a
    name no sandbox mask covers) and locks it down to its owner BEFORE any
    content byte is written: POSIX mode bits protect nothing on Windows
    (``fchmod_safe`` is a documented no-op there), so a lockdown applied after
    the write would leave the tokens readable under the directory-inherited
    DACL for the whole write. A refused lockdown warns rather than aborting a
    token save that already succeeded.

    CALLER CONTRACT: blocking, and every caller is an async request handler
    holding ``_get_config_lock()``, so each one must reach this through
    ``_write_env_off_loop`` rather than ``asyncio.to_thread`` directly --
    the drain there is what keeps the lock from being released mid-write.
    Offload the WHOLE call, never a part of it: the read-modify-write is one
    transaction, and a suspension point between the read and the rename would
    let a concurrent writer's keys be dropped by a write derived from lines
    nobody re-read. ``test/test_channel_env_write_off_loop.py`` pins both
    properties for all six channels.
    """
    ep = _loader.env_path()
    # Ensure the parent exists before opening the lock file (the .env itself may
    # not exist yet — e.g. first credential save into a fresh config dir).
    ep.parent.mkdir(parents=True, exist_ok=True)
    # Serialize this read-modify-write against the OTHER cross-process .env
    # writers (the `kirocrew secrets import` migrator and the WeChat/Weixin QR
    # handler) on the SAME advisory lock, derived from the shared helper so all
    # writers provably use one lock file. Without this, a channel/token save
    # here could interleave with the importer's rewrite (read stale bytes here,
    # or clobber the importer's commit), losing a freshly saved token or leaving
    # a `secret://` reference pointing at the wrong value. The lock wraps the
    # entire read → transform → atomic-rename sequence.
    # Lazy import: `secrets.migrate` is the CLI migration module and must stay
    # OFF the gateway boot path (messaging.py is imported at startup). Importing
    # it inside the writer keeps it off the module-import path so gateway
    # readiness is not delayed by loading the migration module.
    from kiro_crew.secrets.migrate import ChannelSecretStore, _env_lock_path

    lock_path = _env_lock_path(ep)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    if not platform_compat.try_acquire_lock(lock_fd, exclusive=True):
        os.close(lock_fd)
        raise OSError(
            f"{ep} is locked by another process (a secrets import or another "
            "credential save is in progress); no update performed. Retry once "
            "the other operation finishes."
        )
    try:
        # A new channel token goes to the vault and its .env line becomes the
        # secret:// reference; a failed .env write puts the vault back.
        store = ChannelSecretStore(ep)
        env_updates = store.stage(updates)
        try:
            _write_env_updates_locked(ep, env_updates)
        except BaseException:
            store.rollback()
            raise
        store.finish()
    finally:
        platform_compat.release_lock(lock_fd)
        os.close(lock_fd)


def _write_env_updates_locked(ep: "Path", updates: dict[str, str | None]) -> None:
    """The read-modify-atomic-rewrite of .env, run under the .env lock held by
    the caller (:func:`_write_env_updates`)."""

    lines = _loader.read_env_text(ep, encoding="utf-8").splitlines() if ep.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                seen.add(k)
                new_val = updates[k]
                if new_val is None:
                    continue
                out.append(f"{k}={new_val}")
                continue
        out.append(line)
    for k, new_val in updates.items():
        if k not in seen and new_val:
            out.append(f"{k}={new_val}")
    content = "\n".join(out) + ("\n" if out else "")
    publish_env(ep, _loader.env_bom_prefix(ep) + content)


def _threshold_pct_rejection(body: dict[str, Any], key: str) -> tuple[str, str] | None:
    """``(code, message)`` when *body[key]* is present and not a valid percentage.

    ``None`` when the key is absent or the value is in range. One home for the check
    across every channel that exposes context-window nudge thresholds, because
    ``isinstance(True, int)`` is True in Python: each site has to exclude bool
    explicitly or a JSON ``true`` reads as 1%, and a per-channel copy of that subtlety
    is a per-channel chance to omit it. The bounds come from the config loader, so a
    value this accepts is one the loader will not silently clamp.
    """
    if key not in body:
        return None
    from kiro_crew.config.loader import THRESHOLD_PCT_MAX, THRESHOLD_PCT_MIN

    pct = body.get(key)
    if isinstance(pct, int) and not isinstance(pct, bool):
        if THRESHOLD_PCT_MIN <= pct <= THRESHOLD_PCT_MAX:
            return None
    return (
        f"{key}_invalid",
        f"{key} must be an integer between {THRESHOLD_PCT_MIN} and {THRESHOLD_PCT_MAX}",
    )
