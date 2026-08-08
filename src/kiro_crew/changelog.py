"""Parse ``CHANGELOG.md`` into per-version entries for the Releases archive.

## Why this is a module and not a handler helper

The dashboard's Releases archive needs the changelog as *data* (one entry per
version, each independently selectable) rather than as one markdown blob. The
parsing is a pure string function with no I/O, so it lives here and is tested
directly — the handler in ``dashboard/handlers/updates.py`` only reads the file
and calls in.

## What the list contains, and why it is not "every version"

An entry earns its place by having something to say. Two sources qualify:

1. A ``## [X.Y.Z] — YYYY-MM-DD`` section actually present in ``CHANGELOG.md``.
2. The release the *running build* belongs to, whether or not it has a section.

Nothing else is listed. Versions that shipped without a changelog section are
deliberately absent: a row that cannot say anything is worse than no row,
because the reader cannot tell it apart from a broken one. The running build's
release is the one exception, because "what am I on?" is the question the page
is opened to answer, and leaving it out would answer it with someone else's
version number — which is exactly the defect this replaces.

## Prereleases are not entries

``0.2.0-rc.1`` and ``0.2.0-nightly.20260806t065257`` are both *drafts of
0.2.0*, not releases of their own. Listing them as siblings of 0.2.0 would be
the wrong hierarchy, and nightly would add ~4 rows a day forever. Both collapse
onto a single ``0.2.0`` entry flagged :attr:`Release.in_progress`, so one
mechanism serves both channels and the list does not move when 0.2.0 is
eventually promoted — the row is already there and merely loses the flag.
"""

from __future__ import annotations

import re
from typing import NamedTuple

__all__ = ["Release", "base_version", "running_release", "parse_sections", "build_release_list"]

# ``## [0.1.2] — 2026-07-30``. The date is optional and its dash may be an em
# dash (what the repo's own sections use), an en dash, or a hyphen -- authors
# reach for all three and a section is far too valuable to drop over a glyph.
_SECTION_RE = re.compile(
    r"^##\s+\[(?P<version>[^\]]+)\]"
    r"(?:\s*[—–-]\s*(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2}))?"
    r"\s*$"
)

# Any level-2 heading ends the preceding section's body, not just a versioned
# one -- otherwise an unversioned ``## Unreleased`` would be swallowed into the
# entry above it.
_H2_RE = re.compile(r"^##\s+\S")

# Strips a prerelease and/or build suffix off a version string, so every
# spelling of a release folds onto that release.
#
# The alternation is EXHAUSTIVE and CLOSED by construction -- every branch ends
# in a counted group, and none of them can absorb arbitrary trailing text. Two
# earlier shapes ended in an open ``[0-9A-Za-z.-]*`` tail, and each time the
# consequence was the same: ``## [0.1.2rc4junk]`` folded onto ``0.1.2`` and,
# because the last body for a version wins, the malformed section silently
# REPLACED the real release's notes. A spelling this does not recognise keeps its
# own row, which is visible instead of destructive.
#
# The branches are the release pipeline's own output, and nothing else
# (``.github/workflows/release.yml`` maps every semver prerelease tag to ``rcN``
# for the wheel; ``nightly.yml`` emits the semver stamp and the ``.devN`` wheel):
#
#   0.2.0                          stable tag and wheel
#   0.2.0-rc.2 / -insider.4        prerelease tag, desktop build
#   0.2.0-nightly.20260806t065257  nightly desktop stamp (plus two retired
#                                  shapes still installed on old builds:
#                                  -nightly.20260727 and .202607261234)
#   0.2.0rc4                       any prerelease tag's WHEEL version
#   0.2.0.dev20260806065257        nightly wheel
#   ...+local                      a build/local segment on any of the above
#
# Every digit class is ASCII ``[0-9]``, never ``\d``: ``\d`` also matches other
# Unicode decimal digits, and ``## [0.1.2rc٤]`` folding onto ``0.1.2`` is the
# same overwrite bug as ``0.1.2junk`` wearing a different hat.
_BASE_RE = re.compile(
    r"(?P<base>[0-9]+(?:\.[0-9]+)*)"
    r"(?P<pre>"
    r"-(?:rc|insider|alpha|beta)\.[0-9]+"
    r"|-nightly\.[0-9]{8}(?:t[0-9]{6}|[0-9]{4})?"
    r"|(?:a|b|c|rc)[0-9]+(?:\.post[0-9]+)?(?:\.dev[0-9]+)?"
    r"|\.post[0-9]+(?:\.dev[0-9]+)?"
    r"|\.dev[0-9]+"
    r")?"
    r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
)

# The RUNNING BUILD's version, unlike a changelog heading, cannot overwrite
# anything -- it only decides which row is the reader's own. So it gets a lenient
# reading: any SemVer prerelease label at all folds onto its numeric base.
# ``release.yml`` treats EVERY ``v1.2.3-<label>`` tag as a prerelease and passes
# the label through to the desktop build unchanged, so a future ``-preview.7``
# tag must not leave its user staring at a second, apparently-released row.
_RUNNING_RE = re.compile(
    r"(?P<base>[0-9]+(?:\.[0-9]+)*)"
    r"(?P<pre>-[0-9A-Za-z.-]+)?"
    r"(?:\+[0-9A-Za-z.-]+)?"
)


