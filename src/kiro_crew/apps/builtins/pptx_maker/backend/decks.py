"""PPTX Maker — deck discovery and per-deck detail.

Pure filesystem reads over the deck root the engine writes into. A deck
directory looks like this (the engine's layout, not ours):

    <deck-id>/
      deck.json                  # optional; carries a display name
      specs/brief.md             # phase-1 deliverables, produced in order
      specs/outline.md
      specs/art-direction.html
      slides/<slug>.json         # one per slide, once composed
      compose/<slug>_<epoch>.json  # render payloads, newest epoch wins
      compose/defs_<epoch>.json    # shared SVG defs for the deck
      preview/page<N>-*.png      # optional rasterized thumbnails
      output.pptx                # the finished file

Everything here is BLOCKING (``iterdir``/``glob``/``stat``/small file reads over
a directory that can hold hundreds of decks) and must be called through
``routes.off_loop``.

URLs returned to the browser are always ``preview/...`` paths relative to this
app's API base — never absolute filesystem paths — so the frontend cannot be
handed something it could ask the server to open outside the deck root. The two
absolute paths that ARE returned (``dirPath``/``pptxPath``) exist only to feed
the dashboard's own reveal/open endpoint, which re-validates them.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew import hooks
from kiro_crew.apps.builtins.pptx_maker.backend import paths
from kiro_crew.hooks import FileTooLargeError
from kiro_crew.security import redact

logger = logging.getLogger("kirocrew.app.pptx-maker")

# How much of a deck's brief is carried in the LIST response. Enough for the
# sidebar's search-and-preview, small enough that listing 200 decks stays cheap.
BRIEF_PREVIEW_CHARS = 2000

# Hard cap on decks returned by one listing. A deck root is user-controlled and
# can accumulate indefinitely; without a cap one request would stat every
# directory in it.
MAX_DECKS = 500

# Deck-directory filename grammar (the engine's, matched not built).
_EPOCH_RE = re.compile(r"_(\d+)\.json$")
_SLUG_EPOCH_RE = re.compile(r"^(.+)_(\d+)\.json$")
_PAGE_RE = re.compile(r"^page(\d+)[-.]")
_OUTLINE_SLUG_RE = re.compile(r"^-\s*\[([a-z0-9-]+)\]")

# Deliverable docs surfaced as viewer tabs, in the order the engine produces
# them. Each entry is (response key, candidate filenames under specs/).
_SPEC_DOCS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("brief", ("brief.md",)),
    ("outline", ("outline.md",)),
    ("artDirection", ("art-direction.html", "art-direction.md")),
)

_NAME_FILES = ("deck.json", "presentation.json")


@dataclass
class DeckSummary:
    """One row in the deck list."""

    deck_id: str
    name: str
    slide_count: int
    thumbnail_url: str | None = None
    pptx_url: str | None = None
    brief: str = ""

    def to_dict(self) -> dict:
        return {
            "deckId": self.deck_id,
            "name": self.name,
            "slideCount": self.slide_count,
            "thumbnailUrl": self.thumbnail_url,
            "pptxUrl": self.pptx_url,
            "brief": self.brief,
        }


@dataclass
class SlideRef:
    """One slide's render payload + optional rasterized preview."""

    slug: str
    preview_url: str | None = None
    compose_url: str | None = None

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "previewUrl": self.preview_url,
            "composeUrl": self.compose_url,
        }


@dataclass
class DeckDetail:
    """Everything the viewer needs for one deck."""

    deck_id: str
    name: str
    defs_url: str | None = None
    pptx_url: str | None = None
    dir_path: str = ""
    pptx_path: str | None = None
    specs: dict[str, str] = field(default_factory=dict)
    updated_at: dict[str, float] = field(default_factory=dict)
    slides: list[SlideRef] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "deckId": self.deck_id,
            "name": self.name,
            "defsUrl": self.defs_url,
            "pptxUrl": self.pptx_url,
            "dirPath": self.dir_path,
            "pptxPath": self.pptx_path,
            "specs": self.specs,
            "updatedAt": self.updated_at,
            "slides": [s.to_dict() for s in self.slides],
        }


