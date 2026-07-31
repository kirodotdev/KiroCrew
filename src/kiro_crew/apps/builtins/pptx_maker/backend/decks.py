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

from kiro_crew.apps.builtins.pptx_maker.backend import paths
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


def _preview_url(deck_id: str, *rel: str) -> str:
    """A deck-artifact URL relative to this app's API base."""
    return "preview/" + "/".join((deck_id, *rel))


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _deck_name(deck_dir: Path) -> str:
    """The deck's display name from its own metadata, else the directory name."""
    for filename in _NAME_FILES:
        candidate = deck_dir / filename
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("name"):
            # The metadata file is written by the agent, so its `name` is model
            # output on its way to the dashboard: redact before it leaves
            # (AUTOSDE `backend-security-controls`).
            return redact(str(data["name"]))
    return deck_dir.name


def _read_brief(deck_dir: Path) -> str:
    brief_path = deck_dir / "specs" / "brief.md"
    if not brief_path.is_file():
        return ""
    try:
        raw = brief_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # `brief.md` is authored by the agent from the user's conversation, so the
    # preview is untrusted text bound for the dashboard. Redact BEFORE the slice
    # so a credential cannot straddle the truncation boundary and survive.
    return redact(raw)[:BRIEF_PREVIEW_CHARS]


def _first_thumbnail(deck_dir: Path) -> str | None:
    preview_dir = deck_dir / "preview"
    if not preview_dir.is_dir():
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
    for deck_dir in entries:
        if len(out) >= MAX_DECKS:
            break
        if not deck_dir.is_dir() or deck_dir.name.startswith((".", "_")):
            continue
        slides_dir = deck_dir / "slides"
        try:
            slide_count = len(list(slides_dir.glob("*.json"))) if slides_dir.is_dir() else 0
        except OSError:
            slide_count = 0
        has_content = (
            slide_count > 0 or (deck_dir / "deck.json").is_file() or (deck_dir / "specs").is_dir()
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


def _index_compose_dir(compose_dir: Path) -> tuple[dict[str, str], str | None]:
    """Latest-epoch compose file per slug, plus the latest shared defs file.

    The engine writes a NEW ``<slug>_<epoch>.json`` on every recompose rather
    than overwriting, so picking the highest epoch is what makes the preview
    show the current render instead of the first one.
    """
    by_slug: dict[str, tuple[int, str]] = {}
    defs_file: str | None = None
    defs_epoch = -1
    if not compose_dir.is_dir():
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
    outline = deck_dir / "specs" / "outline.md"
    slugs: list[str] = []
    if outline.is_file():
        try:
            for line in outline.read_text(encoding="utf-8", errors="replace").splitlines():
                match = _OUTLINE_SLUG_RE.match(line)
                if match:
                    slugs.append(match.group(1))
        except OSError:
            slugs = []
    if slugs:
        return slugs
    slides_dir = deck_dir / "slides"
    if not slides_dir.is_dir():
        return []
    try:
        return [p.stem for p in sorted(slides_dir.glob("*.json"))]
    except OSError:
        return []


def _previews_by_page(deck_dir: Path) -> dict[int, str]:
    """Rasterized preview filenames keyed by 1-based page number."""
    preview_dir = deck_dir / "preview"
    out: dict[int, str] = {}
    if not preview_dir.is_dir():
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

    compose_by_slug, defs_file = _index_compose_dir(deck_dir / "compose")
    previews = _previews_by_page(deck_dir)
    slides_dir = deck_dir / "slides"

    slides: list[SlideRef] = []
    page = 0
    for slug in _slide_order(deck_dir):
        if not (slides_dir / f"{slug}.json").is_file():
            continue
        page += 1
        compose_name = compose_by_slug.get(slug)
        preview_name = previews.get(page)
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

    specs_dir = deck_dir / "specs"
    specs: dict[str, str] = {}
    updated: dict[str, float] = {}
    for key, candidates in _SPEC_DOCS:
        for filename in candidates:
            path = specs_dir / filename
            if path.is_file():
                specs[key] = _preview_url(deck_id, "specs", filename)
                updated[key] = _mtime(path)
                break

    compose_dir = deck_dir / "compose"
    if compose_dir.is_dir():
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
