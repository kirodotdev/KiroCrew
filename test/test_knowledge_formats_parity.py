"""Guard: the Knowledge page's fallback format list must match the backend's.

``FALLBACK_SUPPORTED_FORMATS`` in ``website/src/pages/knowledge/helpers.ts`` is
what the upload picker's ``accept`` filter and the "Supported formats" copy
advertise until ``GET /api/knowledge/config`` resolves. That endpoint serves
``sorted(FileReader.SUPPORTED - {''})``, so the fallback is a mirror of
``FileReader.SUPPORTED`` (``kiro_crew.knowledge.readers``).

They must stay identical. If the backend gains a format the fallback lacks, the
picker greys out files the backend would ingest fine (and the copy under-sells
it) for the window before config loads. If the fallback gains one the backend
dropped, the page advertises a format that fails at ingest. Neither failure
surfaces anywhere else: the drift is only visible before the config round-trip
completes, which no E2E test races. Three hand-synced copies of this list had
already drifted apart (the issue this test closes out), so the fallback is now
the ONLY frontend copy and this test is what keeps it honest.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from kiro_crew.dashboard.handlers.knowledge import _MAX_INGEST_FILE_SIZE
from kiro_crew.knowledge.readers import FileReader

_WEBSITE = Path(__file__).resolve().parents[1] / "website"
_HELPERS = _WEBSITE / "src" / "pages" / "knowledge" / "helpers.ts"
_LOCALES = _WEBSITE / "src" / "i18n" / "locales"

# `export const FALLBACK_SUPPORTED_FORMATS = [ ... ]` -- capture the array body
# up to the first closing bracket.
_ARRAY_RE = re.compile(
    r"export const FALLBACK_SUPPORTED_FORMATS\s*=\s*\[(?P<body>[^\]]*)\]",
    re.DOTALL,
)
_EXT_RE = re.compile(r"'(?P<ext>\.[^']+)'")


def _frontend_fallback() -> list[str]:
    source = _HELPERS.read_text(encoding="utf-8")
    match = _ARRAY_RE.search(source)
    assert match, "could not find FALLBACK_SUPPORTED_FORMATS in helpers.ts"
    exts = _EXT_RE.findall(match.group("body"))
    assert exts, "FALLBACK_SUPPORTED_FORMATS parsed as empty"
    return exts


def test_frontend_fallback_matches_backend_supported_formats() -> None:
    # Equality against the exact value /api/knowledge/config serves ('' is the
    # extensionless marker, surfaced separately as `accepts_no_extension`).
    assert _frontend_fallback() == sorted(FileReader.SUPPORTED - {""}), (
        "FALLBACK_SUPPORTED_FORMATS (helpers.ts) and FileReader.SUPPORTED "
        "(readers.py) have drifted. Update the frontend fallback to "
        "`sorted(FileReader.SUPPORTED - {''})` -- the backend list is the "
        "single source of truth for what upload ingests."
    )


def test_max_file_size_copy_matches_backend_limit() -> None:
    # The "Max 50 MB per file." sentence is hand-authored catalog copy in every
    # locale, while the real bound is `_MAX_INGEST_FILE_SIZE` in the knowledge
    # handler. `/api/knowledge/config` does not serve the limit, so nothing at
    # runtime reconciles them: changing the constant would leave the page
    # advertising a stale budget in 12 languages and uploads failing at ingest
    # for users the copy told were within it. Pin the figure here instead.
    expected = f"{_MAX_INGEST_FILE_SIZE // (1024 * 1024)} MB"
    checked = 0
    for catalog in sorted(_LOCALES.glob("*.json")):
        if catalog.name in ("en.json", "en-XA.json"):
            continue  # generated: key lives in en.manual.json; en-XA accents 'MB'
        data = json.loads(catalog.read_text(encoding="utf-8"))
        try:
            value = data["pages"]["knowledge"]["sourcesList"]["max_file_size"]
        except KeyError:
            continue  # a catalog without the key is caught by catalogParity.test.ts
        checked += 1
        assert expected in value, (
            f"{catalog.name}: max_file_size copy {value!r} does not carry "
            f"{expected!r} -- update the catalog (all locales) when "
            "_MAX_INGEST_FILE_SIZE changes, or serve the limit from "
            "/api/knowledge/config and interpolate it."
        )
    # en.manual.json + 11 translations + generated en-XA; a lower count means
    # the key moved and this guard is no longer checking anything.
    assert checked >= 12, f"only {checked} catalogs carried max_file_size"
