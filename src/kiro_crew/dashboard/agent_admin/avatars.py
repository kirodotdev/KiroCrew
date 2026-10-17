"""Per-crew uploaded avatars: the image store under the data home (stage, promote, commit, roll back, remove), the motion carry-through for saves that omit them, and ``POST /api/agents/{name}/avatar``."""

from __future__ import annotations

import asyncio
import hashlib
import stat
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import BodyPartReader, web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        _AVATAR_FILE_PIN_RE,
        _AVATAR_GHOST_BOOL_TRAITS,
        _AVATAR_GHOST_STR_TRAITS,
        _AVATAR_IMAGE_EXTS,
        _AVATAR_MAX_BYTES,
        KiroCrewConfig,
        _drained_to_thread,
        _get_config_lock,
        _require_owner,
        _safe_color,
        _sel,
        data_home,
        logger,
        replace_with_retry,
    )


def _carry_motions_through_motionless_save(stored: dict, raw: object, validated: dict) -> dict:
    """Keep a ghost's stored ``motions`` when a ghost save says nothing about them.

    ``motions`` is the one reaction key the shipped crew editor does not know:
    it rebuilds a ghost draft from the axes it can draw (``traits``,
    ``expressions``, ``sounds``) and submits exactly those, so a ghost that was
    given motions through the API would lose them on the next unrelated save --
    a model change, a colour -- with no click that meant it. A save from a
    client that cannot see a value is not a decision about it.

    The rule is the tri-state ``save_pack`` already applies to a pack's cues.
    A payload with NO ``motions`` key leaves the stored ones alone; a payload
    that names the key -- ``{}`` included -- is the caller's statement and
    replaces them. So a client that knows the key can still clear it, and one
    that does not cannot destroy it.

    Deliberately narrow: only when the stored record AND the validated save are
    both ghosts. A tier change (picture, pack) is a real face replacing the old
    one and ``motions`` is the ghost's alone; a reset (``None``, ``{}``, or the
    validator's all-empty collapse) means reset. Both keep their existing
    semantics untouched.
    """
    if stored.get("kind") != "ghost" or validated.get("kind") != "ghost":
        return validated
    kept = stored.get("motions")
    if not isinstance(kept, dict) or not kept:
        return validated
    if not isinstance(raw, dict) or "motions" in raw:
        return validated
    return {**validated, "motions": kept}


def _is_ghost_shaped(value: object) -> bool:
    """True when ``value`` is a structurally well-formed ghost override
    whose trait values all carry their schema types.

    Tells the validator's all-empty→reset collapse apart from caller
    junk at the 400 gate. Structure alone is not enough: the validator
    coerces a wrong-TYPE trait value (``{"eyes": 7}``) to absent, so a
    malformed payload would collapse to reset and — when the crew currently
    wears an uploaded picture — silently delete it. A payload only earns the
    reset collapse when every trait it names is validly typed (string axes
    are strings, boolean axes are real booleans), i.e. it is genuinely
    empty, not mistyped.
    """
    if not (
        isinstance(value, dict)
        and value.get("kind") == "ghost"
        and isinstance(value.get("traits"), dict)
    ):
        return False
    traits = value["traits"]
    known = set(_AVATAR_GHOST_STR_TRAITS) | set(_AVATAR_GHOST_BOOL_TRAITS) | {"tile"}
    if any(k not in known for k in traits):
        # An unknown axis name is a typo'd or version-skewed caller, not an
        # empty override — it must not earn the reset collapse.
        return False
    for key in _AVATAR_GHOST_STR_TRAITS:
        if key in traits and not isinstance(traits[key], str):
            return False
    for key in _AVATAR_GHOST_BOOL_TRAITS:
        if key in traits and not isinstance(traits[key], bool):
            return False
    tile = traits.get("tile", "")
    if not isinstance(tile, str):
        return False
    if tile and not _safe_color(tile):
        # A nonempty tile the color validator coerces to absent is junk,
        # not an intentionally empty axis.
        return False
    return True


def _avatars_dir() -> Path:
    """Uploaded-avatar directory, resolved against the live data home.

    Lives under ``run/`` — the data-home subtree the security layer fences
    from agent file tools (read AND write) — because the config's ``file``
    pin only proves which path was committed, not what is inside it: an
    agent that could write the pinned path would have its bytes served to
    the owner's authenticated dashboard as the saved picture. The gateway's
    own handlers open these paths directly in-process and do not route
    through that gate, so upload/serve/reap all work unchanged.

    Resolved per call, never captured at import — an import-time binding
    freezes the data home and defeats pod isolation and test isolation
    (dashboard/handlers/files.py is the precedent).
    """
    return data_home() / "run" / "avatars"


