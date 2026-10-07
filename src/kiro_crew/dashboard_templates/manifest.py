"""The run-time template format every dynamic dashboard shares.

A template is a directory with two files:

``manifest.json``
    ``id``, ``version`` (positive int), ``title``, ``description``, ``source``
    (``builtin`` | ``user`` | ``shared``) and ``fields``: field name -> field spec.
``template.html``
    A body fragment. Each value sits on an element carrying
    ``data-dashboard-field="<name>"``; scripts may read the same values from
    ``window.kirocrew.fields`` to draw charts.

A field spec is ``{"type": ..., "source": ...}``. ``type`` is one of
:data:`FIELD_TYPES`. ``source`` says where the value comes from:

``{"fold": "<name>", "path": "a.b.c"}``
    A crew-log fold (session or slot keyed) read through the bus subscription, never
    by refolding the log. ``path`` walks the rendered fold value.
``{"agentic": true}``
    The agent writes the value itself (an agentic fold). The page marks it.

This is declarative on purpose: a user template carries no code, so the gateway can
load one at run time without running anything an agent wrote. Built-in templates use
the same format, so one loader, one parity check and one registry serve all three
sources.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

from kiro_crew.crew_log.projection import SESSION_FOLD_NAMES, SLOT_PROJECTION_NAMES
from kiro_crew.dashboard_templates.parity import ExtractionRefused, html_fields

__all__ = [
    "FIELD_TYPES",
    "FOLD_NAMES",
    "FieldSpec",
    "ManifestError",
    "TemplateManifest",
    "load_template",
    "parse_manifest",
]

#: Every name set below is checked with ``in``, which HASHES the candidate, so each
#: membership test requires a string of its own first: a manifest is free to carry a
#: list or an object where a name belongs, and an unhashable candidate raises out of
#: the loader instead of being collected as one more refusal.
FIELD_TYPES: Final[frozenset[str]] = frozenset({"number", "string", "boolean", "array", "object"})
FOLD_NAMES: Final[frozenset[str]] = frozenset(SESSION_FOLD_NAMES) | frozenset(SLOT_PROJECTION_NAMES)
SOURCES: Final[frozenset[str]] = frozenset({"builtin", "user", "shared"})
#: ``\Z``, never ``$``: Python's ``$`` also matches just before a TRAILING NEWLINE, so
#: ``"report\n"`` satisfies a ``$``-anchored id. That id becomes a user-template
#: directory name and a registry key, so it would sit beside ``report`` looking
#: identical and colliding with nothing -- two templates a reader cannot tell apart.
#: The same hole is open on a field name and a fold path, which end up as a DOM
#: attribute and a walked key, so all three are anchored the same way.
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")
_PATH = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*\Z")
#: The host's per-card ceiling (see dashboard-templates.md, "At most 24 fields").
MAX_FIELDS: Final[int] = 24


class ManifestError(ValueError):
    """A manifest or page the loader refuses, with every reason at once."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    fold: str | None
    path: str | None
    agentic: bool


@dataclass(frozen=True)
class TemplateManifest:
    id: str
    version: int
    title: str
    description: str
    source: str
    fields: Mapping[str, FieldSpec]

    @property
    def folds(self) -> frozenset[str]:
        """The crew-log folds this template subscribes to."""
        return frozenset(f.fold for f in self.fields.values() if f.fold)


def _field(name: str, raw: Any, problems: list[str]) -> FieldSpec | None:
    if not _FIELD.match(name):
        problems.append(f"field {name!r}: name must match {_FIELD.pattern}")
        return None
    if not isinstance(raw, dict):
        problems.append(f"field {name!r}: spec must be an object")
        return None
    ftype = raw.get("type")
    if not isinstance(ftype, str) or ftype not in FIELD_TYPES:
        problems.append(f"field {name!r}: type {ftype!r} is not one of {sorted(FIELD_TYPES)}")
    source = raw.get("source")
    if not isinstance(source, dict):
        problems.append(f"field {name!r}: source must be an object")
        return None
    if source.get("agentic") is True:
        if set(source) != {"agentic"}:
            problems.append(f"field {name!r}: an agentic source carries no fold or path")
        return FieldSpec(name, str(ftype), None, None, True)
    fold, path = source.get("fold"), source.get("path")
    if not isinstance(fold, str) or fold not in FOLD_NAMES:
        problems.append(f"field {name!r}: fold {fold!r} is not one of {sorted(FOLD_NAMES)}")
    if not isinstance(path, str) or not _PATH.match(path):
        problems.append(f"field {name!r}: path {path!r} must be dotted keys")
    return FieldSpec(name, str(ftype), str(fold), str(path), False)


def parse_manifest(raw: Any) -> TemplateManifest:
    """Validate a decoded ``manifest.json``. Refuses with every problem, never a part."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise ManifestError(["manifest must be a JSON object"])
    tid, version, tsource = raw.get("id"), raw.get("version"), raw.get("source")
    if not isinstance(tid, str) or not _ID.match(tid):
        problems.append(f"id {tid!r} must match {_ID.pattern}")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        problems.append(f"version {version!r} must be a positive integer")
    for key in ("title", "description"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            problems.append(f"{key} must be a non-empty string")
    if not isinstance(tsource, str) or tsource not in SOURCES:
        problems.append(f"source {tsource!r} is not one of {sorted(SOURCES)}")
    raw_fields = raw.get("fields")
    fields: dict[str, FieldSpec] = {}
    if not isinstance(raw_fields, dict) or not raw_fields:
        problems.append("fields must be a non-empty object")
    else:
        if len(raw_fields) > MAX_FIELDS:
            problems.append(f"{len(raw_fields)} fields exceed the host's cap of {MAX_FIELDS}")
        for name, spec in raw_fields.items():
            parsed = _field(str(name), spec, problems)
            if parsed is not None:
                fields[parsed.name] = parsed
    if problems:
        raise ManifestError(problems)
    assert isinstance(tid, str) and isinstance(version, int)  # narrowed by the checks above
    return TemplateManifest(
        id=tid,
        version=version,
        title=raw["title"],
        description=raw["description"],
        source=raw["source"],
        fields=fields,
    )


def check_parity(manifest: TemplateManifest, html: str) -> None:
    """The page binds exactly the manifest's fields, in both directions."""
    try:
        bound = html_fields(html)
    except ExtractionRefused as exc:
        raise ManifestError([f"template.html: {exc}"]) from None
    declared = set(manifest.fields)
    problems = [
        f"template.html binds {n!r}, which the manifest does not declare"
        for n in sorted(bound - declared)
    ]
    problems += [
        f"manifest declares {n!r}, which template.html never binds"
        for n in sorted(declared - bound)
    ]
    if problems:
        raise ManifestError(problems)


def load_template(directory: Path) -> tuple[TemplateManifest, str]:
    """Load and fully check one template directory."""
    try:
        raw = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        html = (directory / "template.html").read_text(encoding="utf-8")
    # UnicodeDecodeError belongs with the other two: a template saved in any
    # non-UTF-8 encoding is a bad copy of THIS directory, and the scanner's whole
    # posture is that one bad directory isolates itself. Escaping as a bare
    # UnicodeDecodeError -- which is a ValueError, neither an OSError nor a
    # JSONDecodeError -- carries past `_scan` and takes the registry read with it, so
    # a single unreadable user template answers 500 for every built-in dashboard too.
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError([f"{directory.name}: {exc}"]) from None
    manifest = parse_manifest(raw)
    check_parity(manifest, html)
    return manifest, html