class Release(NamedTuple):
    """One row of the archive."""

    version: str
    """Bare ``X.Y.Z``, never a prerelease spelling."""
    date: str
    """``YYYY-MM-DD`` from the section heading, or ``""`` when unknown.

    Deliberately not defaulted to the build date: the heading's date is
    hand-written and has already drifted from the tag it claims to describe
    (``[0.1.2]`` says 2026-07-30; ``v0.1.2`` was tagged 2026-08-04), so an
    absent date is reported as absent rather than guessed.
    """
    body: str
    """The section's markdown, heading excluded. Empty when there is no section."""
    is_current: bool
    """True for the release the running build belongs to."""
    in_progress: bool
    """True when the running build is a *prerelease* of this version.

    Implies the version is not released yet, which is knowable offline: a build
    whose version carries a prerelease suffix is by construction a draft of its
    own base.
    """


def base_version(version: str) -> str:
    """Return the release *version* belongs to.

    ``0.2.0-rc.1``, ``0.2.0-nightly.20260806t065257`` and
    ``0.2.0.dev20260806065257`` all belong to ``0.2.0``. A bare version is its
    own base. An unparseable string returns itself, so a caller never has to
    handle ``None`` and a surprising version spelling degrades to "no match"
    instead of raising on the About page -- and, because the match is total,
    ``0.1.2junk`` degrades to its own row rather than quietly replacing
    ``0.1.2``'s notes.
    """
    m = _BASE_RE.fullmatch(version.strip())
    return m.group("base") if m else version.strip()


def running_release(version: str) -> tuple[str, bool]:
    """Return ``(release, is_prerelease)`` for the version of the RUNNING BUILD.

    Split from :func:`base_version` because the two inputs carry opposite risks.
    A changelog HEADING is text that can be wrong, and a wrong heading that folds
    onto a real release replaces its notes -- so headings get the strict,
    closed reading. The running build's version comes from the release pipeline
    and is only ever used to mark which row is the reader's own, so nothing it
    matches can be overwritten; there, failing to fold is the harmful direction,
    because the reader's own prerelease then appears as a second row that looks
    released.

    ``is_prerelease`` comes from an actual prerelease MARKER rather than from
    ``base != version``: a bare build/local segment (``0.2.0+abc123``) is not a
    prerelease, and testing inequality would badge it "In progress".
    """
    v = version.strip()
    if not v:
        return "", False
    m = _BASE_RE.fullmatch(v)
    if m:
        return m.group("base"), bool(m.group("pre"))
    loose = _RUNNING_RE.fullmatch(v)
    if loose:
        return loose.group("base"), bool(loose.group("pre"))
    return v, False


def _sort_key(version: str) -> tuple[int, ...]:
    """Numeric-segment sort key, so 0.10.0 sorts above 0.9.0 (string sort does not)."""
    parts = []
    for seg in version.split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            # A non-numeric segment sorts below any number at that position
            # rather than aborting the whole comparison.
            parts.append(-1)
    return tuple(parts)


def parse_sections(markdown: str) -> list[tuple[str, str, str]]:
    """Split *markdown* into ``(version, date, body)`` per ``## [X.Y.Z]`` heading.

    Returns them in document order. Content before the first versioned heading
    (the file's title and preamble) is discarded: it belongs to the document,
    not to any release.
    """
    out: list[tuple[str, str, str]] = []
    version = date = ""
    body: list[str] = []
    open_section = False

    def flush() -> None:
        if open_section:
            out.append((version, date, "\n".join(body).strip()))

    for line in markdown.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            flush()
            version = m.group("version").strip()
            date = m.group("date") or ""
            body = []
            open_section = True
            continue
        if open_section and _H2_RE.match(line):
            flush()
            open_section = False
            continue
        if open_section:
            body.append(line)

    flush()
    return out


def build_release_list(markdown: str, current_version: str) -> list[Release]:
    """Merge the changelog's sections with the running build's own release.

    Sorted newest first. The running build's release is present even with no
    section (carrying an empty ``body``), and is the only version allowed in on
    those terms -- see the module docstring for why every *other* section-less
    version stays out.
    """
    current_base, is_prerelease = running_release(current_version)

    entries: dict[str, Release] = {}
    for version, date, body in parse_sections(markdown):
        # Key on the base so a hypothetical ``## [0.2.0-rc.1]`` section folds
        # into 0.2.0 instead of opening a second row for the same release.
        key = base_version(version)
        # FIRST in document order wins, which under keep-a-changelog's
        # newest-first convention means the newest wins. Last-wins let a
        # ``## [0.2.0-rc.1]`` draft lower in the file replace the released
        # ``## [0.2.0]`` section's body AND date -- with the fold itself being
        # correct, so nothing looked wrong.
        entries.setdefault(
            key,
            Release(
                version=key,
                date=date,
                body=body,
                is_current=key == current_base,
                in_progress=key == current_base and is_prerelease,
            ),
        )

    if current_base and current_base not in entries:
        entries[current_base] = Release(
            version=current_base,
            date="",
            body="",
            is_current=True,
            in_progress=is_prerelease,
        )

    return sorted(entries.values(), key=lambda r: _sort_key(r.version), reverse=True)
