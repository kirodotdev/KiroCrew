"""``_RESTORABLE_UI_LANGUAGES`` must equal the frontend's shipped catalogue.

``SUPPORTED_LANGUAGES`` in ``website/src/i18n/languages.ts`` is the single source
of truth for which UI languages exist. ``_RESTORABLE_UI_LANGUAGES`` in
``kiro_crew.context`` answers a narrower question for the backend — which stored
``dashboard.language`` values name a language this install can actually render —
and it must stay identical to that catalogue minus the dev-only pseudolocale.

Both directions fail silently without this test, which is why it exists:

* A language shipped in the frontend but missing here stops reaching the model.
  The dashboard renders, say, Korean while tool-call purposes revert to the
  conversation's language — the mismatch of :issue:`1130`, pointing the other way.
* A code here that the frontend does not ship puts the original bug back: the
  agent is told to write in a language the chrome has no catalog for.

The backend deliberately holds a copy rather than parsing the frontend at
runtime: ``website/src`` is a source-tree path, not part of what an installed
gateway can rely on. That trade is only safe while the copy cannot drift
unnoticed, which is this test's whole job — the same guard
``test_artifact_import_parity.py`` puts on ``_EXT_KIND_MAP``, and for the same
reason. A failure here means one edit is missing, not that the test is wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.context import _RESTORABLE_UI_LANGUAGES

_MODULE = (
    Path(__file__).resolve().parents[1]
    / "website"
    / "src"
    / "i18n"
    / "languages.ts"
)

# `export const SUPPORTED_LANGUAGES: readonly LanguageEntry[] = [ ... ] as const`
# -- capture the array body up to the closing bracket at column 0.
_LIST_RE = re.compile(
    r"export const SUPPORTED_LANGUAGES:[^=]*=\s*\[(?P<body>.*?)^\]",
    re.DOTALL | re.MULTILINE,
)

# One entry: `{ code: 'zh-CN', label: '简体中文' },` optionally with `devOnly: true`.
# Entries are separated by `},` so a doc comment between them cannot merge two.
_ENTRY_RE = re.compile(
    r"\{\s*code:\s*'(?P<code>[^']+)'(?P<rest>[^}]*)\}",
    re.DOTALL,
)


def _frontend_entries() -> list[tuple[str, bool]]:
    """Return ``(code, dev_only)`` for every registered language."""
    source = _MODULE.read_text(encoding="utf-8")
    match = _LIST_RE.search(source)
    assert match, "could not find SUPPORTED_LANGUAGES in languages.ts"
    entries = [
        (m.group("code"), "devOnly" in m.group("rest"))
        for m in _ENTRY_RE.finditer(match.group("body"))
    ]
    assert entries, "SUPPORTED_LANGUAGES parsed as empty"
    return entries


def test_backend_set_matches_frontend_catalogue() -> None:
    """The two lists must name the same shipped languages."""
    shipped = {code for code, dev_only in _frontend_entries() if not dev_only}
    assert shipped == set(_RESTORABLE_UI_LANGUAGES), (
        "SUPPORTED_LANGUAGES (website/src/i18n/languages.ts) and "
        "_RESTORABLE_UI_LANGUAGES (kiro_crew/context.py) have drifted. "
        f"frontend-only: {sorted(shipped - set(_RESTORABLE_UI_LANGUAGES))}; "
        f"backend-only: {sorted(set(_RESTORABLE_UI_LANGUAGES) - shipped)}. "
        "Adding a language needs the entry here too, so the agent is told about "
        "it; removing one needs it dropped here, so the agent stops being told."
    )


def test_parse_finds_the_whole_catalogue() -> None:
    """Guard the regex itself: a silent parse failure would assert nothing.

    If ``languages.ts`` is reformatted so the entry pattern stops matching, the
    set comparison above could pass vacuously on an empty parse. Anchoring on the
    known-shipped count and on ``en`` makes that a failure instead.
    """
    entries = _frontend_entries()
    assert len(entries) >= 10, f"parsed only {len(entries)} entries: {entries}"
    codes = [code for code, _ in entries]
    assert "en" in codes, f"'en' missing from the parse: {codes}"
    assert len(codes) == len(set(codes)), f"duplicate codes parsed: {codes}"


def test_pseudolocale_is_registered_but_not_restorable() -> None:
    """``en-XA`` must be parsed as dev-only and excluded from the backend set.

    It is registered on purpose — ``resolveLanguage()`` matches on the primary
    subtag, so an unregistered ``en-XA`` would collapse to ``en`` and the
    pseudolocale would never activate. But a stored dev-only code degrades to
    auto-detect in a shipped build (``isRestorableLanguage``), so injecting it
    would tell the model to write in a locale the chrome is not rendering.
    """
    dev_only = {code for code, is_dev in _frontend_entries() if is_dev}
    assert "en-XA" in dev_only, (
        "en-XA is no longer marked devOnly in languages.ts; if the pseudolocale "
        "became a real shipped language this test and the backend set need it."
    )
    assert not dev_only & set(_RESTORABLE_UI_LANGUAGES), (
        f"dev-only codes leaked into _RESTORABLE_UI_LANGUAGES: "
        f"{sorted(dev_only & set(_RESTORABLE_UI_LANGUAGES))}"
    )