def _preview_url(deck_id: str, *rel: str) -> str | None:
    """A deck-artifact URL relative to this app's API base, or ``None``.

    Returns ``None`` when ANY segment looks like a credential. Every segment here is
    agent-authored — the deck id, the slide slug, and the engine's preview/compose
    FILENAMES — and a `SEGMENT_RE`-legal name can spell an `AKIA…` key, so an
    unscreened segment reached the dashboard inside the URL itself. That has now been
    the same defect three times over (`deckId`, then the slide slug, then the preview
    filename), which is why the check lives HERE, at the single point every artifact
    URL is built, instead of at the seven call sites: a fourth caller cannot reintroduce
    it.

    Refused rather than redacted, for the reason `paths.resolve_deck_dir` refuses a
    credential-shaped deck id: these segments are real path components the browser
    sends back, so a scrubbed URL would resolve to nothing. Each caller drops the one
    affected link and keeps the rest of the deck usable.
    """
    segments = (deck_id, *rel)
    if any(redact(segment) != segment for segment in segments):
        logger.warning("pptx-maker: refused an artifact URL whose segment looks like a credential")
        return None
    return "preview/" + "/".join(segments)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _read_deck_text(path: Path, deck_dir: Path) -> str | None:
    """Read a file inside *deck_dir*, pinned to the inode opened, or ``None``.

    The ONE reader for every text file this module takes out of the deck tree —
    `deck.json`, `specs/brief.md`, `specs/outline.md`. They are agent-written inside a
    user-configurable root, and `paths.contained_deck_file` stops a SYMLINK but cannot
    stop a HARDLINK: a hardlink has no target to resolve, `is_symlink()` is False, and
    `resolve()` returns the path itself, so every path-based check passes. So
    `os.link("~/.ssh/config", "specs/brief.md")` previewed SSH configuration onto the
    Deck list, which every visit to the app loads. Verified before this guard.

    `hooks.safe_read_file_bytes_nolink` rejects `st_nlink > 1` on the OPENED descriptor
    (its documented R30 F1 case) and, with `within_root`, requires that descriptor's
    real path to sit inside the deck dir — neither of which a path check can express.

    Centralized deliberately. This exact defect has now been reported three times in
    this module, each time one reader over from the last fix, so a per-reader patch is
    what keeps failing; a single chokepoint means a fourth reader cannot reintroduce it.
    `errors="replace"` preserves each caller's existing degrade-not-raise behaviour.
    """
    try:
        raw = hooks.safe_read_file_bytes_nolink(str(path), within_root=str(deck_dir))
    except FileTooLargeError:
        return None
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


def _deck_name(deck_dir: Path) -> str:
    """The deck's display name from its own metadata, else the directory name.

    Every candidate goes through :func:`paths.contained_deck_file`, for the same
    reason :func:`_read_brief` does: these files are agent-written inside a
    user-configurable deck root, so any of them can be a SYMLINK that
    ``read_text`` would follow. Fixing the brief and not this reader would have
    left the identical leak one field over — the metadata's ``name`` is rendered
    on the Deck list just as the brief preview is.
    """
    for filename in _NAME_FILES:
        candidate = paths.contained_deck_file(deck_dir, filename)
        if candidate is None:
            continue
        text = _read_deck_text(candidate, deck_dir)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("name"):
            # The metadata file is written by the agent, so its `name` is model
            # output on its way to the dashboard: redact before it leaves
            # (AUTOSDE `backend-security-controls`).
            return redact(str(data["name"]))
    # The FALLBACK is agent-controlled too, and needs the same pass. The engine
    # creates these directories from the model's own name for the deck, so a deck
    # with no `deck.json` yet — every specs-only deck in progress — surfaced its
    # directory name to the Deck list raw. Redacting one source and not the other
    # left the identical leak one branch over, which is the same mistake the brief
    # and metadata readers each already record.
    return redact(deck_dir.name)


