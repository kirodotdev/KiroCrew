"""Field rules for artifact records: limits, grammar, kind policy and scope matching.

Every artifact-authoring surface -- the store, the dashboard handlers and the MCP
tools -- decides these questions through the functions here, so they cannot drift
apart: which strings are slugs, what a tag or name may hold, which kind a body is,
which kinds a person may pick, whether iframe content hardcodes its palette, what
counts as a document, and whether a session touched an artifact. Validators raise
:class:`~kiro_crew.artifact_store.model.ArtifactValidationError`; nothing here reads
or writes the filesystem.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import List as _List

from kiro_crew.artifact_store.model import Artifact, ArtifactValidationError
from kiro_crew.slugs import slug_hash_fallback

#: Maximum length of human-readable name / description fields.
MAX_NAME_LEN = 200
MAX_DESCRIPTION_LEN = 2_000

#: Maximum length of a file-backed artifact's ``source_path`` / ``source_root``.
#: These are REJECTED past the cap rather than truncated: a truncated path is a
#: different path — usually a nonexistent one — so silently storing it produced
#: an artifact whose live pointer could never resolve, which is precisely the
#: dead-pointer failure ``source_missing`` exists to expose. Comfortably above
#: PATH_MAX on the platforms we support for any realistic project layout.
MAX_SOURCE_PATH_LEN = 512

#: Allowed kinds (extensible — agents pass plain strings, but a soft allow-list
#: keeps the dashboard's filter UI tractable).
ALLOWED_KINDS = frozenset(
    {
        "widget",  # default — sandboxed HTML/JS via mcwidget
        "html",  # raw html document
        "markdown",  # rendered to widget via MarkdownRenderer
        "svg",  # standalone svg
        "json",  # structured data
        "text",  # plain text
        "image",  # generated image (source_path points to PNG/JPEG)
        "webapp",  # a deployed web application; rendered as an infra control card
        # A dynamic dashboard's LAYOUT package -- bound_to / model / view / theme
        # as JSON, never values. Validated on every write by
        # :mod:`kiro_crew.artifact_store.dashboard_package`, which is also why
        # it is absent from USER_SELECTABLE_KINDS: a prose document hand-flipped
        # to this kind would hold content no reader can parse.
        "dashboard",
    }
)

#: Allowed source markers (provenance).
ALLOWED_SOURCES = frozenset(
    {
        # Provenance/creation buckets (back-compat) + actual session origins from
        # ``infer_use_case`` so an artifact's source can reflect WHERE the saving
        # session came from rather than a generic "manual".
        "chat",
        "cron",
        "subagent",
        "manual",
        "import",
        "dashboard",
        "slack",
        "cli",
        "task-runner",
        "unknown",
    }
)

#: Maximum number of tags per artifact. Per-tag length is bounded by ``MAX_TAG_LEN``.
MAX_TAGS = 16

#: Maximum length of one tag, in code points of its NFC form: the count a reader
#: perceives as characters. Bytes would hand a CJK tag a third of an ASCII tag's
#: room, and pre-NFC code points would make the same visible label pass or fail
#: on the keyboard that typed it.
MAX_TAG_LEN = 64

#: The separators a tag may carry between letters, marks and digits.
_TAG_SEPARATORS = frozenset("_:.-")

#: Unicode general categories a tag is made of: letters, the marks that attach to
#: them, and digits. Nonspacing and spacing marks (``Mn``, ``Mc``) are the
#: combining characters NFC leaves standing beside their base (Devanagari vowel
#: signs, Thai tone marks, Arabic harakat); without them whole scripts could not
#: be written as tags. Enclosing marks (``Me``) are NOT admitted: they draw a
#: shape around their base -- the keycap that turns ``1`` into an emoji, the
#: enclosing circle -- so they are symbols by another route. Other numbers
#: (``No``: ``½``, ``²``, ``①``, the numerals of scripts that do not count in
#: decimal digits) are not admitted either: they are symbols drawn from a digit,
#: not digits, and the ones with a plain digit spelling are compatibility forms
#: of it (refused below with that spelling named). Everything else is
#: refused: punctuation other than the separators, symbols and emoji, and every
#: control, format (zero-width, bidi override) and whitespace character, so a tag
#: is always visible and has one unambiguous spelling.
_TAG_BODY_CATEGORIES = frozenset({"Lu", "Ll", "Lt", "Lm", "Lo", "Mn", "Mc", "Nd", "Nl"})

#: The combining marks: the body categories that attach to the character before.
_TAG_MARK_CATEGORIES = frozenset({"Mn", "Mc"})

#: A tag opens with a letter or digit: a mark needs a base to attach to, and a
#: leading separator reads as a flag or a path.
_TAG_FIRST_CATEGORIES = _TAG_BODY_CATEGORIES - _TAG_MARK_CATEGORIES

#: How many combining marks one character may carry. Writing needs few: Hebrew
#: pointing stacks a dagesh, a vowel and a cantillation mark; a Tibetan syllable
#: stacks subjoined letters and a vowel sign; both stay within four. Unicode's
#: own Stream-Safe bound of 30 is for text buffers, not for a label of at most
#: 64 code points: ``a`` under 63 acute accents is a 64-code-point tag that is
#: not writing but a glyph, one that overflows the chip drawn for it.
MAX_CONSECUTIVE_MARKS = 4

#: Compatibility characters kept as typed. NFC leaves compatibility forms alone
#: -- full-width ``ｏｐｓ``, mathematical ``𝐨𝐩𝐬``, the ``ﬁ`` ligature, ``①``
#: and ``µ`` are all letters or numbers by category and all distinct code points
#: from ``ops``, ``fi``, ``1`` and ``μ`` -- so each would be a second stored tag
#: with the look of the first. ``normalize_tag`` refuses every code point whose
#: NFKC form differs from itself and names the plain spelling, except these two:
#: THAI CHARACTER SARA AM and LAO VOWEL SIGN AM decompose for compatibility into
#: NIKHAHIT + SARA AA, yet the composed form is the letter every Thai and Lao
#: keyboard types (``น้ำ``, water, is written with U+0E33). They are the only
#: tag-admissible letters whose ``<compat>`` decomposition opens with a combining
#: mark -- a letter with an inherent mark, not a presentation variant of other
#: letters -- and ``test_thai_and_lao_am_are_letters_not_compatibility_forms``
#: pins that the set is exactly them. Their decomposed twin stays admitted, a
#: residual stated in the spec beside cross-script confusables.
_COMPATIBILITY_LETTERS_KEPT = frozenset("\u0e33\u0eb3")

#: ``Default_Ignorable_Code_Point`` (Unicode 15.0, ``DerivedCoreProperties.txt``):
#: the code points that render as nothing. Most are format characters the
#: category test already refuses, but the property also holds letters and marks
#: -- the variation selectors (U+FE00-FE0F, U+E0100-E01EF, Mongolian U+180B-180D
#: and U+180F), the combining grapheme joiner, the Khmer inherent vowels and the
#: Hangul fillers -- which would pass as ``Mn`` or ``Lo``. Admitting one would
#: break "always visible, one spelling": ``ops`` and ``ops<VS16>`` would be two
#: stored tags with one look, and a key id with an invisible mark inside it would
#: pass a credential redactor while reading as the bare key. The table is the
#: whole published property, not just the subset the category test misses, so it
#: can be checked line by line against the standard. ``unicodedata`` does not
#: expose the property, hence the table; it matches the Unicode version Python
#: 3.12 ships (``unicodedata.unidata_version`` 15.0.0). The ranges already hold
#: the reserved code points the standard lists, so an assignment inside them
#: changes nothing; a later Unicode version can still add a range, so refresh
#: the table from ``DerivedCoreProperties.txt`` when the runtime's version moves
#: -- on such a runtime ``test_the_invisible_table_matches_the_runtime_unicode_version``
#: is skipped with a reason naming that step, and it holds on CI's 3.12 runners.
_DEFAULT_IGNORABLE_UNICODE_VERSION = "15.0.0"
_DEFAULT_IGNORABLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),  # SOFT HYPHEN
    (0x034F, 0x034F),  # COMBINING GRAPHEME JOINER
    (0x061C, 0x061C),  # ARABIC LETTER MARK
    (0x115F, 0x1160),  # HANGUL CHOSEONG FILLER..HANGUL JUNGSEONG FILLER
    (0x17B4, 0x17B5),  # KHMER VOWEL INHERENT AQ..KHMER VOWEL INHERENT AA
    (0x180B, 0x180D),  # MONGOLIAN FREE VARIATION SELECTOR ONE..THREE
    (0x180E, 0x180E),  # MONGOLIAN VOWEL SEPARATOR
    (0x180F, 0x180F),  # MONGOLIAN FREE VARIATION SELECTOR FOUR
    (0x200B, 0x200F),  # ZERO WIDTH SPACE..RIGHT-TO-LEFT MARK
    (0x202A, 0x202E),  # LEFT-TO-RIGHT EMBEDDING..RIGHT-TO-LEFT OVERRIDE
    (0x2060, 0x2064),  # WORD JOINER..INVISIBLE PLUS
    (0x2065, 0x2065),  # <reserved>
    (0x2066, 0x206F),  # LEFT-TO-RIGHT ISOLATE..NOMINAL DIGIT SHAPES
    (0x3164, 0x3164),  # HANGUL FILLER
    (0xFE00, 0xFE0F),  # VARIATION SELECTOR-1..VARIATION SELECTOR-16
    (0xFEFF, 0xFEFF),  # ZERO WIDTH NO-BREAK SPACE
    (0xFFA0, 0xFFA0),  # HALFWIDTH HANGUL FILLER
    (0xFFF0, 0xFFF8),  # <reserved>
    (0x1BCA0, 0x1BCA3),  # SHORTHAND FORMAT LETTER OVERLAP..SHORTHAND FORMAT UP STEP
    (0x1D173, 0x1D17A),  # MUSICAL SYMBOL BEGIN BEAM..MUSICAL SYMBOL END PHRASE
    (0xE0000, 0xE0000),  # <reserved>
    (0xE0001, 0xE0001),  # LANGUAGE TAG
    (0xE0002, 0xE001F),  # <reserved>
    (0xE0020, 0xE007F),  # TAG SPACE..CANCEL TAG
    (0xE0080, 0xE00FF),  # <reserved>
    (0xE0100, 0xE01EF),  # VARIATION SELECTOR-17..VARIATION SELECTOR-256
    (0xE01F0, 0xE0FFF),  # <reserved>
)
_DEFAULT_IGNORABLE = frozenset(
    cp for lo, hi in _DEFAULT_IGNORABLE_RANGES for cp in range(lo, hi + 1)
)

# Slug pattern: lowercase letters, digits, hyphens. 1-80 chars. No leading or
# trailing hyphen. Single-character slugs are allowed for trivial names.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")
_SLUG_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _compatibility_form_of(ch: str) -> str | None:
    """The plain spelling ``ch`` stands in for, or ``None`` when ``ch`` is its own spelling.

    ``ch`` is one code point of an NFC string, so a difference under NFKC is a
    compatibility decomposition: a presentation variant (full-width, mathematical,
    half-width, superscript, circled, a ligature) of characters that are the tag's
    plain spelling. The two letters in :data:`_COMPATIBILITY_LETTERS_KEPT` are
    their own spelling by decision, not by property.
    """
    if ch in _COMPATIBILITY_LETTERS_KEPT:
        return None
    plain = unicodedata.normalize("NFKC", ch)
    return None if plain == ch else plain


def _marks_carried_from(canonical: str, start: int) -> int:
    """How many combining marks run from ``canonical[start]`` on -- for the reason text."""
    count = 0
    for ch in canonical[start:]:
        if unicodedata.category(ch) not in _TAG_MARK_CATEGORIES:
            break
        count += 1
    return count


def normalize_tag(tag: str) -> str:
    """Return the stored spelling of a well-formed tag, or raise ``ValueError`` saying why.

    A tag is a user-facing label that lives only in artifact metadata: never a
    file name, a URL segment or a query identifier. That job belongs to the slug,
    which stays ASCII by transliterating (``slugify``). A label's alphabet is the
    user's alphabet, so after NFC normalization a tag is Unicode letters, the
    marks that attach to them, and digits plus ``_``, ``:``, ``.`` and ``-``,
    opening with a letter or digit, at most :data:`MAX_TAG_LEN` code points, and
    every code point is one a reader can see (no default-ignorable character, no
    enclosing mark) and is its own spelling (no compatibility form of other
    characters: full-width ``ｏｐｓ`` is refused naming ``o``), with at most
    :data:`MAX_CONSECUTIVE_MARKS` combining marks on one character. The stored
    spelling is the NFC form, so one label has one spelling however it was typed.

    This is the ONE tag rule: the store validates through it and the MCP
    argument gate (``validation.py``) checks tag arguments through it, so the
    two cannot drift. The reason in the ``ValueError`` is plain English and is
    shown to the caller as-is.
    """
    canonical = unicodedata.normalize("NFC", tag)
    if not canonical:
        raise ValueError("a tag cannot be empty")
    if len(canonical) > MAX_TAG_LEN:
        raise ValueError(
            f"a tag is at most {MAX_TAG_LEN} characters (this one has {len(canonical)})"
        )
    if unicodedata.category(canonical[0]) not in _TAG_FIRST_CATEGORIES:
        raise ValueError("a tag must start with a letter or digit")
    marks_in_a_row = 0
    after_separator = False
    for index, ch in enumerate(canonical):
        # Before the category test: an invisible letter or mark IS a letter or
        # mark by category, and the reason has to say what the reader cannot see.
        if ord(ch) in _DEFAULT_IGNORABLE:
            raise ValueError(
                f"character U+{ord(ch):04X} ({unicodedata.name(ch, 'unassigned')}) "
                "renders as nothing and is not allowed in a tag"
            )
        if ch in _TAG_SEPARATORS:
            marks_in_a_row = 0
            after_separator = True
            continue
        # Also before the category test: a compatibility form is a letter or
        # number by category, and the reason has to name the spelling to use.
        plain = _compatibility_form_of(ch)
        if plain is not None:
            raise ValueError(
                f"character {ch!r} (U+{ord(ch):04X}) is a compatibility form of {plain!r} "
                "and is not allowed in a tag: write the plain spelling"
            )
        category = unicodedata.category(ch)
        if category not in _TAG_BODY_CATEGORIES:
            raise ValueError(
                f"character {ch!r} (U+{ord(ch):04X}) is not allowed in a tag: "
                "use letters, digits, '_', ':', '.' or '-'"
            )
        if category not in _TAG_MARK_CATEGORIES:
            marks_in_a_row = 0
            after_separator = False
            continue
        # A mark attaches to the character before it, and that character is a
        # letter, a digit or another mark: a mark on a separator draws an accent
        # on a hyphen, which no script writes.
        if after_separator:
            raise ValueError(
                f"combining mark U+{ord(ch):04X} ({unicodedata.name(ch, 'unassigned')}) "
                f"follows {canonical[index - 1]!r}: a mark needs a letter or digit before it"
            )
        marks_in_a_row += 1
        if marks_in_a_row > MAX_CONSECUTIVE_MARKS:
            carried = _marks_carried_from(canonical, index - MAX_CONSECUTIVE_MARKS)
            raise ValueError(
                f"a character carries at most {MAX_CONSECUTIVE_MARKS} combining marks "
                f"(one here carries {carried})"
            )
    return canonical


def slugify(name: str) -> str:
    """Normalize a free-form name into a URL-safe slug.

    Falls back to ``artifact-<hash of the input>`` if the input contains no
    slug-safe characters, so distinct non-ASCII names derive distinct slugs.
    Truncated to 80 characters.
    """
    if not isinstance(name, str):
        raise ArtifactValidationError(f"name must be str, got {type(name).__name__}")
    # NFKD-normalize then drop combining marks so accented letters become ascii.
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().strip()
    text = _SLUG_NORMALIZE_RE.sub("-", text)
    text = text.strip("-")
    if not text:
        return slug_hash_fallback(name, "artifact")
    return text[:80].rstrip("-") or slug_hash_fallback(name, "artifact")


def _validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ArtifactValidationError(f"invalid slug {slug!r}: must match {_SLUG_RE.pattern}")
    return slug


#: Kinds a HUMAN may select for an artifact from the dashboard's type control.
#: A strict subset of :data:`ALLOWED_KINDS`, and deliberately the same set the
#: frontend's ``isEditableKind`` treats as inline-editable: ``widget`` / ``html``
#: render in a sandboxed iframe with no editor, so offering them would let the
#: user strand the document they are typing in, and ``webapp`` carries deploy
#: metadata that a hand-flip would desynchronise. Agents and importers still
#: reach the full :data:`ALLOWED_KINDS` set; this bounds the UI surface only.
USER_SELECTABLE_KINDS = frozenset({"markdown", "text", "json", "svg"})


def _validate_kind(kind: str) -> str:
    if kind not in ALLOWED_KINDS:
        raise ArtifactValidationError(
            f"invalid kind {kind!r}: must be one of {sorted(ALLOWED_KINDS)}"
        )
    return kind


#: File-extension → kind map for inferring the kind of a file-backed artifact
#: (one with a ``source_path``) when the caller didn't pin one. Keys lowercased.
_EXT_KIND_MAP = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "html",
    ".htm": "html",
    ".svg": "svg",
    ".json": "json",
    ".txt": "text",
}

#: Substrings whose presence marks inline content as renderable HTML (→
#: ``widget``). Matched case-insensitively against the lstripped content.
_HTML_SNIFF_MARKERS = (
    "<div",
    "<span",
    "<style",
    "<table",
    "<mcwidget",
    "<html",
    "<!doctype html",
)

#: A leading markdown ATX heading (``#`` .. ``######`` followed by whitespace).
_MD_HEADING_RE = re.compile(r"^#{1,6}\s")

#: An ``<svg>`` document root, optionally preceded by an XML declaration and/or
#: an SVG doctype. Anchored so a stray ``<svg>`` in the middle of a markdown
#: document never re-types it.
_SVG_ROOT_RE = re.compile(
    r"^(?:<\?xml[^>]*\?>\s*|<!DOCTYPE\s+svg[^>]*>\s*)*<svg[\s>]",
    re.IGNORECASE,
)


def detect_editor_kind(content: str) -> str | None:
    """Detect the structured kind of hand-authored ``content``, or ``None``.

    Used by :meth:`ArtifactStore.update` to re-type a document that was created
    blank (``kind_auto``) once its content becomes recognizable. It only ever
    returns an **editable** kind — ``json`` or ``svg`` — and returns ``None``
    for anything it cannot positively identify.

    Two deliberate exclusions:

    * ``html`` / ``widget`` are never returned. Both render in a sandboxed
      iframe and are NOT inline-editable (see ``isEditableKind`` on the
      frontend), so promoting a document the user is *typing in* to either one
      would rip the editor away mid-sentence. Markdown passes raw HTML through
      to its renderer anyway, so a hand-written HTML document still renders
      while staying editable.
    * a bare JSON scalar (``42``, ``true``, a quoted string) is valid JSON but
      far more likely to be prose, so only a whole document parsing as an
      object or an array counts.

    Returning ``None`` rather than ``"markdown"`` for unrecognized content is
    what makes re-typing stable: a JSON document left mid-edit with a syntax
    error detects as nothing and keeps the kind it already had, instead of
    flapping back to markdown on every save.
    """
    sniff = (content or "").strip()
    if not sniff:
        return None
    if _SVG_ROOT_RE.match(sniff):
        return "svg"
    if sniff[0] in "{[":
        try:
            parsed = json.loads(sniff)
        except ValueError:
            return None
        if isinstance(parsed, (dict, list)):
            return "json"
    return None


#: Text document extensions used by the "session docs" feature (virtual All
#: list + materialize-on-save). Deliberately text-only: binary docs (.pdf,
#: .docx) don't fit the content-backed artifact model without extraction, so
#: they're excluded here for now.
DOC_EXTENSIONS = frozenset({".md", ".markdown", ".mdx", ".txt", ".rst"})


def is_document_path(path: str) -> bool:
    """True when ``path`` looks like a (text) document, not code/config/binary."""
    if not path:
        return False
    return os.path.splitext(path)[1].lower() in DOC_EXTENSIONS


# Literal-color detector backing the theme-contrast warning. Lives with the
# field rules (re-exported by kiro_crew.artifacts) so every artifact-authoring
# surface computes the SAME verdict:
# the gateway handlers stamp it on save/update responses, and the MCP tool
# phrases its own hint from it. Hex colors are 3/4/6/8 digits -- 5 and 7 are
# excluded on purpose so hex-ish CSS id selectors ("#added1") don't fire. The
# leading [:=(\s"'] anchors the literal to a value position (color:#111,
# fill="#111") rather than a fragment anchor or an id selector at line start.
# IGNORECASE is what lets RGB(...) / HSL(...) match -- CSS functions are
# case-insensitive. Fragment/URL hrefs (href="#abc") are excluded by
# stripping href attributes BEFORE scanning (see _HREF_ATTR_RE) rather than
# by a lookbehind: Python lookbehinds must be fixed-width, so a lookbehind
# cannot tolerate `href = "#abc"` spacing -- the strip is whitespace-tolerant
# and covers xlink:href and any case for free.
# Accepted noise, documented rather than parsed away: a whitespace-preceded
# hex-ish id selector ("... } #decade {") can still fire, but whitespace must
# stay in the prefix class or true positives like "border: 1px solid #ccc"
# are lost -- and every consumer surfaces this as a soft warning, never a
# rejection.
_HARDCODED_COLOR_RE = re.compile(
    r"[:=(\s\"']#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b" r"|\brgba?\(" r"|\bhsla?\(",
    re.IGNORECASE,
)

# href / xlink:href attribute (quoted value), whitespace-tolerant around the
# ``=``. An href value is a URL or fragment, never a rendered color, so it is
# removed before the color scan to keep the warning's false-positive rate low.
_HREF_ATTR_RE = re.compile(r"href\s*=\s*(\"[^\"]*\"|'[^']*')", re.IGNORECASE)


def has_unthemed_hardcoded_colors(kind: str, content: str) -> bool:
    """True when iframe-rendered content hardcodes its palette.

    Only widget/html kinds render inside the dashboard's themed iframe, so
    only they can clash with the injected theme defaults. Content carrying a
    single ``var(--`` reference is treated as theme-aware -- including the
    recommended fallback form ``color:var(--text,#111)`` -- and never flags.
    A full foreground/background *pairing* check needs a CSS parser; this
    zero-var heuristic catches the observed failure class (partially styled
    content clashing with the injected theme) with one regex.
    """
    if kind not in ("widget", "html"):
        return False
    if not content or "var(--" in content:
        return False
    return bool(_HARDCODED_COLOR_RE.search(_HREF_ATTR_RE.sub("href=x", content)))


def _infer_kind(content: str, source_path: str = "", explicit: str | None = None) -> str:
    """Infer an artifact ``kind`` when the caller didn't pin one.

    Resolution order (first match wins):

    1. **Explicit wins** — a non-empty ``explicit`` kind is returned unchanged
       (back-compat; the caller knows best). Validation happens downstream.
    2. **Extension** — for file-backed artifacts (``source_path`` set), map the
       file extension (``.md`` → ``markdown``, ``.json`` → ``json`` ...). An
       unknown extension on a real file falls back to ``text`` — a file on disk
       is far likelier plain text than a sandboxed widget.
    3. **Content sniff** — for inline content with no ``source_path``: HTML-ish
       markup (``<div``, ``<table``, an ``<mcwidget`` body ...) → ``widget``; a
       leading markdown heading or content with no HTML tags at all →
       ``markdown``; anything else falls back to the legacy ``widget`` default
       so ambiguous blobs keep their prior behavior.

    Only ``widget`` and ``markdown`` are inferred from inline content; the
    richer kinds (``svg`` / ``json`` / ``text``) require the extension signal.
    The returned value is not validated here — callers pass it through
    :func:`_validate_kind`.
    """
    if explicit:
        return explicit
    if source_path:
        ext = os.path.splitext(source_path)[1].lower()
        return _EXT_KIND_MAP.get(ext, "text")
    sniff = (content or "").lstrip()
    if not sniff:
        return "widget"  # empty inline content → keep the legacy default
    lowered = sniff.lower()
    if any(marker in lowered for marker in _HTML_SNIFF_MARKERS):
        return "widget"
    if _MD_HEADING_RE.match(sniff) or "<" not in sniff:
        return "markdown"
    return "widget"


def _markdown_misclassification_reason(content: str, source_path: str) -> str | None:
    """Return why a ``widget``-kind artifact actually looks like ``markdown``.

    Returns a short human-readable reason string, or ``None`` if the artifact
    is a genuine widget. Used by the corrective migration
    (:meth:`ArtifactStore.migrate_kinds`) to find markdown documents stored with
    kind ``widget``. Deliberately conservative — it triggers
    only on a ``.md`` / ``.markdown`` ``source_path`` or content with **no HTML
    tags at all**, so a genuine widget (whose content always contains HTML) is
    never reclassified. Only ever implies ``markdown``; the richer kinds are
    out of migration scope.
    """
    if source_path:
        ext = os.path.splitext(source_path)[1].lower()
        if ext in (".md", ".markdown"):
            return "source_path .md"
    if content and "<" not in content:
        return "no HTML tags"
    return None


def _validate_source(source: str) -> str:
    if source not in ALLOWED_SOURCES:
        raise ArtifactValidationError(
            f"invalid source {source!r}: must be one of {sorted(ALLOWED_SOURCES)}"
        )
    return source


def _strip_session_scope(key: str) -> str:
    """Canonicalize a session key so both spellings of ONE conversation match.

    The store's two provenance fields disagree by construction, and neither is
    wrong on its own:

    * ``Artifact.session_key`` is written from the dashboard's BARE slot key —
      ``chat-7-1785396512`` for a dashboard-born tab, ``slack_1785370133.085469``
      for a channel-born one — on the browser create path.
    * an event's ``session_id`` comes from the caller's FULL session key —
      ``dashboard:chat-7-1785396512``, or the channel's own
      ``slack:1785370133.085469`` — because MCP callers resolve identity through
      ``KIROCREW_SESSION_KEY`` / the signed pid sidecar, which are
      scope-qualified.

    A literal comparison therefore matches the origin case and *never* the event
    case — the involvement scope would silently degrade to the plain origin
    filter it exists to widen. Each family needs the fold that turns its session
    key into its slot name, because the slot name is what the origin field
    holds:

    * a dashboard slot's name is its session key minus the ``dashboard:`` scope.
    * a channel-born slot's name is its session key run through
      ``history._safe_key`` (see ``channel_slots.channel_slot_name``) — the same
      fold that names the transcript, which is why the tab and the thread share
      one file.

    The namespace itself is never dropped: ``slack:X`` canonicalizes to
    ``slack_X``, never to ``X``, so a channel conversation cannot collide with a
    dashboard slot. Keys with no second spelling to reconcile (``cron:``,
    ``subagent:``, ``taskrunner:``) are returned untouched.

    Both channel helpers are imported lazily because each one leads back to the
    artifact facade: ``kiro_crew.history`` and the ``kiro_crew.messaging``
    package (``messaging.driver`` -> ``acp`` -> ... -> ``members``) both import
    ``kiro_crew.artifacts``, which imports this module. A module-scope import here
    would re-enter the half-initialised facade and fail with an ImportError.
    """
    prefix = "dashboard:"
    if key.startswith(prefix):
        return key[len(prefix) :]
    from kiro_crew.history import _safe_key
    from kiro_crew.messaging.link import is_channel_session_key

    return _safe_key(key) if is_channel_session_key(key) else key


def _session_touched(art: "Artifact", session_key: str) -> bool:
    """True when ``session_key`` originated OR left any event on ``art``.

    "Touched" is the union of the two provenance records the store keeps:
    the create-time ``session_key`` field, and the ``session_id`` stamped on
    each lifecycle event (``created`` / ``edited`` / ``iterated`` /
    ``referenced`` / ``reverted``). That union is what makes the in-session
    Artifacts tab able to list what a session *consumed*, not only what it
    authored — a read records a ``referenced`` event and an agent edit records
    an ``iterated`` one, so both surface here without a second index.

    Both sides are compared scope-normalized (see ``_strip_session_scope``)
    because the two fields are persisted in different formats.

    Note the event log is FIFO-capped at ``MAX_EVENTS_PER_ARTIFACT``, so a
    very old association on a heavily-edited artifact can age out. That is
    acceptable for a recency-ordered panel and is why this is a best-effort
    "did this session touch it" predicate, not an audit-grade join.
    """
    if not session_key:
        return False
    want = _strip_session_scope(session_key)
    if art.session_key and _strip_session_scope(art.session_key) == want:
        return True
    return any(
        _strip_session_scope(str(e.get("session_id") or "")) == want
        for e in art.events
        if e.get("session_id")
    )


def _validate_tags(tags: list[str] | None) -> _List[str]:
    if tags is None:
        return []
    if not isinstance(tags, list):
        raise ArtifactValidationError(f"tags must be a list, got {type(tags).__name__}")
    if len(tags) > MAX_TAGS:
        raise ArtifactValidationError(f"too many tags ({len(tags)} > {MAX_TAGS})")
    cleaned: _List[str] = []
    for t in tags:
        if not isinstance(t, str):
            raise ArtifactValidationError(
                f"invalid tag {t!r}: a tag must be a string, got {type(t).__name__}"
            )
        try:
            canonical = normalize_tag(t)
        except ValueError as exc:
            raise ArtifactValidationError(f"invalid tag {t!r}: {exc}") from None
        if canonical not in cleaned:  # preserve order; one label keeps one spelling
            cleaned.append(canonical)
    return cleaned


def _validate_name(name: str) -> str:
    if not isinstance(name, str):
        raise ArtifactValidationError(f"name must be str, got {type(name).__name__}")
    name = name.strip()
    if not name:
        raise ArtifactValidationError("name is required")
    if len(name) > MAX_NAME_LEN:
        raise ArtifactValidationError(f"name exceeds {MAX_NAME_LEN} chars")
    return name


def _validate_description(description: str | None) -> str:
    if description is None:
        return ""
    if not isinstance(description, str):
        raise ArtifactValidationError(f"description must be str, got {type(description).__name__}")
    if len(description) > MAX_DESCRIPTION_LEN:
        raise ArtifactValidationError(f"description exceeds {MAX_DESCRIPTION_LEN} chars")
    return description


def _validate_source_path(value: str | None, field_name: str = "source_path") -> str:
    """Validate a filesystem-pointer field (``source_path`` / ``source_root``).

    REJECTS an over-long value instead of truncating it. Silent truncation
    (``source_path[:512]``) turns a too-long-but-valid path into a shorter path
    that points somewhere else — practically always somewhere that doesn't
    exist. The artifact then looks file-backed while its live read can never
    succeed. Failing the save is the honest outcome: the caller learns
    immediately instead of the user discovering a hollow artifact later.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ArtifactValidationError(f"{field_name} must be str, got {type(value).__name__}")
    if len(value) > MAX_SOURCE_PATH_LEN:
        raise ArtifactValidationError(
            f"{field_name} exceeds {MAX_SOURCE_PATH_LEN} chars "
            f"({len(value)}); refusing to truncate a filesystem path"
        )
    return value