def _avatar_stem(name: str) -> str:
    """Path-safe filename stem for a crew's avatar.

    Crew names are display strings (spaces, CJK, anything) — a digest
    sidesteps every path-traversal and encoding question rather than
    answering them one by one. Full digest: truncating buys nothing and a
    shorter stem is the only thing a collision would need.
    """
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _avatar_variant_paths(name: str) -> list[Path]:
    """Every digest-named stored variant of ``name``'s picture on disk."""
    stem = _avatar_stem(name)
    d = _avatars_dir()
    out: list[Path] = []
    for p in d.glob(f"{stem}.*"):
        suffix = p.name[len(stem) + 1 :]
        if _AVATAR_FILE_PIN_RE.fullmatch(suffix) and p.is_file():
            out.append(p)
    return sorted(out)


def _pending_avatar_path(name: str) -> Path | None:
    """Return the STAGED (uploaded, not yet committed) file, or None."""
    stem = _avatar_stem(name)
    for ext in _AVATAR_IMAGE_EXTS:
        p = _avatars_dir() / f"{stem}.pending.{ext}"
        if p.is_file():
            return p
    return None


def _remove_avatar_files(name: str) -> None:
    """Delete every stored variant (installed + staged) of ``name``'s
    avatar, best-effort — a failed unlink is logged, never raised, because
    both callers (saving ``avatar: {}`` and crew deletion) have already
    cleared the config field, so the file is unreachable either way.
    """
    stem = _avatar_stem(name)
    for ext in _AVATAR_IMAGE_EXTS:
        try:
            (_avatars_dir() / f"{stem}.pending.{ext}").unlink(missing_ok=True)
        except OSError:
            logger.debug("could not remove staged avatar for %s (.%s)", name, ext)
    for p in _avatar_variant_paths(name):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            logger.debug("could not remove avatar file %s for %s", p.name, name)


def _discard_pending_avatar(name: str) -> None:
    """Remove any staged-but-uncommitted upload (best-effort)."""
    stem = _avatar_stem(name)
    for ext in _AVATAR_IMAGE_EXTS:
        try:
            (_avatars_dir() / f"{stem}.pending.{ext}").unlink(missing_ok=True)
        except OSError:
            logger.debug("could not discard pending avatar for %s (.%s)", name, ext)


def _read_avatar_file(path: Path) -> bytes | None:
    """Bounded, symlink-refusing read of a stored avatar file.

    The avatars dir sits behind the ``run/`` agent fence, but a stored file
    is still not trusted just because it is where an upload would have
    landed (defense in depth): a
    planted symlink must not let the authenticated GET read an arbitrary
    file, and a planted oversized blob must not be slurped unbounded into
    gateway memory. ``None`` means "treat as absent".
    """
    try:
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            logger.warning("refusing non-regular avatar file %s", path.name)
            return None
        with path.open("rb") as fh:
            data = fh.read(_AVATAR_MAX_BYTES + 1)
    except OSError:
        return None
    if len(data) > _AVATAR_MAX_BYTES:
        logger.warning("refusing oversized avatar file %s", path.name)
        return None
    return data


def _staging_token(data: bytes) -> str:
    """Identity of a staged upload: a content digest the PUT must echo.

    Staging is keyed by crew, so overlapping saves share the slot; the token
    is what stops save A's commit from promoting save B's bytes.
    """
    return hashlib.sha256(data).hexdigest()[:16]


