"""``GET /api/agent-panel/dashboard/snapshot`` -- a picture of the caller's own page.

A crewmate writes its Dashboard page and cannot look at it. The save's page run already
loads a page in a headless browser on the gateway host; this route runs the page IN FORCE
the same way, over the crewmate's REAL read, and takes a PNG of the first paint, so the
crewmate can judge both what its page says and how it looks.

Which page, and whose
---------------------
The page in force only: the package this crewmate applied, read from the gateway's pinned
snapshot exactly as the tab reads it (``instance.pinned_package``). The crewmate comes from
:func:`_resolve_dashboard_caller`, the gate every route under this prefix asks, and never
from the request, and the package's own ``bound_to`` must name that crewmate. A template
page, a crewmate with no applied page, a root session and a package bound elsewhere have
no picture, and the answer says which. A page that is only SAVED is never run over the
record: it has not been applied, and a page nobody applied is never given a value.

What the page is given
----------------------
The read the tab is served, through the tab's own redaction
(``member_dashboard_fields.snapshot_package_page``), so the picture shows what the
person's tab shows. Nothing else changes about the run: no network, one check at a time
across the gateway, a hard time limit, and a host that is busy, short of memory or has no
browser answers ``no_image`` with the reason. A run that drew nothing worth showing (timed
out, crashed, stopped at a CDN library the offline run does not fetch) answers
``no_image`` too, so no earlier picture is ever offered as the page as it is now.

Where the picture goes
----------------------
The PNG carries the crewmate's real values, so it never leaves the data home and is
never served: the gateway writes it to
``<data home>/dashboard-snapshots/<crewmate>/page-<ms>-<random>.png`` with mode ``0600``
under ``0700`` directories, keeps the newest :data:`SHOT_KEEP` per crewmate, and returns
the PATH. No route serves that directory (the file explorer's data-home allowlist does
not name it) and the PNG is never put in a response body. The directory chain is opened
one descriptor at a time with ``O_NOFOLLOW`` and the file is created ``O_EXCL``
(``pinned_fs``), because an agent can write inside the data home and a link planted there
must not steer the gateway's write. Only the crewmate's own session is told the path. Like
the crew logs its values come from, the file is not fenced at the OS level from another
process running as the same user.

Otherwise READ ONLY: nothing on the page or its versions is written, staged or recorded,
and nothing is remembered between calls.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Final

from aiohttp import web

from kiro_crew.artifact_store import dashboard_package as _dashboard
from kiro_crew.dashboard.handlers.agent_panel_dashboard import _resolve_dashboard_caller

logger = logging.getLogger("kiro_crew.dashboard.handlers.agent_panel")

STATUS_IMAGE: Final[str] = "image"
STATUS_NO_IMAGE: Final[str] = "no_image"

#: The data-home leaf that holds every crewmate's pictures, one directory each.
SHOT_ROOT: Final[str] = "dashboard-snapshots"
SHOT_FILE_PREFIX: Final[str] = "page-"
SHOT_FILE_SUFFIX: Final[str] = ".png"
#: Pictures kept per crewmate. A cache, so a long session cannot fill the disk.
SHOT_KEEP: Final[int] = 8
SHOT_FILE_MODE: Final[int] = 0o600

#: Said when the page in force is not one the crewmate composed.
NOT_COMPOSED: Final[str] = (
    "the page in force is not one you composed and applied (a template page, or no "
    "page), and a snapshot shows only your own applied page"
)

#: The runner's busy reason, worded for a snapshot rather than a save.
BUSY: Final[str] = (
    "busy: another page check is running, so this one was not started; ask for the "
    "snapshot again in a moment"
)


class _NoPage(Exception):
    """There is no page of the caller's own to draw; the message says why."""


