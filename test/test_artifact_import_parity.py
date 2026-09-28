"""Guard: the library's importable-file map must match the backend's kind map.

``IMPORTABLE_EXT_KINDS`` in ``website/src/lib/artifactImport.ts`` decides which
files the Artifact Library's "Add Artifact" button accepts and what ``kind`` it
stamps on the resulting artifact. ``_EXT_KIND_MAP`` in ``kiro_crew.artifacts``
answers the same question for the backend's own kind inference.

They must stay identical. If the frontend map gains an extension the backend
does not know, the artifact is still created -- with a kind the frontend chose
unilaterally. If the backend map gains one and the frontend does not, the button
silently refuses a file type the store handles fine. Neither failure surfaces
anywhere else, because the frontend passes ``kind`` explicitly and so never
exercises the backend's inference on this path.

This repository already carries four hand-maintained copies of the artifact
*kind* list that have drifted apart (``ALLOWED_KINDS``, the MCP arg-schema
regex, the MCP tool ``enum``, and the CLI ``choices``). This test exists so the
extension map does not become the fifth.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from kiro_crew.artifacts import _EXT_KIND_MAP, ALLOWED_KINDS

_MODULE = (
    Path(__file__).resolve().parents[1]
    / "website"
    / "src"
    / "lib"
    / "artifactImport.ts"
)

# `export const IMPORTABLE_EXT_KINDS: Record<...> = { ... }` -- capture the
# object body up to the closing brace at column 0.
_MAP_RE = re.compile(
    r"export const IMPORTABLE_EXT_KINDS:[^=]*=\s*\{(?P<body>.*?)^\}",
    re.DOTALL | re.MULTILINE,
)
_ENTRY_RE = re.compile(r"'(?P<ext>\.[^']+)':\s*'(?P<kind>[a-z]+)'")


def _frontend_map() -> dict[str, str]:
    source = _MODULE.read_text(encoding="utf-8")
    match = _MAP_RE.search(source)
    assert match, "could not find IMPORTABLE_EXT_KINDS in artifactImport.ts"
    entries = _ENTRY_RE.findall(match.group("body"))
    assert entries, "IMPORTABLE_EXT_KINDS parsed as empty"
    return {ext: kind for ext, kind in entries}


def test_frontend_import_map_matches_backend_ext_kind_map() -> None:
    assert _frontend_map() == dict(_EXT_KIND_MAP), (
        "IMPORTABLE_EXT_KINDS (artifactImport.ts) and _EXT_KIND_MAP "
        "(artifacts.py) have drifted. Update both, or -- if they must "
        "genuinely differ -- record why here."
    )


def test_every_importable_kind_is_an_allowed_kind() -> None:
    """A kind the store would reject must never be offered by the picker."""
    unknown = sorted(set(_frontend_map().values()) - set(ALLOWED_KINDS))
    assert not unknown, f"importable kinds absent from ALLOWED_KINDS: {unknown}"


def test_importable_kinds_exclude_unrenderable_kinds() -> None:
    """Only kinds with a real dashboard renderer may be importable.

    ``widget`` is an agent-authored mcwidget body rather than a file on disk,
    ``webapp`` is a deploy control card, and ``image`` has no renderer in the
    dashboard at all (it is missing from the frontend's own ``Artifact['kind']``
    union). Importing any of them would produce an artifact the library cannot
    display.
    """
    offered = set(_frontend_map().values())
    assert not offered & {"widget", "webapp", "image"}


def test_frontend_content_cap_matches_backend() -> None:
    """The client-side size check must not accept more than the store will."""
    from kiro_crew.artifacts import MAX_CONTENT_BYTES

    source = _MODULE.read_text(encoding="utf-8")
    match = re.search(r"MAX_IMPORT_BYTES\s*=\s*([0-9_]+)", source)
    assert match, "could not find MAX_IMPORT_BYTES in artifactImport.ts"
    assert int(match.group(1).replace("_", "")) == MAX_CONTENT_BYTES


def test_accept_attribute_covers_every_importable_extension() -> None:
    """`IMPORT_ACCEPT` is derived, not hand-written -- pin that it stays so."""
    source = _MODULE.read_text(encoding="utf-8")
    assert "Object.keys(IMPORTABLE_EXT_KINDS).join(',')" in source


def test_locale_catalogs_all_carry_the_new_keys() -> None:
    """Every shipped catalog must define the library's add/create strings.

    A missing key renders English inside an otherwise-translated page. The
    frontend's own ``catalogParity`` suite enforces this too; this assertion
    keeps the backend suite from being the only thing a Python-only change
    runs, since the strings and the map ship together.

    ``add_artifact`` is deliberately absent: the toolbar button it labelled was
    replaced by the ``New Artifact`` split button, leaving the key with no call
    site. It was removed rather than kept as dead copy, which is why the
    frontend's dead-key baseline still holds. The file import it fronted lives
    on under ``import_from_a_file``, asserted below.
    """
    locales = _MODULE.parents[1] / "i18n" / "locales"
    expected = {
        "new_artifact",
        "import_from_a_file",
        "add_a_file_from_your_computer_to_the_library",
        "add_artifact_error_unsupported_type",
        "add_artifact_error_too_large",
        "add_artifact_error_empty",
        "add_artifact_error_not_text",
        "add_artifact_error_unreadable",
        "add_artifact_error_unfiled",
        "add_artifact_error_redacted",
    }
    english = json.loads((locales / "en.manual.json").read_text(encoding="utf-8"))
    assert expected <= set(english["pages"]["artifactsPage"]), "en.manual.json"
    translated_catalogs = sorted(
        path
        for path in locales.glob("*.json")
        if path.name not in {"en.json", "en.manual.json", "en-XA.json"}
    )
    for path in translated_catalogs:
        tag = path.stem
        catalog = json.loads(path.read_text(encoding="utf-8"))
        bucket = set(catalog["pages"]["artifactsPage"])
        missing = sorted(expected - bucket)
        assert not missing, f"{tag}.json missing {missing}"
