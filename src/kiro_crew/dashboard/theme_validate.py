"""Theme-pack validation & parsing core (non-HTTP).

Pure, handler-free building blocks for the installable theme-pack subsystem:
the CSS tokenizer, every ``_validate_*`` routine, all ``_THEME_*`` constants
(caps, enums, regexes, denylists, CSP strings), the read-only asset descriptor
builder, and the path/slug helpers. The HTTP handlers live in
``kiro_crew.dashboard.handlers.themes`` and import from here; this module must
never import the handler layer (one-way dependency, no import cycles).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from kiro_crew.config.loader import config_dir
from kiro_crew.hooks import safe_read_file_bytes_nolink

# ── Custom Themes — validation & parsing core ──


_THEMES_DIR_NAME = "themes"
_THEME_NAME_MAX_LEN = 60
_THEME_SLUG_MAX_LEN = 40
_THEME_EMOJI_MAX_LEN = 4
_THEME_DEFAULT_EMOJI = "🎨"
_THEME_REQUIRED_VARS = ("--bg", "--text", "--accent")

# CSS variables that constitute a complete theme definition.
_THEME_CSS_VARS = (
    "--bg",
    "--bg-accent",
    "--bg-elevated",
    "--bg-hover",
    "--card",
    "--card-fg",
    "--card-hl",
    "--panel",
    "--panel-strong",
    "--chrome",
    "--text",
    "--text-strong",
    "--muted",
    "--muted-strong",
    "--border",
    "--border-strong",
    "--border-hover",
    "--accent",
    "--accent-hover",
    "--accent-subtle",
    "--accent-glow",
    "--ring",
    "--ok",
    "--ok-subtle",
    "--warn",
    "--warn-subtle",
    "--danger",
    "--danger-subtle",
    "--info",
    "--aim",
    "--aim-subtle",
    "--clarify",
    "--clarify-subtle",
    "--diff-add",
    "--diff-add-text",
    "--diff-del",
    "--diff-del-text",
    "--diff-hunk",
    "--diff-hunk-text",
    "--diff-meta-text",
    "--shadow-sm",
    "--shadow-md",
    "--shadow-lg",
)


def _themes_dir() -> Path:
    """Return the custom themes directory under config_dir()."""
    return config_dir() / _THEMES_DIR_NAME


# Positive allowlist: only characters that appear in legitimate CSS color,
# shadow, and length values.  This blocks semicolons, braces, backslashes,
# angle brackets, quotes, at-signs, colons, and everything else that could
# escape the CSS declaration context.
_CSS_VALUE_ALLOWED_RE = re.compile(r"^[a-zA-Z0-9#(),.\- %/]+$")

# Function denylist for dangerous CSS functions whose individual characters
# pass the allowlist above (e.g. url(), expression(), image(), image-set()).
_CSS_DANGEROUS_FUNC_RE = re.compile(
    r"url\s*\(|expression\s*\(|image\s*\(|image-set\s*\(",
    re.IGNORECASE,
)

# Set of allowed CSS variable names (mirrors frontend ALLOWED_CSS_VARS).
_THEME_CSS_VARS_SET: frozenset[str] = frozenset(_THEME_CSS_VARS)


def _sanitize_css_value(value: str) -> str | None:
    """Validate a single CSS value using a positive character allowlist.

    Returns the trimmed value if safe, or None if rejected.
    """
    if not isinstance(value, str):
        return None
    if len(value) > 200:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if not _CSS_VALUE_ALLOWED_RE.match(trimmed):
        return None
    if _CSS_DANGEROUS_FUNC_RE.search(trimmed):
        return None
    return trimmed


def _validate_theme_data(data: dict) -> str | None:
    """Validate a theme JSON object. Returns error string or None.

    Validates keys against ``_THEME_CSS_VARS_SET`` allowlist.
    Unknown keys are rejected.
    """
    if not isinstance(data, dict):
        return "theme must be a JSON object"
    name = data.get("name", "")
    if not isinstance(name, str):
        return "name must be a string"
    name = name.strip()
    if not name:
        return "name is required"
    if len(name) > _THEME_NAME_MAX_LEN:
        return f"name too long (max {_THEME_NAME_MAX_LEN} chars)"
    emoji = data.get("emoji", "")
    if not isinstance(emoji, str):
        return "emoji must be a string"
    for mode in ("dark", "light"):
        mode_data = data.get(mode, {})
        if not isinstance(mode_data, dict):
            return f"'{mode}' must be a JSON object"
        for required_var in _THEME_REQUIRED_VARS:
            if required_var not in mode_data:
                return f"'{mode}' is missing required" f" variable '{required_var}'"
        for key, val in mode_data.items():
            if key not in _THEME_CSS_VARS_SET:
                return f"'{mode}' key '{key}' is not a recognized theme variable"
            if _sanitize_css_value(val) is None:
                return f"'{mode}' variable '{key}' has an invalid value"
    return None


def _strip_to_allowed_vars(mode_data: dict[str, str]) -> dict[str, str]:
    """Return only the allowed CSS vars with sanitized values.

    Defense-in-depth: even after validation, re-filter before writing
    so only known variables with clean values reach disk.
    """
    result: dict[str, str] = {}
    for key, val in mode_data.items():
        if key not in _THEME_CSS_VARS_SET:
            continue
        clean = _sanitize_css_value(val)
        if clean is not None:
            result[key] = clean
    return result


def _slugify_theme_name(name: str) -> str:
    """Convert a theme name to a filesystem-safe slug."""
    slug = re.sub(r"[^a-z0-9\-]", "-", name.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug[:_THEME_SLUG_MAX_LEN] or "custom"


def _safe_theme_slug(slug: str) -> str | None:
    """Return the slug if it is filesystem-safe (no traversal), else None.

    Mirrors the inline guard in ``api_theme_detail`` so install/remove share
    one path-traversal check.
    """
    if not isinstance(slug, str) or not slug:
        return None
    cleaned = re.sub(r"[^a-z0-9\-]", "", slug)
    if not cleaned or cleaned != slug:
        return None
    return cleaned


# ── Installed theme directories (Level 0) ──
#
# Editor-created custom themes are flat ``<slug>.json`` files under
# ``_themes_dir()``.  *Installed* themes (from a local folder or GitHub) are
# directories ``_themes_dir()/<slug>/`` holding a ``theme.json`` manifest plus
# a ``variables.json`` (top-level or under ``styles/``).  A file ``<slug>.json``
# and a directory ``<slug>/`` never collide, so the two stores sit side by side.
#
# Level 0 (Color) only: the installer accepts colour variables and rejects any
# Level 1/2 payload (branding, fonts, overrides.css, overlays, topbar, audio,
# persona) — the declared ``level`` must be 0 AND no higher-tier asset may be
# present.  Validation is defensive by construction: bounded entry count, a
# per-file and total size cap, a filename allowlist, symlink rejection, and a
# containment check so nothing resolves outside the theme directory.

_THEME_MANIFEST_NAME = "theme.json"
# variables.json may live at the top level or under styles/ (relative POSIX).
_THEME_VARIABLES_REL = ("variables.json", "styles/variables.json")
# VCS/meta files tolerated (not counted, not rejected, not copied into the store)
# so a cloned repo or a folder carrying LICENSE validates cleanly.
_THEME_META_IGNORE = frozenset(
    {".git", ".github", ".gitignore", ".ds_store", "license", "license.md", "license.txt"}
)
_THEME_MAX_FILE_BYTES = 64 * 1024  # generic JSON cap (theme.json / variables.json)
# GitHub install: https-only, host allowlist, argv (no shell), bounded time.
_THEME_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_THEME_CLONE_TIMEOUT_SEC = 30

# Manifest schema version. A pack declares ``formatVersion`` (integer) in
# theme.json; this is the newest major the installer understands. A pack
# declaring a higher major is rejected with an honest "needs a newer KiroCrew"
# message rather than an opaque schema error (one-way-door schema guard).
_THEME_FORMAT_VERSION = 1

# ── Capability tiers (0 color · 1 branded · 2 experience) ──
_THEME_MAX_LEVEL = 2
# Allowed sub-directories -> minimum level that unlocks them.
_THEME_ALLOWED_DIRS = {
    "styles": 0,
    "styles/fonts": 1,
    "branding": 1,
    "overlays": 2,
    "topbar": 2,
    "audio": 2,
}
# Per-level ceilings (entry count + total uncompressed bytes, §6.2).
_THEME_ENTRIES_BY_LEVEL = {0: 32, 1: 64, 2: 160}
_THEME_TOTAL_BYTES_BY_LEVEL = {0: 256 * 1024, 1: 2 * 1024 * 1024, 2: 5 * 1024 * 1024}
_THEME_MAX_FONTS = 3
_THEME_FONT_ROLES: frozenset[str] = frozenset({"sans", "mono", "system"})
_THEME_MAX_OVERLAYS = 5
_THEME_PERSONA_MAX_CHARS = 2000
_THEME_BOTNAME_MAX = 48  # branding bot-name display cap (plain text)
# Per-file size caps by category (bytes), §4.1.
_THEME_FILE_CAPS = {
    "manifest": 16 * 1024,
    "variables": 64 * 1024,
    "readme": 32 * 1024,
    "overrides": 100 * 1024,
    "font": 512 * 1024,
    "logo": 100 * 1024,
    "favicon": 50 * 1024,
    "wordmark": 100 * 1024,
    "preview": 512 * 1024,
    "overlay": 200 * 1024,
    "topbar": 100 * 1024,
    "audio_manifest": 16 * 1024,
    "audio": 512 * 1024,
    "audio_ambient": 2 * 1024 * 1024,
    "persona": 8 * 1024,
}
# overrides.css install-time denylist (§4.2). The positive selector allowlist is
# applied at runtime scoping; install rejects the dangerous patterns + the
# explicitly forbidden selectors.
_THEME_CSS_DENY_RE = re.compile(
    r"@import|expression\s*\(|javascript:|-moz-binding|"
    r"url\s*\(\s*['\"]?\s*(?:https?:)?//",
    re.IGNORECASE,
)
# Evasion normalization for the denylist: a hand-rolled
# denylist must see what the BROWSER sees, or a pack smuggles a forbidden token
# past it. Browsers strip CSS comments and decode `\`-escapes during tokenization,
# so `ur/**/l(` and `\75 rl(` both become `url(`. We reproduce exactly those two
# normalizations (comment strip + escape decode) and run the denylist on the
# normalized text too — NOT a full CSS parse (that is a separate CSSOM rework),
# just the minimal decode needed so the known evasion families can't hide the
# tokens.
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_ESCAPE_RE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})\s?|(.))", re.DOTALL)


def _decode_css_escapes(s: str) -> str:
    """Decode CSS escape sequences (``\\75`` → ``u``, ``\\.`` → the char) as a
    browser tokenizer would, so the denylist sees the real tokens."""
    def _repl(m: "re.Match[str]") -> str:
        if m.group(1) is not None:
            try:
                return chr(int(m.group(1), 16))
            except (ValueError, OverflowError):
                return ""
        return m.group(2) or ""
    return _CSS_ESCAPE_RE.sub(_repl, s)


def _css_denylist_normalize(s: str) -> str:
    """Strip comments + decode escapes — the two browser-tokenizer steps a pack
    could exploit to hide a denylisted token (external url(), expression(), …)."""
    return _decode_css_escapes(_CSS_COMMENT_RE.sub("", s))


_THEME_CSS_FORBIDDEN = ("iframe", "script", "[data-auth]", ".token", ".credential", "#app-root")
# overlay/topbar HTML install-time denylist (§5.2 step 5) — ADVISORY / defense-in-
# depth only. It is deliberately shallow (misses backtick/alias/sendBeacon/etc.
# forms) and is NOT the security boundary: overlay/topbar HTML is served under
# _THEME_OVERLAY_CSP and rendered in a sandbox="allow-scripts" (no-same-origin)
# iframe, which is what actually neutralises external script/fetch/cookie access
# at runtime. This just rejects the obvious mistakes at install time.
_THEME_HTML_DENY_RE = re.compile(
    r"<script[^>]*\bsrc\s*=|document\.cookie|\blocalStorage\b|\bsessionStorage\b|"
    r"\bXMLHttpRequest\b|\bfetch\s*\(\s*['\"]?(?:https?:)?//",
    re.IGNORECASE,
)

# ── Overlay / topbar theme.json declarations (§3.1) ──
# Declarations are OPTIONAL: a theme.json with no ``overlays``/``topbar`` keys
# still validates and behaves exactly as before (filesystem-derived placement).
# When present, they let a pack pin placement/behaviour instead of inheriting
# the hardcoded defaults below.
_THEME_OVERLAY_ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")
# Closed position enum (LOCKED — doc gives examples only).
_THEME_OVERLAY_POSITIONS = frozenset(
    {
        "top", "bottom", "left", "right",
        "top-left", "top-right", "bottom-left", "bottom-right",
        "center", "fullscreen",
    }
)
_THEME_OVERLAY_ANIMATIONS = frozenset({"continuous", "once", "none"})
_THEME_OVERLAY_TRIGGER_RE = re.compile(r"^(continuous|activate|idle-[0-9]{1,3}s)$")
_THEME_OVERLAY_DEFAULT_POSITION = "fullscreen"
_THEME_OVERLAY_DEFAULT_ZINDEX = 40
_THEME_OVERLAY_MAX_ZINDEX = 9999
_THEME_OVERLAY_DEFAULT_ANIMATION = "continuous"
_THEME_OVERLAY_DEFAULT_TRIGGER = "continuous"
# A pack HTML ``src`` names a single ``.html`` file, optionally prefixed with its
# subdir (``overlays/foo.html`` or ``foo.html``); resolved under that subdir.
_THEME_PACK_HTML_SRC_RE = re.compile(r"^(?:(overlays|topbar)/)?([a-z0-9_-]{1,64})\.html$")
_THEME_TOPBAR_HEIGHT_RE = re.compile(r"^([0-9]{1,4})(px|rem|em)$")
_THEME_TOPBAR_DEFAULT_HEIGHT = "28px"
# A topbar is a thin strip, never a viewport-consuming surface. The regex bounds
# syntax (≤4 digits); this bounds MAGNITUDE so a pack cannot declare e.g.
# "9999rem" and turn its pointer-enabled sandboxed topbar iframe into a
# full-viewport interaction-intercepting / UI-redress surface — the same
# containment class the overrides.css validator already enforces (viewport-
# covering position:fixed rules and z-index > _THEME_OVERLAY_MAX_ZINDEX are
# rejected). px-equivalent ceiling; rem/em approximated at 16px per unit.
_THEME_TOPBAR_MAX_PX = 200


def _topbar_height_ok(height: Any) -> bool:
    """True iff ``height`` is a valid topbar height string AND its magnitude
    stays within the thin-strip ceiling (px-equivalent ≤ ``_THEME_TOPBAR_MAX_PX``).
    Rejects viewport-consuming values that would breach topbar containment.
    """
    if not isinstance(height, str):
        return False
    m = _THEME_TOPBAR_HEIGHT_RE.fullmatch(height)
    if not m:
        return False
    px = int(m.group(1)) * (1 if m.group(2) == "px" else 16)
    return px <= _THEME_TOPBAR_MAX_PX


# ── Audio manifest (audio/manifest.json, §3.3) ──
# 7-name trigger taxonomy; the per-trigger duration caps (seconds) bound how
# long a one-shot cue may run. ``ambient`` is the looping bed (no upper cap).
_THEME_AUDIO_TRIGGERS = frozenset(
    {
        "activate", "deactivate", "message-sent", "message-received",
        "error", "notification", "ambient",
    }
)
_THEME_AUDIO_TRIGGER_CAPS = {
    "activate": 5,
    "deactivate": 2,
    "message-sent": 1,
    "message-received": 1,
    "error": 3,
    "notification": 2,
}
_THEME_AUDIO_SRC_RE = re.compile(r"^(?:audio/)?([a-z0-9_-]{1,64}\.(?:mp3|ogg|wav))$")


def _installed_theme_dir(slug: str) -> Path:
    """Directory for an *installed* theme: ``_themes_dir()/<slug>/``."""
    return _themes_dir() / slug


def _read_json_file(path: Path, max_bytes: int) -> tuple[Any, str | None]:
    """Read + parse a JSON file with a byte cap. Returns ``(data, error)``."""
    try:
        raw = path.read_bytes()
    except OSError as e:
        return None, f"cannot read {path.name}: {e}"
    if len(raw) > max_bytes:
        return None, f"{path.name} too large (max {max_bytes} bytes)"
    try:
        return json.loads(raw.decode("utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, f"{path.name} is not valid JSON: {e}"


def _sniff_audio(head: bytes) -> bool:
    """Lightweight magic-byte check that ``head`` looks like MP3/OGG/WAV.

    Anti-polyglot: rejects files that don't start with a known audio signature.
    """
    if len(head) < 4:
        return False
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return True  # MP3
    if head[:4] == b"OggS":
        return True  # OGG
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return True  # WAV
    return False


def _classify_theme_file(rel: str) -> tuple[str | None, int]:
    """Map a lowercased relative POSIX file path to ``(category, min_level)``.

    Returns ``(None, 0)`` for an unrecognized path (caller rejects it).
    """
    if rel == "theme.json":
        return "manifest", 0
    if rel in ("variables.json", "styles/variables.json"):
        return "variables", 0
    if rel == "readme.md":
        return "readme", 0
    if rel == "styles/overrides.css":
        return "overrides", 1
    if rel == "persona.md":
        return "persona", 2
    if rel == "audio/manifest.json":
        return "audio_manifest", 2
    parts = rel.split("/")
    ext = rel.rsplit(".", 1)[-1] if "." in rel else ""
    top = parts[0]
    if top == "styles" and len(parts) == 3 and parts[1] == "fonts" and ext in ("woff2", "ttf"):
        return "font", 1
    if top == "branding" and len(parts) == 2:
        stem = parts[1].rsplit(".", 1)[0]
        if stem == "logo" and ext in ("svg", "png"):
            return "logo", 1
        if stem == "favicon" and ext in ("ico", "png", "svg"):
            return "favicon", 1
        if stem == "wordmark" and ext in ("svg", "png"):
            return "wordmark", 1
        if stem == "preview" and ext in ("png", "webp"):
            return "preview", 1
    if top == "overlays" and len(parts) == 2 and ext == "html":
        return "overlay", 2
    if top == "topbar" and len(parts) == 2 and parts[1] in ("dark.html", "light.html"):
        return "topbar", 2
    if top == "audio" and len(parts) == 2 and ext in ("mp3", "ogg", "wav"):
        stem = parts[1].rsplit(".", 1)[0]
        return ("audio_ambient" if stem == "ambient" else "audio"), 2
    return None, 0


# overrides.css layout denylist (§4.2/§5.1). Decorative full-bleed pseudo-
# elements are exempt from the interaction/viewport-cover rejections — this is
# the decorative-scanline idiom (``body::before,body::after{position:fixed;
# inset:0;pointer-events:none}``), inert decoration that MUST still validate.
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_THEME_DECORATIVE_PSEUDO = frozenset(
    {"body::before", "body::after", "body:before", "body:after"}
)
_CSS_ZERO_VALUES = frozenset(
    {"0", "0px", "0rem", "0em", "0%", "0vh", "0vw", "0vmin", "0vmax"}
)


def _css_skip_string(text: str, i: int) -> int:
    """Return the index just past a quoted string starting at ``text[i]``.

    Handles backslash escapes; an unterminated string runs to end-of-text.
    """
    quote = text[i]
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == quote:
            return i + 1
        i += 1
    return i


def _css_skip_url(text: str, i: int) -> int:
    """Return the index just past a ``url(...)`` token starting at ``text[i]``.

    Assumes ``text[i:i+4].lower() == 'url('``. Balances nested parens and skips
    quoted strings inside so that braces/semicolons within a (possibly
    data-URI) argument are treated as opaque.
    """
    n = len(text)
    i += 4  # past 'url('
    depth = 1
    while i < n and depth > 0:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c in "\"'":
            i = _css_skip_string(text, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return i


def _css_read_block_body(text: str, i: int) -> tuple[str, int]:
    """Read a brace block body starting just after its opening ``{``.

    ``i`` is the index of the first char inside the block (brace depth 1).
    Returns ``(body_text, index_after_close)``; string/url/nested-brace aware so
    a ``}`` inside a string or ``url()`` does not close the block.
    """
    n = len(text)
    start = i
    depth = 1
    while i < n:
        c = text[i]
        if c in "\"'":
            i = _css_skip_string(text, i)
            continue
        if (c in "uU") and text[i:i + 4].lower() == "url(":
            i = _css_skip_url(text, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    return text[start:i], i


def _scan_css_blocks(text: str):
    """Yield ``(prelude, body)`` for each top-level ``{...}`` block in ``text``.

    ``prelude`` is the raw text since the previous block/brace; ``body`` is the
    raw block content. String/url/comment-stripped text is expected; braces
    inside strings or ``url()`` are ignored so splitting happens on real
    top-level braces only.
    """
    i = 0
    n = len(text)
    prelude_start = 0
    while i < n:
        c = text[i]
        if c in "\"'":
            i = _css_skip_string(text, i)
            continue
        if (c in "uU") and text[i:i + 4].lower() == "url(":
            i = _css_skip_url(text, i)
            continue
        if c == "{":
            prelude = text[prelude_start:i]
            body, i = _css_read_block_body(text, i + 1)
            prelude_start = i
            yield prelude, body
            continue
        if c == "}":
            # Stray/unbalanced close brace — reset the prelude and move on.
            i += 1
            prelude_start = i
            continue
        i += 1


def _css_has_top_level_brace(text: str) -> bool:
    """True if ``text`` contains a ``{`` outside of strings/``url()``."""
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            i = _css_skip_string(text, i)
            continue
        if (c in "uU") and text[i:i + 4].lower() == "url(":
            i = _css_skip_url(text, i)
            continue
        if c == "{":
            return True
        i += 1
    return False


def _css_split_top_level(text: str, sep: str) -> list[str]:
    """Split ``text`` on ``sep`` occurrences at the top nesting level.

    Skips strings, ``url()`` args, and any ``{}``/``[]``/``()`` nesting so that
    a separator inside a quoted string or a function/bracket group does not
    split. Used for both selector-group (``,``) and declaration (``;``)
    splitting so both are string-aware.
    """
    parts: list[str] = []
    i = 0
    n = len(text)
    start = 0
    depth = 0
    while i < n:
        c = text[i]
        if c in "\"'":
            i = _css_skip_string(text, i)
            continue
        if (c in "uU") and text[i:i + 4].lower() == "url(":
            i = _css_skip_url(text, i)
            continue
        if c in "{[(":
            depth += 1
            i += 1
            continue
        if c in "}])":
            if depth > 0:
                depth -= 1
            i += 1
            continue
        if c == sep and depth == 0:
            parts.append(text[start:i])
            i += 1
            start = i
            continue
        i += 1
    parts.append(text[start:i])
    return parts


def _iter_css_rules(text: str):
    """Yield ``(selectors, decls)`` for each flat CSS rule in ``text``.

    Comments are stripped first. ``selectors`` is the comma-split, lowercased
    selector group; ``decls`` is a list of ``(prop, value)`` with prop
    lowercased and value lowered + stripped of a trailing ``!important``.

    The tokenizer is a string-aware state machine: it splits on real top-level
    braces only, treating ``{``/``}``/``;`` inside quoted strings and ``url()``
    (e.g. data-URIs) as opaque — so a value like ``content:"}"`` no longer
    truncates a rule, and legit values containing braces are not false-rejected.
    At-rule groups whose body contains nested rules (``@media``) are flattened:
    their inner rules are yielded and the group prelude itself is not (matching
    the prior naive parser, which only ever surfaced leaf rules).
    """
    stripped = _CSS_COMMENT_RE.sub(" ", text)
    yield from _iter_css_rules_level(stripped)


def _iter_css_rules_level(text: str):
    for prelude, body in _scan_css_blocks(text):
        if _css_has_top_level_brace(body):
            # At-rule group (e.g. @media): recurse into its nested rules and do
            # not emit the group prelude, mirroring the old flat parser.
            yield from _iter_css_rules_level(body)
            continue
        selectors = [s.strip().lower() for s in _css_split_top_level(prelude, ",") if s.strip()]
        if not selectors:
            continue
        decls: list[tuple[str, str]] = []
        for chunk in _css_split_top_level(body, ";"):
            if ":" not in chunk:
                continue
            prop, _, val = chunk.partition(":")
            prop = prop.strip().lower()
            val = val.strip().lower()
            if val.endswith("!important"):
                val = val[: -len("!important")].strip()
            if prop and val:
                decls.append((prop, val))
        yield selectors, decls


def _css_len_ge(val: str, unit: str, threshold: float) -> bool:
    """True if ``val`` is ``<n><unit>`` with ``n >= threshold`` (e.g. 50vw)."""
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)" + unit, val.strip())
    return m is not None and float(m.group(1)) >= threshold


def _overrides_layout_violation(
    selectors: list[str], decls: list[tuple[str, str]]
) -> str | None:
    """Return an error if a single overrides.css rule breaks the layout denylist.

    A rule whose selector group is ENTIRELY decorative pseudo-elements
    (``body::before`` / ``body::after``) is exempt from the pointer-events:none
    and viewport-covering position:fixed rejections.
    """
    decorative = all(s in _THEME_DECORATIVE_PSEUDO for s in selectors)
    d: dict[str, str] = {}
    for prop, val in decls:
        d[prop] = val  # last-wins within a block, mirroring the CSS cascade
    if "z-index" in d:
        m = re.search(r"-?[0-9]+", d["z-index"])
        if m and int(m.group(0)) > _THEME_OVERLAY_MAX_ZINDEX:
            return f"overrides.css sets z-index above {_THEME_OVERLAY_MAX_ZINDEX}"
    if d.get("display") == "none":
        return "overrides.css uses display:none (hides functional elements)"
    if d.get("pointer-events") == "none" and not decorative:
        return "overrides.css uses pointer-events:none on a non-decorative selector"
    if d.get("position") == "fixed":
        covers = False
        if "inset" in d and all(tok in _CSS_ZERO_VALUES for tok in d["inset"].split()):
            covers = True
        elif all(k in d and d[k] in _CSS_ZERO_VALUES for k in ("top", "right", "bottom", "left")):
            covers = True
        elif _css_len_ge(d.get("width", ""), "vw", 50):
            covers = True
        elif _css_len_ge(d.get("height", ""), "vh", 50):
            covers = True
        # A viewport-covering fixed layer is only permitted as an INERT decorative
        # pseudo-element — i.e. decorative AND pointer-events:none (the scanline
        # idiom). A decorative body::before/::after WITHOUT pointer-events:none
        # would be a full-viewport CLICK-INTERCEPTOR in the main (non-sandboxed,
        # non-consent-gated) document — the UI-redress vector the exemption must
        # not open. Non-decorative viewport-covering fixed stays rejected too.
        if covers and not (decorative and d.get("pointer-events") == "none"):
            return "overrides.css uses a viewport-covering position:fixed rule"
    return None


def _validate_overrides_css(text: str) -> str | None:
    """Install-time denylist for ``overrides.css`` (§4.2).

    Two layers: (1) the injection denylist (@import / external url() /
    expression() / javascript: / -moz-binding) and forbidden selectors, then
    (2) a per-rule *layout* denylist (§4.2/§5.1) that rejects rules which could
    hijack the viewport or block interaction — z-index>9999, display:none,
    pointer-events:none, and viewport-covering position:fixed — with an
    exemption for purely decorative ``body::before``/``body::after`` pseudo-
    elements (the decorative-scanline idiom).
    """
    if _THEME_CSS_DENY_RE.search(text) or _THEME_CSS_DENY_RE.search(
        _css_denylist_normalize(text)
    ):
        return (
            "overrides.css uses a forbidden pattern (@import / external url() / "
            "expression() / javascript: / -moz-binding)"
        )
    low = text.lower()
    for sel in _THEME_CSS_FORBIDDEN:
        if sel in low:
            return f"overrides.css targets a forbidden selector: {sel}"
    for selectors, decls in _iter_css_rules(text):
        violation = _overrides_layout_violation(selectors, decls)
        if violation:
            return violation
    return None


def _validate_overlay_html(text: str, name: str) -> str | None:
    """Advisory install-time HTML denylist for overlays/topbar (§5.2 step 5).

    Defense-in-depth only — the runtime CSP + sandboxed iframe are the real
    control (see the _THEME_HTML_DENY_RE note). Rejects the obvious mistakes.
    """
    if _THEME_HTML_DENY_RE.search(text):
        return (
            f"'{name}' uses a forbidden pattern (external <script src>, fetch/XHR "
            "to a URL, or cookie/localStorage/sessionStorage access)"
        )
    return None


def _validate_persona(text: str) -> str | None:
    """Persona-file bounds (§6.5)."""
    if len(text) > _THEME_PERSONA_MAX_CHARS:
        return f"persona.md too long (max {_THEME_PERSONA_MAX_CHARS} chars)"
    low = text.lower()
    if "drop" not in low or "persona" not in low:
        return "persona.md must include an explicit 'drop persona on request' instruction"
    if "security" not in low and "accuracy" not in low:
        return "persona.md must state that security/accuracy override the persona"
    return None


def _walk_theme_entries(root: Path):
    """Yield theme dir/file paths, streaming, never following directory symlinks.

    Replaces ``sorted(root.rglob("*"))`` which buffered the entire tree up front
    (and could be walked into via a symlinked directory / cycle before any cap
    fired). ``os.walk(followlinks=False)`` streams so the caller's entry-count
    cap bounds the work, and meta directories (``.git`` …) are pruned so we
    never descend into them. Directories are yielded before their contents so
    the caller's per-dir level/allowlist gate still runs first.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        kept: list[str] = []
        for d in sorted(dirnames):
            rel = (dp / d).relative_to(root).as_posix().lower()
            top = rel.split("/", 1)[0]
            if top in _THEME_META_IGNORE or rel in _THEME_META_IGNORE:
                continue  # skip + don't descend into meta dirs (.git, .github)
            kept.append(d)
            yield dp / d
        dirnames[:] = kept  # prune descent to accepted dirs only
        for f in sorted(filenames):
            yield dp / f


def _resolve_pack_html_src(src: Any, theme_dir: Path, subdir: str) -> tuple[str | None, str | None]:
    """Resolve a declared overlay/topbar HTML ``src`` to a rel path under ``subdir``.

    Accepts ``<name>.html`` or ``<subdir>/<name>.html``; returns
    ``("<subdir>/<name>.html", None)`` when the file exists in the pack, else an
    error. Rejects unsafe / mismatched-subdir / missing files.
    """
    if not isinstance(src, str) or not src.strip():
        return None, "'src' is required"
    m = _THEME_PACK_HTML_SRC_RE.fullmatch(src.strip().lower())
    if not m:
        return None, f"invalid 'src' (expected {subdir}/<name>.html)"
    prefix, stem = m.group(1), m.group(2)
    if prefix is not None and prefix != subdir:
        return None, f"'src' must live under {subdir}/"
    rel = f"{subdir}/{stem}.html"
    if not (theme_dir / rel).is_file():
        return None, f"'src' references a missing file: {rel}"
    return rel, None


def _validate_overlay_decls(manifest: dict[str, Any], theme_dir: Path) -> str | None:
    """Validate the OPTIONAL ``overlays`` list in theme.json (§3.1).

    Absent key -> no-op (filesystem-derived behaviour is preserved). When
    present: a list of <=5 entries, each ``{id, src, ...}`` where ``id`` is a
    unique ``^[a-z0-9-]{1,64}$`` token equal to the ``src`` file stem (so the
    ``/overlay/{id}`` route serves it) and the optional placement/behaviour
    fields respect their enums/bounds.
    """
    overlays = manifest.get("overlays")
    if overlays is None:
        return None
    if not isinstance(overlays, list):
        return "theme.json 'overlays' must be a list"
    if len(overlays) > _THEME_MAX_OVERLAYS:
        return f"too many overlay declarations (max {_THEME_MAX_OVERLAYS})"
    seen: set[str] = set()
    for entry in overlays:
        if not isinstance(entry, dict):
            return "each overlay declaration must be an object"
        oid = entry.get("id")
        if not isinstance(oid, str) or not _THEME_OVERLAY_ID_RE.fullmatch(oid):
            return "overlay 'id' must match ^[a-z0-9-]{1,64}$"
        if oid in seen:
            return f"duplicate overlay id: {oid}"
        seen.add(oid)
        rel, err = _resolve_pack_html_src(entry.get("src"), theme_dir, "overlays")
        if err:
            return f"overlay '{oid}': {err}"
        if rel != f"overlays/{oid}.html":
            return f"overlay '{oid}' 'src' must be overlays/{oid}.html"
        pos = entry.get("position", _THEME_OVERLAY_DEFAULT_POSITION)
        if pos not in _THEME_OVERLAY_POSITIONS:
            return f"overlay '{oid}' has invalid position: {pos!r}"
        z = entry.get("zIndex", _THEME_OVERLAY_DEFAULT_ZINDEX)
        if not isinstance(z, int) or isinstance(z, bool) or not (0 <= z <= _THEME_OVERLAY_MAX_ZINDEX):
            return f"overlay '{oid}' zIndex must be an int 0..{_THEME_OVERLAY_MAX_ZINDEX}"
        pe = entry.get("pointerEvents", False)
        if not isinstance(pe, bool):
            return f"overlay '{oid}' pointerEvents must be a boolean"
        anim = entry.get("animation", _THEME_OVERLAY_DEFAULT_ANIMATION)
        if anim not in _THEME_OVERLAY_ANIMATIONS:
            return f"overlay '{oid}' has invalid animation: {anim!r}"
        trig = entry.get("trigger", _THEME_OVERLAY_DEFAULT_TRIGGER)
        if not isinstance(trig, str) or not _THEME_OVERLAY_TRIGGER_RE.fullmatch(trig):
            return f"overlay '{oid}' has invalid trigger: {trig!r}"
    return None


def _validate_topbar_decls(manifest: dict[str, Any], theme_dir: Path) -> str | None:
    """Validate the OPTIONAL ``topbar`` object in theme.json (§3.1).

    Absent key -> no-op. When present: ``dark``/``light`` optional strings that
    must resolve to existing topbar files, ``height`` a ``^[0-9]{1,4}(px|rem|
    em)$`` string, ``hideOnMobile`` a boolean.
    """
    topbar = manifest.get("topbar")
    if topbar is None:
        return None
    if not isinstance(topbar, dict):
        return "theme.json 'topbar' must be an object"
    for mode in ("dark", "light"):
        if mode in topbar:
            _rel, err = _resolve_pack_html_src(topbar.get(mode), theme_dir, "topbar")
            if err:
                return f"topbar '{mode}': {err}"
    height = topbar.get("height", _THEME_TOPBAR_DEFAULT_HEIGHT)
    if not _topbar_height_ok(height):
        return (
            "topbar 'height' must match ^[0-9]{1,4}(px|rem|em)$ and stay within "
            f"{_THEME_TOPBAR_MAX_PX}px (px-equivalent) — a topbar is a thin strip"
        )
    if "hideOnMobile" in topbar and not isinstance(topbar.get("hideOnMobile"), bool):
        return "topbar 'hideOnMobile' must be a boolean"
    return None


def _resolve_audio_src(src: Any, theme_dir: Path) -> tuple[str | None, str | None]:
    """Resolve a declared audio ``src`` to ``audio/<file>`` if present + valid.

    Accepts ``<file>`` or ``audio/<file>`` (mp3/ogg/wav); the file must exist
    under ``audio/`` and pass the magic-byte sniff.
    """
    if not isinstance(src, str) or not src.strip():
        return None, "'src' is required"
    m = _THEME_AUDIO_SRC_RE.fullmatch(src.strip().lower())
    if not m:
        return None, "invalid audio 'src' (expected audio/<name>.mp3|ogg|wav)"
    rel = f"audio/{m.group(1)}"
    fpath = theme_dir / rel
    if not fpath.is_file():
        return None, f"'src' references a missing file: {rel}"
    try:
        with fpath.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return None, f"cannot read {rel}"
    if not _sniff_audio(head):
        return None, f"{rel} is not a valid audio file"
    return rel, None


def _validate_audio_manifest(theme_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Parse + validate the OPTIONAL ``audio/manifest.json`` (§3.3).

    Returns ``(audio_desc, None)`` on success, ``(None, None)`` when no manifest
    file exists (backward-compat: ``hasAudio`` stays False, no ``desc['audio']``),
    or ``(None, error)`` on a malformed manifest. ``audio_desc`` has shape
    ``{"triggers": {<name>: {src, volume, maxDuration}}, "ambient": {...}|None}``.
    Unknown top-level keys are tolerated (older sample manifests carry
    ``version``/``sounds``), so an existing pack keeps validating.
    """
    mpath = theme_dir / "audio" / "manifest.json"
    if not mpath.is_file():
        return None, None
    data, err = _read_json_file(mpath, _THEME_FILE_CAPS["audio_manifest"])
    if err:
        return None, err
    if not isinstance(data, dict):
        return None, "audio/manifest.json must be a JSON object"
    out: dict[str, Any] = {"triggers": {}, "ambient": None}

    triggers = data.get("triggers")
    if triggers is not None:
        if not isinstance(triggers, dict):
            return None, "audio 'triggers' must be an object"
        for tname, tval in triggers.items():
            if tname not in _THEME_AUDIO_TRIGGERS:
                return None, f"unknown audio trigger: {tname!r}"
            if not isinstance(tval, dict):
                return None, f"audio trigger '{tname}' must be an object"
            rel, e = _resolve_audio_src(tval.get("src"), theme_dir)
            if e:
                return None, f"audio trigger '{tname}': {e}"
            vol = tval.get("volume", 1.0)
            if not isinstance(vol, (int, float)) or isinstance(vol, bool) or not (0 <= vol <= 1):
                return None, f"audio trigger '{tname}' volume must be a number 0..1"
            cap = _THEME_AUDIO_TRIGGER_CAPS.get(tname)  # None => ambient (uncapped)
            md = tval.get("maxDuration")
            if md is None:
                md = cap if cap is not None else 0  # 0 == unlimited (ambient trigger)
            else:
                if not isinstance(md, (int, float)) or isinstance(md, bool) or md <= 0:
                    return None, f"audio trigger '{tname}' maxDuration must be > 0"
                if cap is not None and md > cap:
                    return None, f"audio trigger '{tname}' maxDuration exceeds cap ({cap}s)"
            out["triggers"][tname] = {
                "src": rel,
                "volume": float(vol),
                "maxDuration": md,
            }

    ambient = data.get("ambient")
    if ambient is not None:
        if not isinstance(ambient, dict):
            return None, "audio 'ambient' must be an object"
        rel, e = _resolve_audio_src(ambient.get("src"), theme_dir)
        if e:
            return None, f"audio ambient: {e}"
        vol = ambient.get("volume", 1.0)
        if not isinstance(vol, (int, float)) or isinstance(vol, bool) or not (0 <= vol <= 1):
            return None, "audio ambient volume must be a number 0..1"
        loop = ambient.get("loop", True)
        if not isinstance(loop, bool):
            return None, "audio ambient loop must be a boolean"
        fade = ambient.get("fadeIn", 0)
        if not isinstance(fade, (int, float)) or isinstance(fade, bool) or fade < 0:
            return None, "audio ambient fadeIn must be a number >= 0"
        out["ambient"] = {
            "src": rel,
            "volume": float(vol),
            "loop": loop,
            "fadeIn": float(fade),
        }
    return out, None


def _validate_theme_dir(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Validate an installed theme **directory** (structure + data) for L0/L1/L2.

    On success returns ``(summary, None)`` where ``summary`` is the record to
    register (``slug``/``name``/``emoji``/``level``/``source``/``dark``/
    ``light``); on any failure returns ``(None, error)``. The declared ``level``
    in ``theme.json`` gates which asset tiers may be present, and it must bound
    the actual payload (no higher-tier asset than the declared level). Reuses
    ``_validate_theme_data`` for the colour values so the 43-var allowlist and
    per-value CSS sanitisation match editor-created themes.
    """
    if not path.is_dir() or path.is_symlink():
        return None, "theme path is not a directory"
    root = path.resolve()

    # Manifest first — the declared level gates the rest of the walk.
    manifest_path = path / _THEME_MANIFEST_NAME
    if not manifest_path.is_file():
        return None, "missing theme.json manifest"
    manifest, err = _read_json_file(manifest_path, _THEME_FILE_CAPS["manifest"])
    if err:
        return None, err
    if not isinstance(manifest, dict):
        return None, "theme.json must be a JSON object"
    # formatVersion FIRST (before any other schema check) so a pack authored
    # for a newer KiroCrew gets the honest version message, not an opaque
    # downstream schema error. Must be a positive integer; a major above the
    # supported one is a forward-incompatible pack.
    fmt = manifest.get("formatVersion")
    if not isinstance(fmt, int) or isinstance(fmt, bool):
        return None, (
            f'theme.json must declare "formatVersion" '
            f"(integer; current version is {_THEME_FORMAT_VERSION})"
        )
    if fmt > _THEME_FORMAT_VERSION:
        return None, (
            f"this pack requires a newer version of Kiro Crew "
            f"(pack formatVersion {fmt}, supported {_THEME_FORMAT_VERSION})"
        )
    if fmt < 1:
        return None, 'theme.json "formatVersion" must be a positive integer (>= 1)'
    level = manifest.get("level", 0)
    if (
        not isinstance(level, int)
        or isinstance(level, bool)
        or not (0 <= level <= _THEME_MAX_LEVEL)
    ):
        return None, f"theme.json 'level' must be 0, 1, or 2 (got {level!r})"
    name = manifest.get("name", "")
    if not isinstance(name, str) or not name.strip():
        return None, "theme.json 'name' is required"
    emoji = manifest.get("emoji", _THEME_DEFAULT_EMOJI)
    if not isinstance(emoji, str):
        return None, "theme.json 'emoji' must be a string"

    max_entries = _THEME_ENTRIES_BY_LEVEL.get(level, 160)
    max_total = _THEME_TOTAL_BYTES_BY_LEVEL.get(level, 5 * 1024 * 1024)

    # Walk the tree: reject symlinks / escaping paths, skip meta, classify every
    # entry, gate by declared level, enforce per-file + total caps + count caps,
    # and run content checks on the security-sensitive categories.
    total = 0
    entries = 0
    fonts = 0
    overlays = 0
    for entry in _walk_theme_entries(path):
        rel = entry.relative_to(path).as_posix().lower()
        top = rel.split("/", 1)[0]
        if top in _THEME_META_IGNORE or rel in _THEME_META_IGNORE:
            continue
        entries += 1
        if entries > max_entries:
            return None, f"too many files in theme (max {max_entries})"
        if entry.is_symlink():
            return None, f"symlinks are not allowed: {entry.name}"
        try:
            entry.resolve().relative_to(root)
        except (OSError, ValueError):
            return None, "path escapes theme directory"
        if entry.is_dir():
            if rel not in _THEME_ALLOWED_DIRS:
                return None, f"unexpected directory in theme: '{rel}'"
            if _THEME_ALLOWED_DIRS[rel] > level:
                return None, (
                    f"'{rel}/' requires level {_THEME_ALLOWED_DIRS[rel]}; "
                    f"theme declares level {level}"
                )
            continue
        category, min_level = _classify_theme_file(rel)
        if category is None:
            return None, f"unexpected file in theme: '{rel}'"
        if min_level > level:
            return None, f"'{rel}' is a Level {min_level} asset; theme declares level {level}"
        cap = _THEME_FILE_CAPS.get(category, _THEME_MAX_FILE_BYTES)
        try:
            sz = entry.stat().st_size
        except OSError:
            return None, f"cannot stat '{rel}'"
        if sz > cap:
            return None, f"'{rel}' too large (max {cap} bytes)"
        total += sz
        if category == "font":
            fonts += 1
            if fonts > _THEME_MAX_FONTS:
                return None, f"too many fonts (max {_THEME_MAX_FONTS})"
        elif category == "overlay":
            overlays += 1
            if overlays > _THEME_MAX_OVERLAYS:
                return None, f"too many overlays (max {_THEME_MAX_OVERLAYS})"
        # Security-sensitive content checks.
        if category == "overrides":
            c_err = _validate_overrides_css(entry.read_text(encoding="utf-8", errors="replace"))
            if c_err:
                return None, c_err
        elif category in ("overlay", "topbar"):
            c_err = _validate_overlay_html(
                entry.read_text(encoding="utf-8", errors="replace"), rel
            )
            if c_err:
                return None, c_err
        elif category == "persona":
            c_err = _validate_persona(entry.read_text(encoding="utf-8", errors="replace"))
            if c_err:
                return None, c_err
        elif category in ("audio", "audio_ambient"):
            try:
                with entry.open("rb") as fh:
                    head = fh.read(16)
            except OSError:
                return None, f"cannot read '{rel}'"
            if not _sniff_audio(head):
                return None, f"'{rel}' is not a valid audio file"
    if total > max_total:
        return None, f"theme too large (max {max_total} bytes total)"

    # OPTIONAL manifest declarations (§3.1/§3.3). Each returns None when its key
    # is absent, so a pack that declares nothing (e.g. the bikini sample) keeps
    # validating purely from its on-disk layout.
    ov_err = _validate_overlay_decls(manifest, path)
    if ov_err:
        return None, ov_err
    tb_err = _validate_topbar_decls(manifest, path)
    if tb_err:
        return None, tb_err
    _audio_desc, au_err = _validate_audio_manifest(path)
    if au_err:
        return None, au_err

    # Variables (required at every level) — reuse the colour-data validator.
    var_path: Path | None = None
    for var_rel in _THEME_VARIABLES_REL:
        cand = path / var_rel
        if cand.is_file():
            var_path = cand
            break
    if var_path is None:
        return None, "missing variables.json (top-level or under styles/)"
    variables, err = _read_json_file(var_path, _THEME_FILE_CAPS["variables"])
    if err:
        return None, err
    if not isinstance(variables, dict):
        return None, "variables.json must be a JSON object"
    theme_data = {
        "name": name.strip()[:_THEME_NAME_MAX_LEN],
        "emoji": emoji,
        "dark": variables.get("dark", {}),
        "light": variables.get("light", {}),
    }
    data_err = _validate_theme_data(theme_data)
    if data_err:
        return None, data_err

    raw_slug = manifest.get("slug")
    slug = _slugify_theme_name(
        raw_slug if isinstance(raw_slug, str) and raw_slug.strip() else name
    )
    return {
        "slug": slug,
        "name": theme_data["name"],
        "emoji": emoji.strip()[:_THEME_EMOJI_MAX_LEN] or _THEME_DEFAULT_EMOJI,
        "level": level,
        "source": "installed",
        "dark": _strip_to_allowed_vars(variables.get("dark", {})),
        "light": _strip_to_allowed_vars(variables.get("light", {})),
    }, None


def _theme_asset_descriptor(
    theme_dir: Path, manifest: dict[str, Any], level: int, *, installing: bool = False
) -> dict[str, Any]:
    """Build the frontend asset descriptor for an installed L1/L2 theme.

    Read-only: references only files that actually exist on disk and classify
    at/below ``level``. Font families and the bot name are sanitised to
    CSS/text-safe tokens; a manifest that names a missing/oversized asset simply
    yields no entry (the loader degrades gracefully). L0 themes return ``{}``.

    When *installing* is True the descriptor is being built as part of a
    theme-pack install.  Unknown font ``role`` values raise ``ValueError``
    instead of silently coercing to ``"sans"`` so the pack author gets early
    feedback.  The read path (installing=False) stays lenient for forward
    compatibility with packs written for newer Kiro Crew versions.
    """
    desc: dict[str, Any] = {}
    if level < 1:
        return desc

    # Branding — bot name (plain text) + logo/favicon/wordmark by file presence.
    branding = manifest.get("branding")
    branding = branding if isinstance(branding, dict) else {}
    bres: dict[str, str] = {}
    bot = branding.get("botName")
    if isinstance(bot, str):
        bot = bot.strip()[:_THEME_BOTNAME_MAX]
        if bot and bot.isprintable():
            bres["botName"] = bot
    for role, exts in (
        ("logo", ("svg", "png")),
        ("favicon", ("ico", "png", "svg")),
        ("wordmark", ("svg", "png")),
    ):
        for ext in exts:
            rel = f"branding/{role}.{ext}"
            cat, min_level = _classify_theme_file(rel)
            if cat is not None and min_level <= level and (theme_dir / rel).is_file():
                bres[role] = rel
                break
    if bres:
        desc["branding"] = bres

    # Fonts — validated @font-face specs (family token + existing woff2 file).
    fonts = manifest.get("fonts")
    out_fonts: list[dict[str, Any]] = []
    if isinstance(fonts, list):
        for f in fonts[:_THEME_MAX_FONTS]:
            if not isinstance(f, dict):
                continue
            fam, file = f.get("family"), f.get("file")
            if not isinstance(fam, str) or not isinstance(file, str):
                continue
            fam = fam.strip()
            if not re.fullmatch(r"[A-Za-z0-9 _-]{1,40}", fam):
                continue
            file_l = file.lower()
            if re.sub(r"[^a-z0-9._-]", "", file_l) != file_l or not file_l.endswith(
                (".woff2", ".ttf")
            ):
                continue
            rel = f"styles/fonts/{file_l}"
            if not (theme_dir / rel).is_file():
                continue
            weight = f.get("weight", 400)
            if not isinstance(weight, int) or isinstance(weight, bool) or not (100 <= weight <= 900):
                weight = 400
            style = f.get("style") if f.get("style") in ("normal", "italic") else "normal"
            fmt = "truetype" if file_l.endswith(".ttf") else "woff2"
            role = f.get("role", "sans")
            if not isinstance(role, str) or role not in _THEME_FONT_ROLES:
                if installing:
                    raise ValueError(
                        f"font '{fam}' declares unknown role {role!r}; "
                        f"valid roles are: {', '.join(sorted(_THEME_FONT_ROLES))}"
                    )
                role = "sans"
            out_fonts.append(
                {"family": fam, "src": rel, "weight": weight, "style": style, "format": fmt, "role": role}
            )
    if out_fonts:
        desc["fonts"] = out_fonts

    if (theme_dir / "styles" / "overrides.css").is_file():
        desc["hasOverrides"] = True

    if level >= 2:
        overlays_dir = theme_dir / "overlays"
        declared = manifest.get("overlays")
        overlays_out: list[dict[str, Any]] = []
        if isinstance(declared, list) and declared:
            # Manifest-declared placement/behaviour (§3.1). ``id`` == file stem,
            # so ``/overlay/{id}`` still serves it; defaults fill any omitted
            # field. Entries were validated at install; re-check defensively.
            for entry in declared[:_THEME_MAX_OVERLAYS]:
                if not isinstance(entry, dict):
                    continue
                oid = entry.get("id")
                if not isinstance(oid, str) or not _THEME_OVERLAY_ID_RE.fullmatch(oid):
                    continue
                if not (overlays_dir / f"{oid}.html").is_file():
                    continue
                pos = entry.get("position")
                z = entry.get("zIndex")
                anim = entry.get("animation")
                trig = entry.get("trigger")
                overlays_out.append(
                    {
                        "id": oid,
                        "position": (
                            pos if pos in _THEME_OVERLAY_POSITIONS
                            else _THEME_OVERLAY_DEFAULT_POSITION
                        ),
                        "zIndex": (
                            z if isinstance(z, int) and not isinstance(z, bool)
                            and 0 <= z <= _THEME_OVERLAY_MAX_ZINDEX
                            else _THEME_OVERLAY_DEFAULT_ZINDEX
                        ),
                        "pointerEvents": bool(entry.get("pointerEvents", False)),
                        "animation": (
                            anim if anim in _THEME_OVERLAY_ANIMATIONS
                            else _THEME_OVERLAY_DEFAULT_ANIMATION
                        ),
                        "trigger": (
                            trig if isinstance(trig, str)
                            and _THEME_OVERLAY_TRIGGER_RE.fullmatch(trig)
                            else _THEME_OVERLAY_DEFAULT_TRIGGER
                        ),
                    }
                )
        elif overlays_dir.is_dir():
            # No declaration -> today's glob behaviour, emitted with a uniform
            # shape (default fullscreen placement) so the loader sees one type.
            stems = sorted(
                p.stem
                for p in overlays_dir.glob("*.html")
                if p.is_file() and _safe_theme_slug(p.stem.lower()) == p.stem.lower()
            )
            for stem in stems[:_THEME_MAX_OVERLAYS]:
                overlays_out.append(
                    {
                        "id": stem,
                        "position": _THEME_OVERLAY_DEFAULT_POSITION,
                        "zIndex": _THEME_OVERLAY_DEFAULT_ZINDEX,
                        "pointerEvents": False,
                        "animation": _THEME_OVERLAY_DEFAULT_ANIMATION,
                        "trigger": _THEME_OVERLAY_DEFAULT_TRIGGER,
                    }
                )
        if overlays_out:
            desc["overlays"] = overlays_out

        # Topbar — dark/light presence from files; height/hideOnMobile from the
        # optional manifest declaration (defaults otherwise).
        tb_manifest = manifest.get("topbar")
        tb_manifest = tb_manifest if isinstance(tb_manifest, dict) else {}
        dark_present = (theme_dir / "topbar" / "dark.html").is_file()
        light_present = (theme_dir / "topbar" / "light.html").is_file()
        if tb_manifest or dark_present or light_present:
            height = tb_manifest.get("height")
            if not _topbar_height_ok(height):
                height = _THEME_TOPBAR_DEFAULT_HEIGHT
            desc["topbar"] = {
                "dark": dark_present,
                "light": light_present,
                "height": height,
                "hideOnMobile": bool(tb_manifest.get("hideOnMobile", False)),
            }

        # Audio — keep the ``hasAudio`` flag (manifest file presence) and add the
        # parsed trigger/ambient map when the manifest is well-formed.
        if (theme_dir / "audio" / "manifest.json").is_file():
            desc["hasAudio"] = True
        audio_desc, _au_err = _validate_audio_manifest(theme_dir)
        if audio_desc is not None:
            desc["audio"] = audio_desc
        persona_path = theme_dir / "persona.md"
        if persona_path.is_file():
            desc["hasPersona"] = True
            # Surface persona hash + text so the frontend can key user consent
            # to this exact content. Persona is <=2000 chars,
            # so including the full text is cheap; only expose it when it still
            # passes the install-time persona validation.
            # Read persona.md through the hooks chokepoint, NOT read_text: the
            # installed-theme dir is influenced, and a plain reopen after the
            # validation walk leaves a symlink-swap window (persona.md -> a short
            # sensitive file like ~/.aws/credentials, exposed via personaInfo.text).
            # safe_read_file_bytes_nolink opens O_NOFOLLOW, pins the fstat/inode,
            # requires the opened fd's real path to stay inside theme_dir, and
            # rejects sensitive paths — returning None on any of those, in which
            # case personaInfo is simply not surfaced (consent can't be keyed, so
            # no persona is injected: fail-safe).
            _pbytes = safe_read_file_bytes_nolink(
                str(persona_path), within_root=str(theme_dir)
            )
            _ptext = (
                _pbytes.decode("utf-8", errors="replace")
                if _pbytes is not None
                else None
            )
            if _ptext is not None and _validate_persona(_ptext) is None:
                desc["personaInfo"] = {
                    "sha256": hashlib.sha256(_ptext.encode("utf-8")).hexdigest(),
                    "chars": len(_ptext),
                    "text": _ptext,
                }
    return desc


_THEME_ASSET_CT = {
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".json": "application/json",
    ".css": "text/css",
}
# ``sandbox`` (no allow-same-origin) makes a DIRECT top-level navigation to an
# overlay/topbar URL run in an opaque origin — pack script can never execute in
# the dashboard origin (same stored-XSS class as the static-asset CSP below).
# The intended embedding (<iframe sandbox="allow-scripts">) is unaffected: the
# frame is already opaque-origin, and ``allow-scripts`` here keeps its inline
# script working under the response CSP too.
_THEME_OVERLAY_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; media-src 'self'; connect-src 'none'; form-action 'none'; "
    "sandbox allow-scripts"
)
# Static theme assets (fonts/images/audio/css/json) are inert data, but an
# installed SVG opened as a TOP-LEVEL document would otherwise inherit the app's
# permissive base CSP and could run inline <script>/onload= in the dashboard
# origin (stored XSS via a direct asset link). Serve every static asset under a
# maximally-restrictive CSP: no script, no network, fully sandboxed. This only
# governs direct-document loads — embedding as <img>/<link>/@font-face/<audio>
# is unaffected.
_THEME_ASSET_CSP = "default-src 'none'; sandbox"


def _resolve_theme_asset(slug: str, subpath: str) -> tuple[Path | None, str | None]:
    """Resolve an installed-theme file by (slug, subpath), containment-checked.

    Returns ``(path, None)`` for an existing regular file inside the theme dir,
    or ``(None, error)``. Rejects unsafe slugs, absolute/`..` paths, symlinks,
    and anything resolving outside the theme directory.
    """
    safe = _safe_theme_slug(slug)
    if not safe:
        return None, "invalid theme slug"
    if not subpath or subpath.startswith("/") or ".." in subpath.split("/"):
        return None, "invalid path"
    base = _installed_theme_dir(safe).resolve()
    if not base.is_dir():
        return None, "not found"
    target = (base / subpath).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None, "invalid path"
    if not target.is_file() or target.is_symlink():
        return None, "not found"
    return target, None


def _read_theme_text(target: Path) -> str:
    return target.read_text(encoding="utf-8", errors="replace")
