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

from kiro_crew.knowledge.readers import FileReader

_WEBSITE = Path(__file__).resolve().parents[1] / "website"
_HELPERS = _WEBSITE / "src" / "pages" / "knowledge" / "helpers.ts"
_LOCALES = _WEBSITE / "src" / "i18n" / "locales"
_KNOWLEDGE_DOC = (
    Path(__file__).resolve().parents[1] / "docs" / "system-specs" / "modules" / "knowledge.md"
)

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


def test_docs_supported_listing_matches_backend() -> None:
    # The knowledge module spec carries a hand-written copy of
    # ``FileReader.SUPPORTED``. It is prose, so nothing at runtime reconciles
    # it, and it has drifted before (.jsonl/.ndjson were in the code but not
    # the spec). Compare set-wise: the block mirrors declaration order, not
    # sorted order, and '' (the extensionless marker) is a legitimate entry.
    source = _KNOWLEDGE_DOC.read_text(encoding="utf-8")
    marker = "is the ingestion allowlist:"
    idx = source.find(marker)
    assert idx != -1, "knowledge.md no longer carries the SUPPORTED listing intro"
    block = re.search(r"```\n(?P<body>.*?)```", source[idx:], re.DOTALL)
    assert block, "no fenced code block after the SUPPORTED listing intro"
    exts = set(re.findall(r"'(?P<ext>[^']*)'", block.group("body")))
    assert exts == FileReader.SUPPORTED, (
        "The SUPPORTED listing in docs/system-specs/modules/knowledge.md has "
        "drifted from FileReader.SUPPORTED (readers.py). Update the docs "
        "block when the allowlist changes."
    )


def test_max_file_size_copy_interpolates_the_served_limit() -> None:
    # The "Max N MB per file." sentence is catalog copy in every locale, while
    # the real bound is `dashboard.upload_max_mb`, which an operator can change.
    # SourcesList reads it from `/api/dashboard/config` and interpolates it, so
    # every catalog must carry the `{{max}}` placeholder rather than a literal
    # figure -- a hard-coded number would advertise a stale budget the moment
    # the config differs from it.
    checked = 0
    for catalog in sorted(_LOCALES.glob("*.json")):
        if catalog.name == "en.json":
            continue  # generated: key lives in en.manual.json
        data = json.loads(catalog.read_text(encoding="utf-8"))
        try:
            value = data["pages"]["knowledge"]["sourcesList"]["max_file_size"]
        except KeyError:
            continue  # a catalog without the key is caught by catalogParity.test.ts
        checked += 1
        assert "{{max}}" in value, (
            f"{catalog.name}: max_file_size copy {value!r} does not interpolate "
            "{{max}} -- the limit is served by /api/dashboard/config."
        )
        assert not re.search(
            r"\d", value.replace("{{max}}", "")
        ), f"{catalog.name}: max_file_size copy {value!r} still carries a literal figure"
    # en.manual.json + 11 translations + generated en-XA; a lower count means
    # the key moved and this guard checks nothing.
    assert checked >= 12, f"only {checked} catalogs carried max_file_size"
