"""Persisted artifact records: ``meta.json``, the lifecycle event log and ``comments.json``.

What the store writes and how it reads it back. Loading is tolerant by contract:
unknown keys are ignored, missing keys take their defaults, and a malformed nested
block (``publication``, ``fork_metadata``, ``image``) degrades to its safe value
instead of failing the load, so a schema added later never breaks a record written
earlier. :class:`kiro_crew.artifacts.ArtifactStore` owns the files, the lock and the
fence; this module owns only their contents.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import fields as fields_of
from pathlib import Path
from typing import Any
from typing import List as _List

from kiro_crew.artifact_store.model import (
    Artifact,
    ArtifactComment,
    ArtifactError,
    ArtifactPublication,
    ArtifactValidationError,
    ForkMetadata,
    ImageMetadata,
)
from kiro_crew.deploy.webapp_types import webapp_metadata_from_dict
from kiro_crew.publish_provider import DEFAULT_PROVIDER

logger = logging.getLogger(__name__)

#: Shape of a persisted ``content_salt``: 128 random bits as lowercase hex.
_CONTENT_SALT_RE = re.compile(r"[0-9a-f]{32}")

#: Allowed lifecycle event types. ``referenced`` records a chat impression of the
#: artifact; the in-line save/update path emits ``created`` / ``edited`` /
#: ``iterated`` / ``reverted``, and comment lifecycle changes emit ``comment``.
ALLOWED_EVENT_TYPES = frozenset(
    {"created", "edited", "iterated", "referenced", "reverted", "comment"}
)


def append_event(
    art: Artifact,
    *,
    clock: Callable[[], str],
    cap: int,
    type: str,
    by: str | None = None,
    session_id: str | None = None,
    version: int | None = None,
    from_version: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append a lifecycle entry to ``art.events``, keeping at most ``cap`` (FIFO).

    Caller is responsible for persisting the record. ``clock`` supplies the
    entry's timestamp and is called only after ``type`` validates. Events are
    validated lightly — unknown ``type`` strings are rejected so callers
    can't poison the audit trail with arbitrary text. The ``by`` field
    is a free-form string ('user' / 'agent' / cron job name); ``version``
    is the artifact version that was active immediately AFTER the event
    (for ``edited``/``iterated`` events that bump version, the new
    post-bump value); ``from_version`` is set on ``reverted`` events to
    record which historical version the new state was copied from;
    ``metadata`` carries event-type-specific extras (e.g.
    ``referenced`` events store ``message_ts`` and ``widget_index`` so
    the activity timeline can group multiple impressions by chat
    location).
    """
    if type not in ALLOWED_EVENT_TYPES:
        raise ArtifactValidationError(
            f"invalid event type {type!r}: must be one of {sorted(ALLOWED_EVENT_TYPES)}"
        )
    entry: dict[str, Any] = {"ts": clock(), "type": type}
    if by:
        entry["by"] = str(by)[:64]
    if session_id:
        entry["session_id"] = str(session_id)[:128]
    if version is not None:
        entry["version"] = int(version)
    if from_version is not None:
        entry["from_version"] = int(from_version)
    if metadata:
        # Defense-in-depth: accept only string keys + simple scalar
        # values, cap each value at 256 chars. Prevents callers from
        # smuggling unbounded blobs into meta.json (which is read on
        # every artifact GET) or nested structures that would defeat
        # the activity timeline UI's flat-rendering assumption.
        cleaned: dict[str, Any] = {}
        for k, v in metadata.items():
            if not isinstance(k, str) or len(cleaned) >= 8:
                continue
            if isinstance(v, (str, int, float, bool)) or v is None:
                cleaned[k[:64]] = v[:256] if isinstance(v, str) else v
        if cleaned:
            entry["metadata"] = cleaned
    art.events.append(entry)
    # Cap the audit log so meta.json stays bounded — drop oldest first.
    if len(art.events) > cap:
        del art.events[: len(art.events) - cap]