def _page_in_force(slug: str) -> tuple[dict[str, Any], str, int]:
    """``(package, package slug, version)`` of the caller's applied page, or
    :class:`_NoPage`.

    The tab's own answer (``member_dashboard._serve``): a record that pins a package is
    drawn from the gateway's snapshot, and an older unlocked record written before pins
    existed (``instance.keeps_older_package_rule``) keeps the rule of the package bound
    to *slug*, at its newest version a person cut (:func:`_older_page`)."""
    from kiro_crew.dashboard_templates import instance as instance_store

    record = instance_store.read(slug)
    pinned = instance_store.pinned_package(record)
    if pinned is not None:
        package_slug, version, content = str(pinned.slug), int(pinned.version), pinned.content
    elif getattr(record, "package_slug", ""):
        raise _NoPage("the page in force could not be read back from its pinned copy")
    elif instance_store.keeps_older_package_rule(record):
        package_slug, version, content = _older_page(slug)
    else:
        raise _NoPage(NOT_COMPOSED)
    package = _dashboard.parse_package(_dashboard.canonical_package_content(content))
    if package.get("bound_to") != f"crewmate:{slug}":
        raise _NoPage("the page in force is not bound to you, so it is not drawn")
    return package, package_slug, version


def _older_page(slug: str) -> tuple[str, int, str]:
    """``(package slug, version, content)`` an older unpinned record draws, or
    :class:`_NoPage`: the tab's own lookups, so the two cannot disagree. The binding
    comes from ``dashboard_agentic.bound_package``, the one lookup the tab and the turn
    block share."""
    from kiro_crew import dashboard_agentic
    from kiro_crew.artifacts import get_default_store
    from kiro_crew.dashboard.handlers import member_dashboard

    package_slug = dashboard_agentic.bound_package(slug)
    if not package_slug or not dashboard_agentic.has_person_cut_version(package_slug):
        raise _NoPage(NOT_COMPOSED)
    store = get_default_store()
    loaded = store.get(package_slug)
    # The tab's own refusals (`_serve_package`): another kind, or a linked file whose
    # bytes skip the write gate, is not a page to draw.
    if str(getattr(loaded, "kind", "")) != _dashboard.DASHBOARD_KIND or (
        str(getattr(loaded, "source_path", "") or "")
        and not bool(getattr(loaded, "source_copy_only", False))
    ):
        raise _NoPage("the page in force is not a dashboard package that can be drawn")
    version = member_dashboard.applied_package_version(loaded)
    if version <= 0:
        raise _NoPage(NOT_COMPOSED)
    applied = store.get(package_slug, version=version)
    if member_dashboard._record_identity(applied) != member_dashboard._record_identity(loaded):
        raise _NoPage("the page in force was replaced while it was read; ask again")
    return package_slug, version, applied.content or ""


def _shot_root() -> Path:
    """The data home, resolved, as the root of the pinned walk. A seam for tests."""
    from kiro_crew.config.paths import data_home

    return Path(os.path.realpath(data_home()))


def _now_ms() -> int:
    """The wall clock in milliseconds, which names a picture by age. A seam for tests."""
    return int(time.time() * 1000)


def spool_png(slug: str, png: bytes) -> str:
    """Write *png* as a fresh ``0600`` file in *slug*'s picture directory; its path.

    Raises :class:`OSError` (or a pinned-path refusal) when the picture cannot be written
    safely, which the caller answers as ``no_image``.
    """
    from kiro_crew.pinned_fs import open_pinned_descendant_dir, write_new_file_pinned

    if not slug or slug in (".", "..") or os.sep in slug or (os.altsep and os.altsep in slug):
        raise OSError(f"{slug!r} cannot name a picture directory")
    root = _shot_root()
    name = f"{SHOT_FILE_PREFIX}{_now_ms()}-{secrets.token_hex(4)}{SHOT_FILE_SUFFIX}"
    with open_pinned_descendant_dir(
        root, (SHOT_ROOT, slug), what="dashboard snapshot directory", create=True
    ) as dir_fd:
        if dir_fd is None:
            # Windows: no pinned walk, so nothing below runs there, os.fchmod included.
            raise OSError("this platform cannot write the picture through a pinned directory")
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 is owner-only, the tightest traversable dir mode; 0o644 would add world-read and drop traverse.  # noqa: E501
        os.fchmod(dir_fd, 0o700)
        write_new_file_pinned(dir_fd, name, png, mode=SHOT_FILE_MODE)
        _trim(dir_fd, keep=name)
    return str(root / SHOT_ROOT / slug / name)