def _promote_pending_avatar(name: str, token: str) -> tuple[int, str] | None:
    """Install the staged upload at its digest-named path; return
    ``(cache stamp, file pin)``.

    Installs only when the staged bytes match ``token`` (the digest the
    upload response handed THIS save) — a slot overwritten by a newer save's
    staging returns None instead of committing someone else's bytes. The
    install target is ``<stem>.<token>.<ext>``: content-addressed, so it can
    never collide with (or overwrite) the currently committed file — a
    process kill anywhere before the config save leaves the committed
    picture byte-identical at its own pinned path, and the orphaned install
    is reaped by the next successful commit. The caller MUST follow with
    :func:`_commit_promoted_avatar` (save succeeded) or
    :func:`_rollback_promoted_avatar` (save failed). Runs synchronous
    filesystem work: call via ``asyncio.to_thread``.
    """
    pending = _pending_avatar_path(name)
    if pending is None:
        return None
    staged = _read_avatar_file(pending)
    if staged is None or _staging_token(staged) != token:
        return None
    stem = _avatar_stem(name)
    d = _avatars_dir()
    pin = f"{token}{pending.suffix}"
    final = d / f"{stem}.{pin}"
    # replace_with_retry rides out the Windows sharing-violation window an
    # AV scanner or indexer opens on either path. Re-uploading bytes already
    # committed lands on the same digest path with identical content.
    replace_with_retry(pending, final)
    # Best-effort: a scanner holding a pending sibling open must not fail a
    # promotion whose install already landed.
    for other in _AVATAR_IMAGE_EXTS:
        try:
            (d / f"{stem}.pending.{other}").unlink(missing_ok=True)
        except OSError:
            logger.debug("could not remove staged avatar for %s (.%s)", name, other)
    # Nanosecond mtime: a same-size same-second replacement must still get a
    # fresh ?v= or the browser keeps showing the old bytes.
    return int(final.stat().st_mtime_ns), pin


def _commit_promoted_avatar(name: str, keep_pin: str) -> None:
    """After the config save succeeded: reap every variant except the one
    the config now pins — the previous picture and any orphaned installs.
    """
    for p in _avatar_variant_paths(name):
        if p.name[len(_avatar_stem(name)) + 1 :] == keep_pin:
            continue
        try:
            p.unlink(missing_ok=True)
        except OSError:
            logger.debug("could not reap avatar variant %s for %s", p.name, name)


def _rollback_promoted_avatar(name: str, installed_pin: str, keep_pin: object) -> None:
    """Undo an install whose config save failed: remove the installed file.

    The committed picture was never touched — installs are content-addressed
    — so rollback is a single unlink, skipped when the install landed on the
    committed pin itself (a re-upload of identical bytes).
    """
    if installed_pin == keep_pin:
        return
    try:
        (_avatars_dir() / f"{_avatar_stem(name)}.{installed_pin}").unlink(missing_ok=True)
    except OSError:
        logger.debug("could not remove installed avatar %s for %s", installed_pin, name)


def _live_avatar_file(name: str, pin: object) -> Path | None:
    """The avatar file the config's ``file`` pin selects, or None.

    Only the exact pinned file counts. There is deliberately NO fallback for a
    record without a valid pin (a hand-edited ``{"kind": "image"}``): every
    writer stamps ``file`` at the commit, so a pinless record never names a
    committed picture, and "any stored variant" would include an orphaned
    install left by a crash between the install and the config save — the
    one file this pin exists to keep out of the roster. A pinless record
    therefore serves nothing (the frontend falls back to the seeded ghost)
    and a picture-keeping save on it fails with ``avatar_file_missing``
    rather than adopting an unknown file.
    """
    if isinstance(pin, str) and _AVATAR_FILE_PIN_RE.fullmatch(pin):
        p = _avatars_dir() / f"{_avatar_stem(name)}.{pin}"
        return p if p.is_file() else None
    return None


