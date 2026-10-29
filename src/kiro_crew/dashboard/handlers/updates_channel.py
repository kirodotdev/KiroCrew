"""What this install follows and what it shows about releases.

``POST /api/update/auto`` (the auto-update policy), ``POST /api/update/channel``
(the release lane), ``POST /api/update/revalidate`` (a terminal update's request to
re-check), and the bundled changelog and release list.

Served through :mod:`kiro_crew.dashboard.handlers.updates`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
from pathlib import Path

from aiohttp import web

from kiro_crew.changelog import Release, build_release_list
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_path,
    overlay_pins,
    update_config_locked,
)
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.updates_check import _do_update_check, _invalidate_update_check
from kiro_crew.dashboard.handlers.updates_step_up import _audit_update_event
from kiro_crew.dashboard.handlers.updates_verdict import (
    _display_local_version,
    _display_version,
    _update_info,
)
from kiro_crew.dashboard.urls import is_loopback
from kiro_crew.platform.update_layout import detect_install_layout
from kiro_crew.platform.update_layout import release_channel as _release_channel
from kiro_crew.platform.update_layout import set_release_channel, wheel_update_command
from kiro_crew.platform.update_provider import resolve_provider

logger = logging.getLogger("kiro_crew.dashboard.handlers.updates")


async def api_update_auto(request: web.Request) -> web.Response:
    """POST /api/update/auto — toggle auto-update on/off."""
    owner_denied = await require_owner_dashboard_request(request, "update.auto")
    if owner_denied is not None:
        return owner_denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        # Stored verbatim below, and the update loop reads it for truthiness:
        # a string would read as ON whatever it says, and no switch can render
        # it. Refuse rather than coerce — which value the caller meant by
        # ``"false"`` is not ours to guess.
        return web.json_response(
            {"error": "enabled must be a boolean", "code": "invalid_enabled"}, status=400
        )

    # 409, not 200, when config.local.json owns the key — the same refusal the
    # trust-settings and MCP writers give an overlay-owned setting. Writing
    # config.json here would change NOTHING (the overlay deep-merges over it),
    # and a success response would show the switch flipped while the next load
    # snaps it back.
    if await asyncio.to_thread(overlay_pins, "auto_update"):
        return web.json_response(
            {
                "error": (
                    "auto_update is set in config.local.json, which overrides this "
                    "switch — change or remove it there"
                ),
                "code": "auto_update_overlay_owned",
                "overlay_override": True,
            },
            status=409,
        )

    def _set_auto_update(data: dict) -> dict:
        data["auto_update"] = enabled
        # RETURN it: `update_config_locked` reads a `None` return as "do not write" and
        # exits with the data untouched, so an in-place-only callback silently drops the
        # write while still reporting success.
        return data

    # `update_config_locked` holds the advisory lock across the READ and the write, so no
    # other process can land between them -- that is what stopped the CLI and a second
    # gateway interleaving here.
    #
    # But the flock is only ONE of the two generations that guard config.json. The legacy
    # dashboard writers -- the agents endpoint, the core theme and settings PUTs, security.py,
    # messaging.py, mcp.py, computer_use.py -- do a read-modify-write of this same file
    # while holding ONLY the loop-side `_get_config_lock()`, which the sidecar flock does
    # not exclude. So a theme save landing between this endpoint's read and its write
    # commits from a snapshot taken before it, and silently reverts the auto-update flag
    # the user just toggled -- or this write reverts their theme. Nothing errors and the
    # response still reports success, because it is built from `enabled` rather than from
    # a re-read of what actually landed.
    #
    # `run_config_write` is the one entry point that holds BOTH: it takes the loop-side
    # lock on the event loop and then runs the blocking writer in a worker, so the flock
    # wait still never stalls the loop -- the property the bare `to_thread` was there for,
    # unchanged. Nothing here holds a config lock already, so this introduces no nesting;
    # the handler is a flat coroutine and `run_config_write` is its only lock acquisition.
    #
    # The read still fails CLOSED (`on_corrupt` defaults to "fail"): treating an unreadable
    # config as {} would write back a single-key file and wipe every other setting the user
    # has (see read_config_for_update). `run_config_write` propagates the writer's
    # exceptions unchanged, so that contract is untouched.
    try:
        await run_config_write(update_config_locked, config_path(), mutate=_set_auto_update)
    except ConfigReadError:
        logger.exception("Refusing to toggle auto-update: config is unreadable")
        return web.json_response(
            {"error": "failed to read config file", "code": "config_unreadable"}, status=500
        )
    # The EFFECTIVE value, re-read after the write: ``config.local.json``
    # deep-merges over the file this endpoint wrote, so reporting ``enabled``
    # would promise a state the next load does not produce.
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    overlay_override = await asyncio.to_thread(overlay_pins, "auto_update")
    # Any refresh frame invalidates the SPA's config query, so a second open tab
    # stops showing the old switch position instead of waiting for an unrelated
    # frame to arrive. Best-effort by design: the write is already committed, so
    # a state that cannot broadcast must not turn it into a 500 — the other tab
    # catches up on its next frame.
    with contextlib.suppress(Exception):
        request.app["state"].push_refresh("config")
    return web.json_response(
        {
            "ok": True,
            "auto_update": cfg.auto_update,
            "requested": enabled,
            "overlay_override": overlay_override,
        }
    )


def _changelog_path() -> Path | None:
    """Locate CHANGELOG.md across install layouts.

    1. ``KIROCREW_PROJECT_DIR/CHANGELOG.md`` — dev / git installs.
    2. Bundled ``kiro_crew/CHANGELOG.md`` — pip-wheel installs where
       no source tree is present (copied into the package at build time by
       ``setup.py``'s ``BuildWithFrontend._copy_changelog``).

    Returns the first existing path, or ``None`` if neither is found.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if proj:
        p = Path(proj) / "CHANGELOG.md"
        if p.is_file():
            return p
    # This module lives at kiro_crew/dashboard/handlers/ — parents[2] == kiro_crew/
    bundled = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
    if bundled.is_file():
        return bundled
    return None


#: Cached CHANGELOG.md body, keyed on ``(path, st_mtime_ns, st_size)``.
#: Caching avoids reading and decoding the whole file on every
#: ``GET /api/changelog`` request (the About panel re-fetches on each open,
#: and the file grows with every release). The stat signature keeps a
#: dev-install edit visible immediately, so the endpoint stays live.
_changelog_cache: tuple[tuple[str, int, int], str] | None = None


def _read_changelog() -> str:
    """Return CHANGELOG.md's contents, re-reading only when the file changes."""
    global _changelog_cache
    path = _changelog_path()
    if path is None:
        return ""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return ""
    cached = _changelog_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    _changelog_cache = (key, content)
    return content


async def api_changelog(request: web.Request) -> web.Response:
    """GET /api/changelog — read full CHANGELOG.md from project or bundle.

    The read runs in a thread: ``_read_changelog`` stats the file on every call
    and reads the whole file on a cache miss, which must not block the loop.
    """
    content = await asyncio.to_thread(_read_changelog)
    return web.json_response({"content": content})


async def api_releases(request: web.Request) -> web.Response:
    """GET /api/releases — CHANGELOG.md as per-version entries for the archive.

    The list is the changelog's sections plus the release the running build
    belongs to; :mod:`kiro_crew.changelog` owns that rule and the reasoning.

    ``stale`` is the caveat the page has to state out loud: the changelog is
    read from this install (project tree or the copy bundled into the wheel),
    never from the network, so a prerelease build shows the archive as it stood
    when its release branch was cut. A section added to ``main`` afterwards is
    invisible here, which is a real gap for an insider build that can be weeks
    old -- it is reported rather than hidden.

    The read and the parse are offloaded: ``_read_changelog`` stats the file on
    every call and re-reads it whenever it changed, and the parse is linear in
    the file's size. Both are small today (~10 KB), but the changelog only ever
    grows, and this handler is the one place that adds parsing on top of the
    read -- so it runs in a thread rather than betting the gateway's whole loop
    on the file staying small.
    """

    def _read_and_parse() -> list[Release]:
        return build_release_list(_read_changelog(), _display_local_version())

    releases = await asyncio.to_thread(_read_and_parse)
    return web.json_response(
        {
            "current_version": _display_local_version(),
            "releases": [r._asdict() for r in releases],
            "stale": any(r.in_progress for r in releases),
        }
    )


async def api_update_channel(request: web.Request) -> web.Response:
    """POST /api/update/channel — move this install onto another release channel.

    Only meaningful for a feed-checkable install (a ``cli.sh`` wheel, a cloud
    source install). A git checkout tracks a git remote and a desktop bundle or
    container is updated by something else entirely, so those layouts are
    REFUSED rather than silently writing a file nothing reads — a switcher that
    appears to work and changes nothing is worse than no switcher.

    Switching does not install anything. It changes which feed the next check
    compares against, and which ``--channel`` the recommended installer command
    spells; the user still runs that command (or clicks Update on a
    self-updatable layout). That keeps a channel change from ever being an
    unattended, unconsented version jump.

    **Not a governance bypass.** A channel the policy requires (``updates.channel``,
    or ``stable`` in an enterprise build with no pin; ``update_layout``
    ``policy_release_channel``) wins on read, so a switch to any other channel is
    refused below with ``locked_by_policy`` rather than stored. When the pins
    define a command provider the endpoint refuses outright — the provider's
    commands never read the channel file, so a "successful" switch would be the
    lying switcher described above. Nor does the endpoint grant a new capability:
    the channel file is an ordinary file in the data home that the operator can
    already write. What it adds is an authenticated, allowlist-validated path to
    the same write.
    """
    owner_denied = await require_owner_dashboard_request(request, "update.channel")
    if owner_denied is not None:
        return owner_denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)

    # A well-formed JSON ARRAY or scalar parses fine and then has no ``.get``, so
    # the type check is separate from the parse guard above: without it, a body of
    # ``[]`` from an authenticated caller raises AttributeError and answers 500
    # where the honest answer is 400.
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )

    requested = body.get("channel")
    if not isinstance(requested, str):
        return web.json_response(
            {"error": "channel must be a string", "code": "invalid_channel"}, status=400
        )

    # A policy-defined provider OWNS updates on this host, and its commands do
    # not read the channel file — the switch would "succeed" and change nothing,
    # which is exactly the lying switcher the layout refusals below exist to
    # prevent. Checked before the layout probe because it needs no git shell-out.
    if resolve_provider() is not None:
        return web.json_response(
            {
                "error": "Updates on this install are managed by policy, not a release channel.",
                "code": "channel_not_applicable_command_managed",
            },
            status=409,
        )

    # Offloaded: the layout derivation shells out to git, and this handler runs on
    # the event loop — a synchronous probe here freezes every gateway task and the
    # liveness heartbeat for as long as git takes to answer.
    layout = await asyncio.to_thread(detect_install_layout)
    if layout.is_git:
        return web.json_response(
            {
                "error": "A git checkout follows its git remote, not a release channel.",
                "code": "channel_not_applicable_git",
            },
            status=409,
        )
    if layout.is_externally_managed:
        return web.json_response(
            {"error": layout.guidance, "code": "channel_not_applicable_managed"},
            status=409,
        )

    # The policy's channel (stable in an enterprise build with no pin) wins on
    # read, so a file write naming another channel would be ignored.
    from kiro_crew.edition_locks import LOCKED_BY_POLICY
    from kiro_crew.platform.update_layout import policy_release_channel

    required = await asyncio.to_thread(policy_release_channel)
    if required and requested.strip().lower() != required:
        await _audit_update_event(
            request,
            operation="update.channel",
            outcome="denied",
            error="locked_by_policy",
            resources=requested[:32],
        )
        return web.json_response(
            {"error": LOCKED_BY_POLICY, "code": "locked_by_policy", "channel": required},
            status=409,
        )

    try:
        # Offloaded: mkdir + write + os.replace are synchronous syscalls, and the
        # data home can be network-backed (NFS/SMB), where even a ten-byte write
        # can stall long enough to freeze the loop and the liveness heartbeat with
        # it. Small does not mean non-blocking.
        stored = await asyncio.to_thread(set_release_channel, requested)
    except ValueError:
        # The allowlist is the whole guard: `channel` becomes a path segment in
        # every feed URL and an argument in a shell command, so an unknown value
        # is rejected, never coerced.
        return web.json_response(
            {"error": "unknown release channel", "code": "invalid_channel"}, status=400
        )
    except OSError:
        logger.exception("Failed to persist release channel")
        return web.json_response(
            {"error": "failed to write channel file", "code": "channel_write_failed"}, status=500
        )

    # Re-check immediately against the NEW feed. Without this the panel would
    # keep showing the previous channel's verdict until the next 12-hourly poll,
    # which reads as the switch having done nothing. `stored` is what the write
    # above just validated and persisted, so the invalidation needs no read of
    # its own.
    _invalidate_update_check(stored)
    await _do_update_check()
    # Same reason as the write above: a config read is disk I/O on a path the
    # operator may have put on a network mount.
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    return web.json_response(
        {
            "ok": True,
            **_update_info,
            "auto_update": cfg.auto_update,
            "overlay_override": await asyncio.to_thread(overlay_pins, "auto_update"),
            # AFTER the spread, deliberately. When a check was already in flight
            # `_do_update_check` returns early and the cache still holds the
            # invalidated ``channel: ""`` / ``update_command: ""``; letting those
            # win would blank the switcher right after a successful switch, and --
            # worse -- leave the client falling back to the PREVIOUS channel's
            # command, so copy-pasting it would move the install straight back.
            #
            # Neither value needs the check: both are pure functions of the channel
            # now on disk, so they are composed locally from the already-validated
            # name (same helper, same https pin as the check's own path).
            "channel": stored,
            "update_command": wheel_update_command(stored),
            # Same display-only sibling as api_update_check: this response IS
            # the re-run check the panel adopts wholesale, so it must carry the
            # folded version too or a channel switch would blank the clean
            # display back to the raw stamp.
            "latest_version_display": _display_version(
                str(_update_info.get("latest_version") or ""), stored
            ),
        }
    )