def _trim(dir_fd: int, *, keep: str) -> None:
    """Keep at most :data:`SHOT_KEEP` pictures, *keep* always among them, then the newest.

    *keep* is the picture just written, whose path the caller is about to hand back: a
    clock that stepped back gives it the oldest name, so it is never a candidate.
    Best-effort: never fails a call."""
    try:
        names = [
            n
            for n in os.listdir(dir_fd)
            if n.startswith(SHOT_FILE_PREFIX) and n.endswith(SHOT_FILE_SUFFIX) and n != keep
        ]
    except OSError:
        return
    # The name carries a millisecond stamp first, so name order is age order. An unlink
    # never follows its final component, and it is relative to the pinned directory.
    for old in sorted(names)[: max(0, len(names) - (SHOT_KEEP - 1))]:
        try:
            os.unlink(old, dir_fd=dir_fd)
        except OSError:
            logger.debug("dashboard snapshot trim skipped %s", old, exc_info=True)


def _no_image(reason: str, verification: Any = None) -> web.Response:
    return web.json_response(
        {
            "snapshot": {"status": STATUS_NO_IMAGE, "reason": reason},
            "verification": verification,
        }
    )


async def _await_run(work: Any) -> Any:
    """Wait for the run at most as long as a save waits for its check."""
    from kiro_crew import dashboard_verify

    return await asyncio.wait_for(work, timeout=dashboard_verify.REPLY_SECONDS)


async def api_dashboard_snapshot(request: web.Request) -> web.Response:
    """GET /api/agent-panel/dashboard/snapshot -- a PNG of your own page in force.

    See the module docstring. ``snapshot.status`` is ``image`` with ``path``, ``width``,
    ``height``, ``bytes``, ``package_version`` and ``data: "record"``, or ``no_image``
    with a ``reason``; ``verification`` is the run's own block whenever a run started.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_snapshot")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, crew_name, slot = resolved
    if request.query:
        return web.json_response(
            {"error": "dashboard_snapshot takes no arguments", "code": "validation_error"},
            status=400,
        )
    if not crew_name:
        return _no_image(
            "a root session's Dashboard is a template page, and only a crewmate's own page "
            "is drawn"
        )
    try:
        package, package_slug, version = await asyncio.to_thread(_page_in_force, slug)
    except _NoPage as exc:
        return _no_image(str(exc))
    except Exception:
        logger.warning("dashboard snapshot: %s's page could not be read", slug, exc_info=True)
        return _no_image("your page in force could not be read")

    from kiro_crew import dashboard_verify
    from kiro_crew.dashboard.handlers import member_dashboard_fields

    deadline = time.monotonic() + dashboard_verify.RUN_TIMEOUT_SECONDS
    try:
        verification, image = await _await_run(
            asyncio.to_thread(
                member_dashboard_fields.snapshot_package_page,
                slug,
                crew_name,
                slot,
                package,
                deadline,
                package_slug,
                version,
                request.app.get("state"),
            )
        )
    except TimeoutError:
        verification, image = dashboard_verify.timed_out_wire(), None
    if verification is None:
        return _no_image("the page is a block layout, which has no page of its own to draw")
    if image is None or not image.png:
        reason = getattr(image, "reason", "") or str(verification.get("reason") or "")
        if reason == dashboard_verify._BUSY:
            # The runner's words name a save; here nothing was saved.
            reason = BUSY
        return _no_image(reason or "the page was not drawn", verification)
    try:
        path = await asyncio.to_thread(spool_png, slug, image.png)
    except Exception:
        logger.warning("dashboard snapshot: %s's picture was not written", slug, exc_info=True)
        return _no_image("the picture could not be written to a file", verification)
    return web.json_response(
        {
            "snapshot": {
                "status": STATUS_IMAGE,
                "path": path,
                "package_version": version,
                "width": image.width,
                "height": image.height,
                "bytes": len(image.png),
                "data": dashboard_verify.DATA_RECORD,
            },
            "verification": verification,
        }
    )
