"""A frozen dashboard: what it showed, which template drew it, and where the log was.

A live dashboard answers "what is true now", and that is the wrong tool for "what was
true when this went wrong". A **snapshot** is the second question: the field values at
one moment, plus everything needed to know what those values meant -- the template and
version that laid them out, the instance version that was installed, and the crew-log
sequence the values were read at.

**All five, or it is not a snapshot.** A bag of numbers with no template is a page
nobody can redraw; a page with no seq cannot be placed against the log it came from;
values with no instance version cannot be told from values the crewmate's next edit
would have laid out differently. So each is required, and a snapshot that cannot name
one is refused rather than stored with a gap a later reader fills in by guessing.

**Frozen means frozen.** A snapshot is written once and never updated -- there is no
function here that changes one. A snapshot that could be edited is a claim about the
past that the present can rewrite, which is the one property that would make it
worthless as evidence. Writing one twice under the same id is refused for the same
reason.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.dashboard_templates.instance import instance_dir

__all__ = [
    "MAX_SNAPSHOTS",
    "MAX_SNAPSHOT_BYTES",
    "SCHEMA_VERSION",
    "Snapshot",
    "SnapshotRefused",
    "list_snapshots",
    "read_snapshot",
    "take_snapshot",
]

SCHEMA_VERSION: Final[int] = 1

#: Snapshots retained per crewmate, newest kept. A bound on the directory, not a
#: judgement about which snapshot matters: the oldest goes first because nothing here
#: knows which one somebody is about to need.
MAX_SNAPSHOTS: Final[int] = 50

#: One snapshot's ceiling. The values come from a template's at most 24 fields, so a
#: document over this is a field carrying something no dashboard renders.
MAX_SNAPSHOT_BYTES: Final[int] = 64 * 1024

_SUBDIR: Final[str] = "snapshots"
#: Snapshot ids are MINTED here, never supplied. The id is also the filename, so a
#: caller-chosen id is a path a caller chose; and a stamp-ordered id is what makes a
#: directory listing a chronology without opening every file.
#: ``\Z``, never ``$``: Python's ``$`` also matches just before a TRAILING NEWLINE, so
#: a ``$``-anchored id lets ``"<stamp>-<hex>\n"`` through the grammar check and into the
#: path join below -- which is the one thing :func:`read_snapshot` says it prevents. It
#: also reaches the listing and the prune, where a file whose stem ends in a newline
#: would be offered back as an id a caller can pass.
_ID = re.compile(r"^[0-9]{13}-[0-9a-f]{8}\Z")


class SnapshotRefused(ValueError):
    """A snapshot was refused, with the reason a user can be told."""


@dataclass(frozen=True)
class Snapshot:
    """One frozen dashboard. Every field is required; see the module docstring."""

    id: str
    slug: str
    template_id: str
    template_version: int
    instance_version: int
    fields: Mapping[str, Any]
    #: The crew-log sequence the values were read at. What places these values against
    #: the log, so a reader can see what the session did next.
    seq: int
    captured_ms: int

    def wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "template": {"id": self.template_id, "version": self.template_version},
            "instance_version": self.instance_version,
            "fields": dict(self.fields),
            "seq": self.seq,
            "captured_ms": self.captured_ms,
        }


def _dir(slug: str) -> Path:
    return instance_dir(slug) / _SUBDIR


def _mint_id(captured_ms: int) -> str:
    """A stamp plus random tail: ordered by time, and unique within one millisecond."""
    import secrets

    return f"{captured_ms:013d}-{secrets.token_hex(4)}"


def take_snapshot(
    slug: str,
    *,
    template_id: str,
    template_version: int,
    instance_version: int,
    fields: Mapping[str, Any],
    seq: int,
) -> Snapshot:
    """Freeze one dashboard. Refuses rather than storing a snapshot missing a part."""
    if not template_id:
        raise SnapshotRefused("a snapshot must name the template that drew it")
    if not isinstance(template_version, int) or template_version < 1:
        raise SnapshotRefused(f"template version {template_version!r} must be a positive integer")
    if not isinstance(instance_version, int) or instance_version < 1:
        raise SnapshotRefused(f"instance version {instance_version!r} must be a positive integer")
    if not isinstance(seq, int) or seq < 0:
        raise SnapshotRefused(f"seq {seq!r} must be a non-negative integer")
    if not isinstance(fields, Mapping) or not fields:
        raise SnapshotRefused("a snapshot with no field values records nothing")
    captured_ms = int(time.time() * 1000)
    snapshot = Snapshot(
        id=_mint_id(captured_ms),
        slug=slug,
        template_id=template_id,
        template_version=template_version,
        instance_version=instance_version,
        fields=dict(fields),
        seq=seq,
        captured_ms=captured_ms,
    )
    body = json.dumps(
        {"schema": SCHEMA_VERSION, **snapshot.wire()}, ensure_ascii=False, sort_keys=True
    )
    size = len(body.encode("utf-8"))
    if size > MAX_SNAPSHOT_BYTES:
        raise SnapshotRefused(
            f"the snapshot is {size} bytes, over the {MAX_SNAPSHOT_BYTES}-byte ceiling"
        )
    directory = _dir(slug)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{snapshot.id}.json"
    if path.exists():  # pragma: no cover - a 32-bit collision inside one millisecond
        raise SnapshotRefused("a snapshot already exists under that id; frozen means frozen")
    atomic_write(path, body, fsync=True)
    fsync_dir(directory)
    _prune(directory)
    return snapshot


def _prune(directory: Path) -> None:
    kept = sorted(p for p in directory.iterdir() if p.suffix == ".json" and _ID.match(p.stem))
    for path in kept[: max(0, len(kept) - MAX_SNAPSHOTS)]:
        try:
            path.unlink()
        except OSError:  # pragma: no cover - a losing race with another pruner
            pass


def list_snapshots(slug: str) -> tuple[str, ...]:
    """Snapshot ids for *slug*, oldest first. Ordered by the stamp inside the id."""
    try:
        children = list(_dir(slug).iterdir())
    except OSError:
        return ()
    return tuple(sorted(p.stem for p in children if p.suffix == ".json" and _ID.match(p.stem)))


def read_snapshot(slug: str, snapshot_id: str) -> Snapshot:
    """One frozen dashboard.

    The id is matched against :data:`_ID` BEFORE it is joined to a path. The id is the
    filename, so an unmatched one is a traversal rather than a miss, and refusing on the
    grammar means no caller-supplied text ever reaches the join.
    """
    if not _ID.match(snapshot_id or ""):
        raise SnapshotRefused(f"snapshot id {snapshot_id!r} is not a snapshot id")
    path = _dir(slug) / f"{snapshot_id}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SnapshotRefused(f"no snapshot {snapshot_id!r} for {slug!r}") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotRefused(f"snapshot {snapshot_id!r} cannot be read: {exc}") from None
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
        raise SnapshotRefused(f"snapshot {snapshot_id!r} is not schema {SCHEMA_VERSION}")
    template = raw.get("template")
    template = template if isinstance(template, dict) else {}
    fields = raw.get("fields")
    return Snapshot(
        id=str(raw.get("id") or snapshot_id),
        slug=str(raw.get("slug") or slug),
        template_id=str(template.get("id") or ""),
        template_version=int(template.get("version") or 0),
        instance_version=int(raw.get("instance_version") or 0),
        fields=dict(fields) if isinstance(fields, dict) else {},
        seq=int(raw.get("seq") or 0),
        captured_ms=int(raw.get("captured_ms") or 0),
    )