def _read_brief(deck_dir: Path) -> str:
    """The deck's brief preview, or "" when there is none.

    Resolved through :func:`paths.contained_deck_file` rather than read straight
    off ``deck_dir / "specs" / "brief.md"``. The deck root is user-configurable and
    its contents are agent-written, so ``brief.md`` can be a SYMLINK — and
    ``read_text`` follows it. A link to ``~/.ssh/config`` would have had its
    contents previewed straight onto the Deck list, which every visit to the app
    loads. The shared helper resolves symlinks and applies the sensitive-path gate,
    which is the same check every other file this app serves already goes through.
    """
    brief_path = paths.contained_deck_file(deck_dir, "specs", "brief.md")
    if brief_path is None:
        return ""
    raw = _read_deck_text(brief_path, deck_dir)
    if raw is None:
        return ""
    # `brief.md` is authored by the agent from the user's conversation, so the
    # preview is untrusted text bound for the dashboard. Redact BEFORE the slice
    # so a credential cannot straddle the truncation boundary and survive.
    return redact(raw)[:BRIEF_PREVIEW_CHARS]


def _first_thumbnail(deck_dir: Path) -> str | None:
    preview_dir = paths.contained_deck_dir(deck_dir, "preview")
    if preview_dir is None:
        return None
    try:
        pngs = sorted(preview_dir.glob("*.png"))
    except OSError:
        return None
    if not pngs:
        return None
    return _preview_url(deck_dir.name, "preview", pngs[0].name)


def list_decks() -> list[dict]:
    """Every deck under the deck root, newest id first.

    A deck is listed once it has ANY content — composed slides, a ``deck.json``,
    or a ``specs/`` dir — so an in-progress deck (brief written, slides not yet
    generated) appears while the agent is still working on it. The engine creates
    the directory before writing anything, so a bare empty dir is skipped.

    BLOCKING — call through ``off_loop``.
    """
    root = paths.deck_root()
    if not root.is_dir():
        return []
    out: list[DeckSummary] = []
    try:
        entries = sorted(root.iterdir())
    except OSError as exc:
        logger.debug("pptx-maker: deck root unreadable: %s", exc)
        return []
    for entry in entries:
        if len(out) >= MAX_DECKS:
            break
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        # Re-resolve through the SAME containment gate every other deck reader uses,
        # rather than trusting the directory entry.
        #
        # `iterdir()` yields `{root}/{name}` unresolved, and `is_dir()` FOLLOWS symlinks —
        # so a link in the deck root pointing at a deck-shaped directory elsewhere passed
        # this check, and every reader below then took the LINK as its own containment
        # root. `_read_brief` and `_first_thumbnail` are bounded relative to the deck dir
        # they are handed, so they stayed "inside" a deck whose real location was outside
        # the root entirely — and the brief's contents were served to the dashboard.
        # Demonstrated: a `20260101-linked -> /elsewhere/20260101-secret` symlink listed
        # the deck and returned its brief verbatim, while `resolve_deck_dir` on the same
        # name correctly answered `None`.
        #
        # The fix is to ask that authority instead of re-deriving the answer here: it
        # validates the name against `SEGMENT_RE`, resolves the path, and requires the
        # result to be strictly inside the root. Same lesson as the Papyrus readers —
        # *derived* does not mean *contained*.
        deck_dir = paths.resolve_deck_dir(entry.name)
        if deck_dir is None:
            logger.debug("pptx-maker: skipped uncontained deck entry %r", entry.name)
            continue
        slides_dir = paths.contained_deck_dir(deck_dir, "slides")
        try:
            slide_count = len(list(slides_dir.glob("*.json"))) if slides_dir else 0
        except OSError:
            slide_count = 0
        has_content = (
            slide_count > 0
            or paths.contained_deck_file(deck_dir, "deck.json") is not None
            or paths.contained_deck_dir(deck_dir, "specs") is not None
        )
        if not has_content:
            continue
        out.append(
            DeckSummary(
                deck_id=deck_dir.name,
                name=_deck_name(deck_dir),
                slide_count=slide_count,
                thumbnail_url=_first_thumbnail(deck_dir),
                pptx_url=(
                    _preview_url(deck_dir.name, "output.pptx")
                    if (deck_dir / "output.pptx").is_file()
                    else None
                ),
                brief=_read_brief(deck_dir),
            )
        )
    # The engine names decks with a timestamp prefix, so a reverse id sort puts
    # the newest first — which is what the user is working on.
    out.sort(key=lambda d: d.deck_id, reverse=True)
    return [d.to_dict() for d in out]