def _sniff_image_ext(head: bytes) -> str:
    """Return the format of ``head`` by magic bytes, or ``""``.

    PNG / JPEG / WEBP only — the formats every target browser renders in an
    ``<img>`` and none of which can carry active content the way SVG can.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return ""


def _image_body_complete(ext: str, body: bytes) -> bool:
    """Whether ``body`` is a structurally complete image of format ``ext``.

    Magic bytes alone accept a body cut off mid-stream (a client that died
    mid-upload, or a hand-built request), and committing one would reap the
    crew's previous picture in exchange for a file no browser can decode. A
    full decoder is not a dependency of this package, so this checks the one
    property every truncation breaks — that the container is closed:

    - PNG: the stream ends with the ``IEND`` chunk (its 4-byte CRC is fixed).
    - JPEG: the stream ends with the ``FFD9`` end-of-image marker.
    - WEBP: the RIFF header's declared payload length matches the body.

    Trailing padding after the terminator is not tolerated either: an
    ``<img>`` renders it fine, but it is exactly the shape a smuggled payload
    takes, and no encoder this endpoint accepts pictures from emits it.
    """
    if ext == "png":
        return body.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
    if ext == "jpg":
        return body.endswith(b"\xff\xd9")
    if ext == "webp":
        if len(body) < 12:
            return False
        declared = int.from_bytes(body[4:8], "little")
        # RIFF length counts everything after the 8-byte RIFF header. A
        # single pad byte is legal when the payload length is odd.
        return declared + 8 in (len(body), len(body) - 1)
    return False


async def api_kirocrew_agent_avatar_upload(request: web.Request) -> web.Response:
    """POST /api/agents/{name}/avatar — STAGE the crew's picture (multipart).

    Staging only: the file lands as ``<stem>.pending.<ext>`` and nothing the
    roster serves changes. The commit point is the ordinary agent update
    (`PUT /api/agents/{name}` with ``avatar: {"kind": "image"}``), which
    promotes the staged file and writes the field under one config lock —
    so a failed or abandoned Save can never have replaced the live picture,
    and the editor's Apply→Save two-step holds for images exactly as it
    does for ghost traits.
    """
    denied = await _require_owner(request, "agent.avatar_upload")
    if denied is not None:
        return denied
    name = request.match_info["name"]
    if not (request.content_type or "").startswith("multipart/"):
        return web.json_response(
            {"error": "expected multipart/form-data", "code": "not_multipart"}, status=400
        )
    data = bytearray()
    try:
        reader = await request.multipart()
        part = await reader.next()
        # `next()` may yield a nested MultipartReader (multipart/mixed); only
        # a concrete body part carries a file, so anything else is skipped.
        while part is not None and (not isinstance(part, BodyPartReader) or part.name != "file"):
            part = await reader.next()
        if part is None:
            return web.json_response(
                {"error": "missing 'file' part", "code": "missing_file_part"}, status=400
            )
        while True:
            chunk = await part.read_chunk(64 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > _AVATAR_MAX_BYTES:
                return web.json_response(
                    {
                        "error": f"avatar exceeds {_AVATAR_MAX_BYTES // 1024} KB limit",
                        "code": "avatar_too_large",
                    },
                    status=413,
                )
    except (ValueError, AssertionError):
        # aiohttp raises plain ValueError for a bad/missing boundary or a
        # body truncated mid-part; that is caller junk, not a server error.
        return web.json_response(
            {"error": "malformed multipart body", "code": "invalid_multipart"}, status=400
        )
    ext = _sniff_image_ext(bytes(data[:16]))
    if not ext:
        return web.json_response(
            {
                "error": "avatar must be a PNG, JPEG, or WEBP image",
                "code": "avatar_bad_format",
            },
            status=400,
        )
    # Valid magic bytes on a truncated body must not stage: promotion would
    # reap the committed picture and serve an undecodable file in its place.
    if not _image_body_complete(ext, bytes(data)):
        return web.json_response(
            {
                "error": "avatar image is truncated or malformed — re-export and upload again",
                "code": "avatar_bad_format",
            },
            status=400,
        )

    def _stage() -> None:
        d = _avatars_dir()
        d.mkdir(parents=True, exist_ok=True)
        staged = d / f"{_avatar_stem(name)}.pending.{ext}"
        # Atomic even for the staging file: a crash mid-write must not leave
        # a truncated body a later promote would install. replace_with_retry
        # rides out the Windows sharing-violation window an AV scanner or
        # indexer opens on either path.
        tmp = staged.with_suffix(f".{ext}.tmp-{uuid.uuid4().hex[:8]}")
        try:
            tmp.write_bytes(bytes(data))
            replace_with_retry(tmp, staged)
        finally:
            tmp.unlink(missing_ok=True)
        # A re-pick with a different format supersedes the previous staging.
        # Best-effort: a scanner holding a stale sibling open must not fail
        # the upload that already staged its bytes.
        for other in _AVATAR_IMAGE_EXTS:
            if other != ext:
                try:
                    (d / f"{_avatar_stem(name)}.pending.{other}").unlink(missing_ok=True)
                except OSError:
                    logger.debug("could not remove stale staging for %s (.%s)", name, other)

    # Staging happens under the config lock, with the crew's existence
    # re-checked inside it: an upload racing a crew deletion must not write
    # an orphan file the deletion's cleanup already missed. The multipart
    # body was fully read above, so the lock is held only for the short
    # filesystem commit.
    async with _get_config_lock():
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if name not in cfg.agents:
            return web.json_response(
                {"error": f"Agent '{name}' not found", "code": "agent_not_found"},
                status=404,
            )
        await _drained_to_thread(_stage)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.avatar_upload",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "staged": True, "token": _staging_token(bytes(data))})