def backfill_events(art: Artifact) -> bool:
    """Synthesize lifecycle events for legacy artifacts that pre-date the
    events field. Idempotent — sets ``events_backfilled=True`` so we
    only run once per artifact. Returns True if mutated.

    Generates a synthetic ``created`` event from ``created_at`` and one
    ``edited`` event per intermediate version (created_at → updated_at
    gap counts as a single edit if version > 1; we don't have per-version
    timestamps in legacy meta).
    """
    if art.events_backfilled or art.events:
        # Either explicitly backfilled before, or a fresh artifact whose
        # events were tracked from the start — nothing to do.
        return False
    if art.created_at:
        art.events.append(
            {
                "ts": art.created_at,
                "type": "created",
                "by": art.source if art.source != "chat" else "agent",
                "version": 1,
            }
        )
    if art.version > 1 and art.updated_at and art.updated_at != art.created_at:
        # We can't reconstruct per-version timestamps; collapse the gap
        # into a single edited event at updated_at.
        art.events.append(
            {
                "ts": art.updated_at,
                "type": "edited",
                "by": "unknown",
                "version": art.version,
            }
        )
    art.events_backfilled = True
    return True


def decode_meta(raw: Any, path: Path) -> Artifact:
    """Build an :class:`Artifact` from a parsed ``meta.json`` (the file at *path*)."""
    # Tolerant load: ignore unknown keys, fill defaults for missing keys.
    slug = raw.get("slug")
    if not slug:
        raise ArtifactError(f"meta.json missing slug: {path}")
    # Events: lifecycle audit log. Tolerate older meta.json files written
    # before the field existed — they get an empty
    # list and pick up a synthetic backfilled history on next get().
    raw_events = raw.get("events", []) or []
    events: _List[dict] = []
    if isinstance(raw_events, list):
        for ev in raw_events:
            if isinstance(ev, dict):
                events.append(dict(ev))
    # Publication: provider state. Tolerate older meta.json without the
    # field (defaults to None) and a malformed/partial block (a missing
    # artifact_id means the artifact isn't really published — treat as
    # unpublished rather than raising).
    publication = parse_publication(raw.get("publication"))
    fork_metadata = parse_fork_metadata(raw.get("fork_metadata"))
    image = parse_image_metadata(raw.get("image"))
    # Per-version render kinds (tolerant: keys + values must be str).
    raw_vk = raw.get("version_kinds") or {}
    version_kinds: dict[str, str] = {}
    if isinstance(raw_vk, dict):
        for vk_k, vk_v in raw_vk.items():
            if isinstance(vk_k, str) and isinstance(vk_v, str):
                version_kinds[vk_k] = vk_v
    # The salt keys the token HMAC; a malformed one is dropped and the next
    # current read assigns a fresh one, like the other tolerated fields.
    content_salt = raw.get("content_salt") or ""
    if not isinstance(content_salt, str) or (
        content_salt and _CONTENT_SALT_RE.fullmatch(content_salt) is None
    ):
        logger.warning("artifact meta.json content_salt malformed, resetting: %s", path)
        content_salt = ""
    return Artifact(
        slug=str(slug),
        name=str(raw.get("name", slug)),
        kind=str(raw.get("kind", "widget")),
        kind_auto=bool(raw.get("kind_auto", False)),
        source=str(raw.get("source", "chat")),
        description=str(raw.get("description", "")),
        tags=list(raw.get("tags", []) or []),
        version=int(raw.get("version", 1)),
        created_at=str(raw.get("created_at", "")),
        updated_at=str(raw.get("updated_at", "")),
        events=events,
        events_backfilled=bool(raw.get("events_backfilled", False)),
        source_path=str(raw.get("source_path", "")),
        # Tolerant: every artifact written before source_root existed
        # defaults to "" and therefore keeps exactly today's allowed-roots
        # behavior (home / data home / configured relocate roots).
        source_root=str(raw.get("source_root", "")),
        source_copy_only=bool(raw.get("source_copy_only", False)),
        folder_id=str(raw.get("folder_id") or ""),
        pinned=bool(raw.get("pinned", False)),
        session_key=str(raw.get("session_key", "")),
        auto_registered=bool(raw.get("auto_registered", False)),
        publication=publication,
        fork_metadata=fork_metadata,
        version_kinds=version_kinds,
        webapp_metadata=webapp_metadata_from_dict(raw.get("webapp_metadata")),
        image=image,
        content_salt=content_salt,
    )


def encode_meta(art: Artifact) -> str:
    """Serialize an artifact for ``meta.json``.

    ``content`` is never persisted there -- it lives in ``current.html`` -- and the
    GET-time fields ``live_dirty`` / ``source_missing`` are stripped by
    ``to_dict(persist=True)``.
    """
    return json.dumps(art.to_dict(persist=True), indent=2, sort_keys=True)