def _index_compose_dir(compose_dir: Path | None) -> tuple[dict[str, str], str | None]:
    """Latest-epoch compose file per slug, plus the latest shared defs file.

    ``None`` when the ``compose`` directory is absent or not contained (a symlink out
    of the deck), which reads the same as an empty deck rather than raising.

    The engine writes a NEW ``<slug>_<epoch>.json`` on every recompose rather
    than overwriting, so picking the highest epoch is what makes the preview
    show the current render instead of the first one.
    """
    by_slug: dict[str, tuple[int, str]] = {}
    defs_file: str | None = None
    defs_epoch = -1
    if compose_dir is None:
        return {}, None
    try:
        names = os.listdir(compose_dir)
    except OSError:
        return {}, None
    for name in names:
        if not name.endswith(".json"):
            continue
        if name.startswith("defs_"):
            match = _EPOCH_RE.search(name)
            epoch = int(match.group(1)) if match else 0
            if epoch > defs_epoch:
                defs_epoch, defs_file = epoch, name
            continue
        match = _SLUG_EPOCH_RE.match(name)
        if not match:
            continue
        slug, epoch = match.group(1), int(match.group(2))
        current = by_slug.get(slug)
        if current is None or epoch > current[0]:
            by_slug[slug] = (epoch, name)
    return {slug: name for slug, (_, name) in by_slug.items()}, defs_file


def _slide_order(deck_dir: Path) -> list[str]:
    """Slide slugs in presentation order.

    The outline is authoritative — it is what the user approved — and the
    on-disk slide files are the fallback for a deck built without one.
    """
    # Contained like the other two deck readers: agent-written file, user-chosen
    # root, so it can be a symlink `read_text` would follow. The slugs derived from
    # it name preview URLs, so a foreign file's contents would surface too.
    outline = paths.contained_deck_file(deck_dir, "specs", "outline.md")
    slugs: list[str] = []
    if outline is not None:
        text = _read_deck_text(outline, deck_dir)
        for line in (text or "").splitlines():
            match = _OUTLINE_SLUG_RE.match(line)
            if match:
                slugs.append(match.group(1))
    if slugs:
        return slugs
    slides_dir = paths.contained_deck_dir(deck_dir, "slides")
    if slides_dir is None:
        return []
    try:
        return [p.stem for p in sorted(slides_dir.glob("*.json"))]
    except OSError:
        return []


def _previews_by_page(deck_dir: Path) -> dict[int, str]:
    """Rasterized preview filenames keyed by 1-based page number."""
    preview_dir = paths.contained_deck_dir(deck_dir, "preview")
    out: dict[int, str] = {}
    if preview_dir is None:
        return out
    try:
        names = os.listdir(preview_dir)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".png"):
            continue
        match = _PAGE_RE.match(name)
        if match:
            out[int(match.group(1))] = name
    return out