async def api_update_revalidate(request: web.Request) -> web.Response:
    """POST /api/update/revalidate — drop the cached verdict and re-check now.

    A terminal ``kirocrew update`` on a git checkout moves the tree while THIS
    gateway keeps running, so its cached ``_update_info`` verdict still describes
    the pre-update HEAD. The About panel keeps showing "Update available" for a
    checkout that is now current until the 12-hourly poll, a manual check, or a
    restart. This endpoint lets the CLI reconcile the badge the moment the update
    finishes.

    It does MORE than the recompute ``GET /api/update/check`` already performs:
    it first calls :func:`_invalidate_update_check`, which bumps the check
    GENERATION. That is the load-bearing half — an update check ALREADY in flight
    against the pre-update state (the 12-hourly coordinator, or a dashboard poll
    that overlapped the update) cannot be cancelled, and without the generation
    bump it finishes after the recompute and re-pins its stale verdict plus the
    12-hourly clock. A bare re-check cannot close that race; the invalidation can.

    Authenticated like the other CLI→gateway endpoints (``/api/token/local``,
    ``/api/logout``): loopback origin plus the per-generation local secret in
    ``X-Local-Secret``, compared in constant time. This is a CLI-only endpoint —
    the dashboard panel reconciles through ``GET /api/update/check`` — so it does
    NOT use the browser owner gate, whose identity the raw local-secret request
    never carries. A kernel-attested same-user AF_UNIX peer
    (``core._unix_peer_is_self``) is admitted beside loopback, because the CLI's
    owner-verified send prefers the dashboard's owner-only socket, where
    ``request.remote`` is empty; the secret check is unchanged on both.
    """
    from kiro_crew.dashboard.handlers.core import _unix_peer_is_self

    if not is_loopback(request.remote or "") and not _unix_peer_is_self(request):
        await _audit_update_event(
            request, operation="update.revalidate", outcome="denied", resources="non-loopback"
        )
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    expected = request.app.get("local_secret", "")
    provided = request.headers.get("X-Local-Secret", "")
    # Compare as bytes: hmac.compare_digest raises TypeError on a str carrying a
    # non-ASCII character, and this header is attacker-controllable on the
    # tokenless bypass path, so a str compare would turn an auditable 403 into an
    # unaudited 500. Encoding both sides makes a non-ASCII secret an ordinary
    # constant-time mismatch instead. ``surrogatepass`` because aiohttp decodes a
    # header value with ``surrogateescape``, so a non-UTF-8 byte arrives as a lone
    # surrogate, which a strict encode refuses by raising -- the same 500.
    if (
        not expected
        or not provided
        or not hmac.compare_digest(
            str(expected).encode("utf-8", "surrogatepass"),
            provided.encode("utf-8", "surrogatepass"),
        )
    ):
        await _audit_update_event(
            request, operation="update.revalidate", outcome="denied", resources="invalid-secret"
        )
        return web.json_response({"error": "invalid secret", "code": "invalid_secret"}, status=403)

    # A config read is disk I/O on a path the operator may have put on a network
    # mount, so keep the reads off the event loop for the same reason the channel
    # switch does.
    channel = await asyncio.to_thread(_release_channel)
    _invalidate_update_check(channel)
    await _do_update_check()
    await _audit_update_event(request, operation="update.revalidate", outcome="completed")
    # The CLI reads only the HTTP status, so the body carries nothing more.
    return web.json_response({"ok": True})
