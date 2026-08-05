"""Note parsing, wikilinks, backlinks, tags and full-text search for Notes.

Ported from the app's ``@md-notebook/core-notes`` TypeScript package. Extraction
is text-based with code masking: fenced blocks and inline code are blanked
before scanning, so ``[[links]]`` and ``#tags`` written inside code samples
never register as real links or tags.

Masking replaces code characters with spaces rather than deleting them, which
keeps every line's length and the total line count intact — backlink line
numbers are computed off the masked body and must match the source file.
"""
from __future__ import annotations

import datetime
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import yaml  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

# [[Target]] or [[Target|alias]] — target may not contain ] [ | or newline.
WIKILINK_RE = re.compile(r"\[\[([^\]\[|\n]+?)(?:\|([^\]\n]+?))?\]\]")
# Inline #tag at a word boundary: must start with alphanumeric.
TAG_RE = re.compile(r"(?:^|[\s(])#([A-Za-z0-9][\w/-]*)", re.MULTILINE)
# YAML frontmatter fenced by --- at the very top of the file.
FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)
_FENCED_RE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`", re.MULTILINE)

# Search scoring: a title hit is worth this multiple of a body hit.
TITLE_BOOST = 2.0
# Characters of surrounding context returned with a search hit.
SNIPPET_BEFORE = 40
SNIPPET_AFTER = 80
# Backlink context is truncated to this many characters.
BACKLINK_CONTEXT_CHARS = 160


def _blank(match: re.Match[str]) -> str:
    """Replace a match with spaces, preserving newlines and total length."""
    return re.sub(r"[^\n]", " ", match.group(0))


def mask_code(src: str) -> str:
    """Blank fenced code blocks and inline code, preserving line structure."""
    out = _FENCED_RE.sub(_blank, src)
    return _INLINE_CODE_RE.sub(_blank, out)


def note_basename(path: str) -> str:
    """Filename without directories or the .md extension."""
    file = path.split("/")[-1] if "/" in path else path
    return re.sub(r"\.md$", "", file, flags=re.IGNORECASE)


def _json_safe(value: Any, _seen: Optional[set[int]] = None) -> Any:
    """Coerce YAML-parsed values that JSON cannot serialize into safe forms.

    ``yaml.safe_load`` resolves the YAML core schema, so an unquoted frontmatter
    date (``date: 2026-08-01`` — routine in Obsidian) becomes a ``datetime.date``,
    a ``!!binary`` becomes ``bytes``, a ``!!set`` becomes ``set`` and ``.nan`` /
    ``.inf`` become non-finite floats — none of which round-trips as valid JSON,
    so the note read would 500 (or emit bare ``NaN``, which the browser's
    JSON.parse rejects). Recurse and turn each into a string / list.

    A container is coerced only the FIRST time it is reached; any later
    occurrence of the same object (``_seen`` tracks ids on the whole walk) is
    replaced with ``None``. YAML returns aliased nodes as the *same* object, so a
    nested-anchor document is O(1) to parse but a naive tree-walk — and, more to
    the point, ``json.dumps`` writing the result — would expand it into an
    exponential tree (a billion-laughs amplification reachable from a cloned
    remote's note that would exhaust the event loop / OOM). Collapsing repeats to
    ``None`` keeps both the walk and the serialized output linear, and also turns
    a truly cyclic alias (``a: &a [*a]``) into a finite, JSON-safe structure.
    """
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, (dict, list, tuple, set)):
        if _seen is None:
            _seen = set()
        vid = id(value)
        if vid in _seen:
            # A repeated alias or a cycle — collapse it so neither this walk nor
            # json.dumps re-expands the shared node.
            return None
        _seen.add(vid)
        if isinstance(value, dict):
            return {str(k): _json_safe(v, _seen) for k, v in value.items()}
        return [_json_safe(v, _seen) for v in value]
    return value


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from the body. Malformed YAML yields no data."""
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    data: dict[str, Any] = {}
    try:
        parsed = yaml.safe_load(m.group(1))
        if isinstance(parsed, dict):
            data = _json_safe(parsed)
    except (yaml.YAMLError, RecursionError):
        # Malformed frontmatter — or a cyclic YAML alias (`loop: &l [*l]`) that
        # safe_load accepts as a self-referential structure and that would
        # recurse without bound here (and later in json.dumps). Treat it as
        # absent rather than failing the whole note read.
        logger.debug("ignoring malformed or cyclic frontmatter")
    return data, content[m.end() :]


@dataclass(frozen=True)
class NoteRef:
    """A note's identity for link resolution: its path and display title."""

    path: str
    title: str


def resolve_target(target: str, notes: Iterable[NoteRef]) -> Optional[str]:
    """Resolve a wikilink target against known notes by title, then filename."""
    want = target.strip().lower()
    refs = list(notes)
    for n in refs:
        if n.title.lower() == want:
            return n.path
    for n in refs:
        if note_basename(n.path).lower() == want:
            return n.path
    return None


def extract_wikilinks(content: str, notes: Iterable[NoteRef] = ()) -> list[dict[str, Any]]:
    """Extract [[Target]] / [[Target|alias]] links. Dangling links resolve to None."""
    refs = list(notes)
    links: list[dict[str, Any]] = []
    for m in WIKILINK_RE.finditer(mask_code(content)):
        target = m.group(1).strip()
        if not target:
            continue
        alias = m.group(2).strip() if m.group(2) else None
        links.append(
            {"target": target, "alias": alias, "resolvedPath": resolve_target(target, refs)}
        )
    return links


def extract_tags(content: str) -> list[str]:
    """Extract inline #tags, de-duplicated, in first-seen order."""
    seen: dict[str, None] = {}
    for m in TAG_RE.finditer(mask_code(content)):
        seen.setdefault(m.group(1), None)
    return list(seen)


def note_title(path: str, content: str) -> str:
    """Title for a note: frontmatter ``title`` when set, else the filename."""
    data, _ = parse_frontmatter(content)
    title = data.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return note_basename(path)


def parse_note(path: str, content: str, notes: Iterable[NoteRef] = ()) -> dict[str, Any]:
    """Parse one note into frontmatter, tags and links."""
    data, body = parse_frontmatter(content)
    raw_tags = data.get("tags")
    fm_tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
    merged: dict[str, None] = {}
    for t in [*fm_tags, *extract_tags(body)]:
        merged.setdefault(t, None)
    return {
        "frontmatter": data,
        "tags": list(merged),
        "links": extract_wikilinks(body, notes),
    }


def build_backlinks(notes: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Build a target-path -> backlinks map for a whole vault.

    A note linking to itself is not a backlink, and unresolvable targets are
    skipped, so the map only ever contains links between real notes.
    """
    refs = [NoteRef(path=p, title=note_title(p, c)) for p, c in notes.items()]
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source_path, content in notes.items():
        _, body = parse_frontmatter(content)
        lines = mask_code(body).split("\n")
        for i, line in enumerate(lines):
            for m in WIKILINK_RE.finditer(line):
                resolved = resolve_target(m.group(1), refs)
                if not resolved or resolved == source_path:
                    continue
                out[resolved].append(
                    {
                        "sourcePath": source_path,
                        "line": i + 1,
                        "context": line.strip()[:BACKLINK_CONTEXT_CHARS],
                    }
                )
    return dict(out)


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens; the unit of both indexing and querying."""
    return re.findall(r"[a-z0-9]+", text.lower())


@dataclass
class SearchDoc:
    """One indexed note."""

    path: str
    title: str
    content: str


@dataclass
class _Posting:
    """Per-document term frequencies, split by field so titles can be boosted."""

    title: int = 0
    content: int = 0


class SearchIndex:
    """Full-text index over note titles and bodies.

    An inverted index with prefix matching and a title boost, scored with a
    tf-idf variant. The TypeScript original used MiniSearch (BM25); ranking is
    therefore similar in shape but not numerically identical. Term presence,
    prefix behaviour and the title boost — the parts users can perceive — match.
    """

    #: path -> indexed document
    docs: dict[str, SearchDoc]
    #: term -> {path: per-field frequencies}
    _postings: dict[str, dict[str, _Posting]]

    def __init__(self, docs: Iterable[SearchDoc] = ()) -> None:
        self.docs = {}
        self._postings = defaultdict(dict)
        for d in docs:
            self.add(d)

    def add(self, doc: SearchDoc) -> None:
        """Index a note, replacing any existing entry for the same path."""
        if doc.path in self.docs:
            self.update(doc)
            return
        self.docs[doc.path] = doc
        for term in _tokenize(doc.title):
            self._postings[term].setdefault(doc.path, _Posting()).title += 1
        for term in _tokenize(doc.content):
            self._postings[term].setdefault(doc.path, _Posting()).content += 1

    def update(self, doc: SearchDoc) -> None:
        """Replace a note's indexed content."""
        if doc.path in self.docs:
            self.remove(doc.path)
        self.add(doc)

    def remove(self, path: str) -> None:
        """Drop a note from the index."""
        if path not in self.docs:
            return
        del self.docs[path]
        for term in list(self._postings):
            self._postings[term].pop(path, None)
            if not self._postings[term]:
                del self._postings[term]

    def _matching_terms(self, token: str) -> list[str]:
        """Exact match plus prefix matches, mirroring MiniSearch's prefix search."""
        if token in self._postings:
            return [token]
        return [t for t in self._postings if t.startswith(token)]

    def search(self, query: str) -> list[dict[str, Any]]:
        """Rank notes against a query. Returns highest-scoring first."""
        tokens = _tokenize(query)
        if not tokens or not self.docs:
            return []
        total_docs = len(self.docs)
        scores: dict[str, float] = defaultdict(float)
        hit_terms: dict[str, list[str]] = defaultdict(list)
        for token in tokens:
            for term in self._matching_terms(token):
                postings = self._postings.get(term, {})
                if not postings:
                    continue
                idf = math.log(1 + total_docs / len(postings))
                for path, posting in postings.items():
                    weighted = posting.content + posting.title * TITLE_BOOST
                    scores[path] += weighted * idf
                    hit_terms[path].append(term)
        results: list[dict[str, Any]] = []
        for path, score in scores.items():
            doc = self.docs[path]
            results.append(
                {
                    "path": path,
                    "title": doc.title,
                    "score": round(score, 6),
                    "snippet": self._snippet(doc.content, hit_terms[path]),
                }
            )
        results.sort(key=lambda r: (-r["score"], r["path"]))
        return results

    @staticmethod
    def _snippet(content: str, terms: list[str]) -> Optional[str]:
        """Text around the first matching term, or None when it is title-only."""
        lowered = content.lower()
        for term in terms:
            at = lowered.find(term)
            if at >= 0:
                return content[max(0, at - SNIPPET_BEFORE) : at + SNIPPET_AFTER].strip()
        return None