def deck_detail(deck_id: str) -> dict | None:
    """One deck's full detail, or ``None`` when *deck_id* names no deck.

    The ``updatedAt`` map is what lets the viewer auto-focus the tab that just
    changed: the frontend compares successive polls and follows the newest
    deliverable, so a user watching the panel sees the brief, then the outline,
    then the art direction, then the slides, without clicking.

    BLOCKING — call through ``off_loop``.
    """
    deck_dir = paths.resolve_deck_dir(deck_id)
    if deck_dir is None:
        return None

    compose_by_slug, defs_file = _index_compose_dir(paths.contained_deck_dir(deck_dir, "compose"))
    previews = _previews_by_page(deck_dir)
    slides_dir = paths.contained_deck_dir(deck_dir, "slides")

    slides: list[SlideRef] = []
    page = 0
    for slug in _slide_order(deck_dir):
        if slides_dir is None or not (slides_dir / f"{slug}.json").is_file():
            continue
        page += 1
        compose_name = compose_by_slug.get(slug)
        preview_name = previews.get(page)
        # The slug is screened HERE as well as in `_preview_url`, because it is not
        # only a URL segment — it is also the slide's rendered LABEL, so a
        # credential-shaped slug leaks through the `slug` field even when both URLs
        # are refused. The artifact filenames need no check at this level: they reach
        # the response only inside a URL, and `_preview_url` screens every segment.
        #
        # `_slide_order` prefers `outline.md`, whose `[a-z0-9-]+` grammar cannot spell
        # a credential — but its FALLBACK is the slide filenames, and `SEGMENT_RE`
        # allows `[A-Za-z0-9._-]`, so an `AKIA…`-named slide file reached `/deck`
        # verbatim. Verified before this guard.
        #
        # Skipped, not redacted, for the reason `paths.resolve_deck_dir` refuses a
        # credential-shaped deck id rather than rewriting it: the slug IS the filename
        # the browser sends back, so a scrubbed value would name a slide that resolves
        # to nothing. Dropping the one slide keeps the rest of the deck usable.
        if redact(slug) != slug:
            logger.warning("pptx-maker: skipped a slide whose slug looks like a credential")
            continue
        slides.append(
            SlideRef(
                slug=slug,
                preview_url=(
                    _preview_url(deck_id, "preview", preview_name) if preview_name else None
                ),
                compose_url=(
                    _preview_url(deck_id, "compose", compose_name) if compose_name else None
                ),
            )
        )

    specs: dict[str, str] = {}
    updated: dict[str, float] = {}
    for key, candidates in _SPEC_DOCS:
        for filename in candidates:
            path = paths.contained_deck_file(deck_dir, "specs", filename)
            if path is not None:
                # `_SPEC_DOCS` filenames are fixed constants, so this cannot be
                # refused today — but the URL builder screens every segment, so the
                # result is Optional and is handled rather than asserted away. A
                # refused spec is simply absent, exactly like a missing file.
                url = _preview_url(deck_id, "specs", filename)
                if url is None:
                    break
                specs[key] = url
                updated[key] = _mtime(path)
                break

    compose_dir = paths.contained_deck_dir(deck_dir, "compose")
    if compose_dir is not None:
        try:
            slide_mtimes = [_mtime(p) for p in compose_dir.glob("*.json")]
        except OSError:
            slide_mtimes = []
        if slide_mtimes:
            updated["slides"] = max(slide_mtimes)

    pptx = deck_dir / "output.pptx"
    return DeckDetail(
        deck_id=deck_id,
        name=_deck_name(deck_dir),
        defs_url=_preview_url(deck_id, "compose", defs_file) if defs_file else None,
        pptx_url=_preview_url(deck_id, "output.pptx") if pptx.is_file() else None,
        dir_path=str(deck_dir),
        pptx_path=str(pptx) if pptx.is_file() else None,
        specs=specs,
        updated_at=updated,
        slides=slides,
    ).to_dict()
