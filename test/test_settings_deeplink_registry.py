"""Guards for the agent-facing settings registry bundled in the docs package.

`settings-registry.generated.json` is how a DEPLOYED gateway answers "where is
that setting?" with a working link: the agent is pointed at the bundled docs
directory (`context._build_docs_section`), reads the file, and hands back one
entry's `route`. Nothing about that path is visible to the frontend test suite,
so the two halves are guarded separately.

The frontend owns freshness — `settingsRegistry.test.ts` byte-matches this file
against a live extraction from the panels, so it cannot describe controls the
dashboard does not render. What is guarded HERE is everything that would make a
perfectly fresh file unreachable or unsafe: it has to sit in the directory the
agent is actually pointed at, it has to survive both packaging lanes, and every
route in it has to be an internal dashboard path.

The packaging checks MODEL the two lanes rather than building artifacts, for the
same reason `test_vendored_llama_payload.py` does: shelling out to `build` skips
wherever `build`/`setuptools` is missing, and a skip scores as a pass.
"""

from __future__ import annotations

import json
from fnmatch import fnmatch
from pathlib import Path

from kiro_crew.context import _BUNDLED_DOCS_DIR
from kiro_crew.tips import _sanitize_tip_action
from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

_REPO_ROOT = Path(__file__).resolve().parents[1]

_REGISTRY_NAME = "settings-registry.generated.json"
_SCHEME_DOC_NAME = "settings-deeplink.md"

#: Minimum entry count. The frontend generator refuses to write below its own
#: floor, but nothing stops a hand-edit or a bad merge from truncating the
#: committed file, and a registry of two entries fails silently: the agent simply
#: says it cannot find the setting.
_MIN_ENTRIES = 40

_REQUIRED_FIELDS = ("id", "label", "tab", "route")
_OPTIONAL_FIELDS = ("description", "configKey")


def _load_registry() -> list[dict[str, str]]:
    """The bundled registry, read from the directory the AGENT is pointed at.

    Deliberately resolved through `_BUNDLED_DOCS_DIR` rather than by repo path:
    that constant is what `_build_docs_section` puts in the prompt, so a file
    that moved out from under it is unreachable no matter where it sits in git.
    """
    payload = json.loads((_BUNDLED_DOCS_DIR / _REGISTRY_NAME).read_text(encoding="utf-8"))
    settings = payload["settings"]
    assert isinstance(settings, list)
    return settings


def test_registry_sits_where_the_agent_is_pointed() -> None:
    assert (_BUNDLED_DOCS_DIR / _REGISTRY_NAME).is_file()
    assert (_BUNDLED_DOCS_DIR / _SCHEME_DOC_NAME).is_file()
    # Flat, like every doc beside it: `package_data`'s `docs/*.json` glob does
    # not recurse, so a subdirectory would ship in the sdist and vanish from the
    # wheel (see the docs README).
    assert (_BUNDLED_DOCS_DIR / _REGISTRY_NAME).parent == _BUNDLED_DOCS_DIR


def test_registry_is_self_describing() -> None:
    """A reader that opens the JSON cold learns not to edit it, and where to look.

    JSON carries no comments, so the banner is a field; without it the file looks
    like hand-maintained data and the next contributor edits it in place.
    """
    payload = json.loads((_BUNDLED_DOCS_DIR / _REGISTRY_NAME).read_text(encoding="utf-8"))
    comment = payload["$comment"]
    assert "DO NOT EDIT" in comment
    assert "gen-settings-registry.mjs" in comment
    assert _SCHEME_DOC_NAME in comment


def test_registry_entries_carry_only_the_agent_facing_fields() -> None:
    entries = _load_registry()
    assert len(entries) >= _MIN_ENTRIES
    ids = [e["id"] for e in entries]
    assert len(set(ids)) == len(ids), "duplicate ids: an ambiguous anchor picks the wrong control"
    for entry in entries:
        for field in _REQUIRED_FIELDS:
            assert entry.get(field), f"{entry.get('id')!r} is missing {field}"
        extra = set(entry) - set(_REQUIRED_FIELDS) - set(_OPTIONAL_FIELDS)
        assert not extra, f"{entry['id']!r} exposes frontend-only fields {sorted(extra)}"


def test_every_route_passes_the_internal_path_validator() -> None:
    """Every route is safe to hand to the dashboard router.

    Reuses `_sanitize_tip_action` — the same server-side check the curated tips'
    deep links go through — rather than restating "leading `/`, not `//`, no
    scheme" here, so the registry can never be held to a weaker rule than the
    other surface that emits these links.
    """
    for entry in _load_registry():
        action = {"kind": "route", "label": "Open", "route": entry["route"]}
        assert _sanitize_tip_action(action) is not None, f"{entry['id']!r}: {entry['route']!r}"
        assert entry["route"].startswith(f"/settings/{entry['tab']}"), entry["id"]


def test_scheme_doc_is_indexed_but_not_a_tips_candidate() -> None:
    """Indexed for docs-lint, and kept out of the feature-tips catalog.

    `tips.py` globs `*.md` here and filters through the allowlist. The scheme doc
    is reference material for answering a question the user already asked, not a
    feature to announce above the composer — the exclusion the allowlist's own
    docstring calls out — so it must stay off that list.
    """
    assert _SCHEME_DOC_NAME not in TIP_DOC_ALLOWLIST
    for index in ("README.md", "index.md"):
        assert _SCHEME_DOC_NAME in (_BUNDLED_DOCS_DIR / index).read_text(encoding="utf-8")


def test_both_packaging_lanes_ship_the_registry() -> None:
    """The wheel reads `package_data`; the sdist reads MANIFEST.in.

    Only the wheel's glob is obvious, and `python -m build` builds the wheel FROM
    the sdist — so a missing MANIFEST.in rule drops the file from every published
    artifact while a local `pip install .` keeps working.
    """
    setup_cfg = (_REPO_ROOT / "setup.cfg").read_text(encoding="utf-8")
    assert "docs/*.json" in setup_cfg
    manifest = (_REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include src/kiro_crew/docs *.json" in manifest
    # No later exclude may strip it back out: MANIFEST.in is order-sensitive, and
    # `global-exclude *.so` silently removing `libllama.so` from every published
    # Linux wheel is the precedent (test_vendored_llama_payload.py).
    tail = manifest.split("recursive-include src/kiro_crew/docs *.json", 1)[1]
    for line in tail.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("exclude", "global-exclude", "recursive-exclude"):
            continue
        patterns = parts[2:] if parts[0] == "recursive-exclude" else parts[1:]
        for pattern in patterns:
            assert not fnmatch(_REGISTRY_NAME, pattern), line