def parse_image_metadata(raw_img: Any) -> "ImageMetadata | None":
    """Build an :class:`ImageMetadata` from a meta.json sub-object.

    Returns ``None`` when the block is absent or not a dict (every non-image
    artifact). Tolerant per field: a wrong-typed value falls back to the
    dataclass default rather than raising, so a partially-written or
    forward/backward-skewed block still loads. ``width``/``height`` stay
    ``None`` unless present as ints — the sniff genuinely could not measure
    the image, and ``0`` would be a lie the frontend would box to.
    """
    if not isinstance(raw_img, dict):
        return None

    def _int_or_none(v: Any) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    def _int(v: Any) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    def _str(v: Any) -> str:
        return v if isinstance(v, str) else ""

    return ImageMetadata(
        mime=_str(raw_img.get("mime")),
        ext=_str(raw_img.get("ext")),
        size_bytes=_int(raw_img.get("size_bytes")),
        width=_int_or_none(raw_img.get("width")),
        height=_int_or_none(raw_img.get("height")),
        sha256=_str(raw_img.get("sha256")),
        original_filename=_str(raw_img.get("original_filename")),
        alt=_str(raw_img.get("alt")),
    )


def parse_publication(raw_pub: Any) -> "ArtifactPublication | None":
    """Build an ArtifactPublication from a meta.json sub-object.

    Returns None when absent or when the block lacks the stable
    ``artifact_id`` (the one field without which the publication is
    meaningless). All other fields fall back to dataclass defaults so a
    forward/backward schema drift never crashes a load.
    """
    if not isinstance(raw_pub, dict):
        return None
    artifact_id = raw_pub.get("artifact_id")
    if not artifact_id or not isinstance(artifact_id, str):
        return None
    raw_version_map = raw_pub.get("version_map") or {}
    version_map: dict[str, int] = {}
    if isinstance(raw_version_map, dict):
        for k, v in raw_version_map.items():
            try:
                version_map[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
    raw_shared = raw_pub.get("shared_with") or []
    shared_with = (
        [str(a) for a in raw_shared if isinstance(a, str)] if isinstance(raw_shared, list) else []
    )
    # Tolerant parse: a corrupted/hand-edited meta.json may store a
    # non-numeric value here; honor the "schema drift never crashes a load"
    # contract by falling back to 0 (same pattern as version_map above).
    try:
        last_synced = int(raw_pub.get("last_synced_kirocrew_version", 0) or 0)
    except (TypeError, ValueError):
        last_synced = 0
    try:
        wrapper_rev = int(raw_pub.get("wrapper_revision", 0) or 0)
    except (TypeError, ValueError):
        wrapper_rev = 0
    return ArtifactPublication(
        artifact_id=str(artifact_id),
        view_url=str(raw_pub.get("view_url") or ""),
        provider=str(raw_pub.get("provider") or DEFAULT_PROVIDER),
        visibility=str(raw_pub.get("visibility") or "PRIVATE"),
        shared_with=shared_with,
        auto_sync=bool(raw_pub.get("auto_sync", True)),
        collab_mode=("live" if raw_pub.get("collab_mode") == "live" else "mirror"),
        last_pushed_sha256=str(raw_pub.get("last_pushed_sha256") or ""),
        last_synced_kirocrew_version=last_synced,
        wrapper_revision=wrapper_rev,
        version_map=version_map,
        published_at=str(raw_pub.get("published_at") or ""),
        published_by=str(raw_pub.get("published_by") or ""),
        last_error=str(raw_pub.get("last_error") or ""),
        notice=str(raw_pub.get("notice") or ""),
        notice_code=str(raw_pub.get("notice_code") or ""),
        last_synced_remote_hash=str(raw_pub.get("last_synced_remote_hash") or ""),
    )


def parse_fork_metadata(raw_fm: Any) -> "ForkMetadata | None":
    """Build a ForkMetadata from a meta.json sub-object.

    Returns None when absent or when the block lacks the upstream
    ``upstream_artifact_id``. All other fields fall back to defaults.
    """
    if not isinstance(raw_fm, dict):
        return None
    upstream_id = raw_fm.get("upstream_artifact_id")
    if not upstream_id or not isinstance(upstream_id, str):
        return None
    try:
        upstream_version = int(raw_fm.get("upstream_version") or 0)
    except (ValueError, TypeError):
        upstream_version = 0
    return ForkMetadata(
        upstream_artifact_id=str(upstream_id),
        upstream_url=str(raw_fm.get("upstream_url") or ""),
        upstream_owner=str(raw_fm.get("upstream_owner") or ""),
        upstream_version=upstream_version,
        forked_at=str(raw_fm.get("forked_at") or ""),
        upstream_provider=str(raw_fm.get("upstream_provider") or ""),
    )


def decode_comments(raw_list: Any) -> _List[ArtifactComment]:
    """Build the comment list from a parsed ``comments.json`` (tolerant: a
    non-list document, a non-dict entry or an entry without an id is skipped)."""
    if not isinstance(raw_list, list):
        return []
    comments: _List["ArtifactComment"] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        cid = raw.get("id")
        if not cid:
            continue
        comments.append(
            ArtifactComment(
                id=str(cid),
                origin=str(raw.get("origin") or "local"),
                provider=raw.get("provider"),
                scope=str(raw.get("scope") or "private"),
                author=str(raw.get("author") or ""),
                is_agent=bool(raw.get("is_agent")),
                body=str(raw.get("body") or ""),
                anchor_quote=raw.get("anchor_quote"),
                anchor_prefix=raw.get("anchor_prefix"),
                anchor_suffix=raw.get("anchor_suffix"),
                anchor_start_offset=raw.get("anchor_start_offset"),
                anchor_end_offset=raw.get("anchor_end_offset"),
                anchor_version=raw.get("anchor_version"),
                thread_id=str(raw.get("thread_id") or cid),
                parent_id=raw.get("parent_id"),
                status=str(raw.get("status") or "open"),
                target_provider=raw.get("target_provider"),
                target_external_id=raw.get("target_external_id"),
                sync_state=str(raw.get("sync_state") or "local_only"),
                anchor_orphaned=bool(raw.get("anchor_orphaned")),
                created_at=str(raw.get("created_at") or ""),
                updated_at=str(raw.get("updated_at") or ""),
            )
        )
    return comments


def encode_comments(comments: _List[ArtifactComment]) -> str:
    """Serialize comments for ``comments.json``: fixed keys first, then each
    optional key only when it carries a value."""
    data = []
    for c in comments:
        entry: dict[str, Any] = {
            "id": c.id,
            "origin": c.origin,
            "scope": c.scope,
            "author": c.author,
            "is_agent": c.is_agent,
            "body": c.body,
            "thread_id": c.thread_id,
            "status": c.status,
            "sync_state": c.sync_state,
            "created_at": c.created_at,
            "updated_at": c.updated_at,
        }
        if c.provider:
            entry["provider"] = c.provider
        if c.parent_id:
            entry["parent_id"] = c.parent_id
        if c.target_provider:
            entry["target_provider"] = c.target_provider
        if c.target_external_id:
            entry["target_external_id"] = c.target_external_id
        if c.anchor_orphaned:
            entry["anchor_orphaned"] = True
        if c.anchor_quote:
            entry["anchor_quote"] = c.anchor_quote
        if c.anchor_prefix:
            entry["anchor_prefix"] = c.anchor_prefix
        if c.anchor_suffix:
            entry["anchor_suffix"] = c.anchor_suffix
        if c.anchor_start_offset is not None:
            entry["anchor_start_offset"] = c.anchor_start_offset
        if c.anchor_end_offset is not None:
            entry["anchor_end_offset"] = c.anchor_end_offset
        if c.anchor_version is not None:
            entry["anchor_version"] = c.anchor_version
        data.append(entry)
    return json.dumps(data, indent=2)


#: Field types :func:`patch_fork_metadata` accepts, by field name.
_FORK_FIELD_TYPES = {
    "upstream_artifact_id": str,
    "upstream_url": str,
    "upstream_owner": str,
    "upstream_version": int,
    "forked_at": str,
    "upstream_provider": str,
}


def patch_fork_metadata(fm: ForkMetadata, fields: dict[str, Any]) -> None:
    """Apply ``fields`` to a fork-metadata block, refusing unknown names and wrong types."""
    for k, v in fields.items():
        if k not in _FORK_FIELD_TYPES:
            raise ArtifactError(f"ForkMetadata has no field {k!r}")
        expected = _FORK_FIELD_TYPES[k]
        if not isinstance(v, expected):
            raise ArtifactError(
                f"ForkMetadata.{k} expects {expected.__name__}, got {type(v).__name__}"
            )
        setattr(fm, k, v)


def patch_publication(pub: ArtifactPublication, fields: dict[str, Any]) -> None:
    """Apply ``fields`` to a publication block, refusing names it does not have."""
    valid = {f.name for f in fields_of(ArtifactPublication)}
    for key, value in fields.items():
        if key not in valid:
            raise ArtifactValidationError(f"unknown publication field: {key}")
        setattr(pub, key, value)
