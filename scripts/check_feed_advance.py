#!/usr/bin/env python3
"""Decide whether a channel feed pointer may advance to a new version.

The CDN's channel feed files (``feed/<channel>/latest-cli.json``,
``latest-mac.yml``, ``latest-linux*.yml``, ``latest.yml``) are mutable
last-writer-wins pointers. A hotfix release cut on an OLD release line
(e.g. tagging ``v0.4.1-insider.1`` while the insider channel already
serves ``0.5.0-insider.1``) must still build, sign, and publish its
immutable per-version assets -- but it must NOT move the channel pointer
backward, or every client on the channel is offered a downgrade
(electron-updater accepts it when the installed version carries a
prerelease suffix).

Usage::

    check_feed_advance.py --new <version> --channel <channel>
        [--current-file <path>] [--tags-file <path>] [--self <tag>]

Two independent "what does the channel currently serve" signals, because
neither alone is trustworthy from a publish job:

* ``--current-file``: the live feed fetched through the PUBLIC CDN (the
  publish role is Put-only on ``feed/*``, so S3 read-back is not an
  option). CloudFront serves it with ``max-age=300``, so a read can be
  up to five minutes stale -- exactly the window in which two
  back-to-back serialized release runs would miss each other.
* ``--tags-file``: the repository's tags (one per line, ``v`` prefix
  optional). Tags are pushed before the run that publishes them starts,
  so they close the CDN staleness window. Only consulted for the insider
  and stable channels (nightly builds are untagged); ``--self`` names
  the tag THIS run publishes so it never holds against itself.

Verdict: HOLD when any candidate is STRICTLY newer than ``--new``;
ADVANCE otherwise. Equality advances deliberately: a re-run of the
current release (recovering a run that died between the versioned upload
and the feed write) must be able to complete the pointer write, and
rewriting the pointer with the version it already names is a no-op.

* exit 0  -- advance (prints ``advance``)
* exit 3  -- hold (prints ``hold``)
* exit 2  -- bad invocation

Version spellings accepted (all appear across the feeds and tags):

* ``X.Y.Z``                  (bare stable)
* ``X.Y.Z-<label>.N``        (tag/desktop prerelease, e.g. 0.5.0-insider.1)
* ``X.Y.Z<label>N``          (PEP 440 wheel stamp, e.g. 0.5.0rc1)
* ``X.Y.Z-nightly.<D>t<T>``  (desktop nightly stamp; date+time both order)
* ``X.Y.Z.devN``             (PEP 440 nightly wheel stamp)
* ``X.Y.Z-<anything>``       (any other prerelease spelling release.yml
  accepts, e.g. 0.5.0-beta-preview.1 or 0.5.0-beta)

That last row is a PARITY requirement, not generosity. ``release.yml``
triggers on ``v*`` and validates only the part BEFORE the first hyphen
(``^[0-9]+\\.[0-9]+\\.[0-9]+$``); the prerelease remainder is free-form,
and it derives the wheel's ``rcN`` from the text after the LAST dot,
falling back to ``0`` when that is not numeric. So a tag like
``v0.5.0-beta-preview.1`` builds, signs, and reaches the publish jobs.
The desktop publishers pass the RAW tag version to ``--new``, so any
spelling this parser rejects makes the guard exit 2 with empty stdout --
which those jobs treat as "neither advance nor hold" and fail. A guard
narrower than the tag surface therefore converts an odd-but-accepted tag
into a hard publish outage, so the fallback below mirrors release.yml's
own trailing-number rule exactly rather than inventing a stricter one.

Ordering: release parts compare numerically; a bare release outranks any
prerelease of the SAME base (0.5.0 > 0.5.0-insider.9); prereleases of
the same base compare by their trailing number, label-insensitively
(``-insider.N`` and ``rcN`` are the same lane spelled two ways).

Deliberately dependency-free: this runs on bare CI runners where neither
``packaging`` nor the repo's own wheel is installed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# X.Y.Z with an optional prerelease in one of three spellings:
#   -<label>.N[tN]  (tag/desktop form; label alphanumeric, e.g. insider, rc,
#                    nightly -- the nightly stamp carries a tHHMMSS time part
#                    that MUST participate in ordering, or two same-day
#                    nightlies compare equal and a rerun of the earlier one
#                    could advance over the later one)
#   <label>N        (PEP 440 wheel form appended without a dash, e.g. rc1)
#   .devN           (PEP 440 nightly wheel form, e.g. .dev20260830065756)
_VERSION_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)"
    r"(?:-(?P<dashlabel>[A-Za-z]+)\.(?P<dashnum>\d+)(?:t(?P<dashtime>\d+))?"
    r"|\.dev(?P<devnum>\d+)"
    r"|(?P<taglabel>[A-Za-z]+)(?P<tagnum>\d+))?$"
)

# Fallback for every OTHER prerelease spelling release.yml accepts: a valid
# X.Y.Z base, a hyphen, and a free-form remainder (hyphenated labels like
# `beta-preview.1`, dotless labels like `beta`, multi-dot labels). Tried only
# after _VERSION_RE, so the structured forms above keep their richer ordering
# (notably the nightly tHHMMSS time part).
_LOOSE_PRERELEASE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-(?P<pre>.*)$")


def _loose_pre_number(pre: str) -> int:
    """release.yml's own rule: the text after the LAST dot, else 0.

    Mirrors the shell in the Derive Version + Channel job
    (``N="${PRE##*.}"``; non-numeric ``N`` becomes ``0``), so the guard's
    idea of "which prerelease is this" cannot disagree with the wheel
    version the same tag publishes under.
    """
    tail = pre.rsplit(".", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def parse_version(text: str) -> tuple[int, int, int, int, int] | None:
    """Comparable tuple: (major, minor, patch, is_release, pre_number).

    ``is_release`` is 1 for a bare X.Y.Z and 0 for any prerelease, so a
    release sorts above every prerelease of the same base. The prerelease
    LABEL is deliberately ignored for ordering -- ``0.5.0-insider.1`` and
    ``0.5.0rc1`` are the same build spelled two ways, and a nightly's two
    spellings (``-nightly.YYYYMMDDtHHMMSS`` and ``.devYYYYMMDDHHMMSS``)
    concatenate to the same 14-digit number.
    """
    text = text.strip()
    if text.startswith("v"):
        text = text[1:]
    m = _VERSION_RE.match(text)
    if not m:
        loose = _LOOSE_PRERELEASE_RE.match(text)
        if not loose:
            return None
        return (
            int(loose.group(1)),
            int(loose.group(2)),
            int(loose.group(3)),
            0,
            _loose_pre_number(loose.group("pre")),
        )
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if m.group("devnum") is not None:
        return (major, minor, patch, 0, int(m.group("devnum")))
    if m.group("dashnum") is not None:
        pre = m.group("dashnum") + (m.group("dashtime") or "")
        return (major, minor, patch, 0, int(pre))
    if m.group("tagnum") is not None:
        return (major, minor, patch, 0, int(m.group("tagnum")))
    return (major, minor, patch, 1, 0)


def extract_feed_version(content: str) -> str | None:
    """Pull the version string out of a feed file, JSON or YAML.

    Tries JSON first (``latest-cli.json``, legacy ``latest-mac.json``);
    falls back to a line-anchored ``version:`` scan for the
    electron-updater YAML feeds. Returns None when nothing parseable is
    found -- the caller treats that as "no current version".
    """
    body = content.strip()
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict):
        version = data.get("version")
        return str(version) if version else None
    for line in body.splitlines():
        # electron-updater feeds put the version at top level; the files
        # are flat so a left-anchored match cannot hit a nested key.
        m = re.match(r"^version:\s*['\"]?([^'\"\s]+)['\"]?\s*$", line)
        if m:
            return m.group(1)
    return None


def tag_candidates(tags: list[str], channel: str, self_tag: str) -> list[str]:
    """Tags that speak for the channel's current level, minus this run's own.

    * stable: bare ``vX.Y.Z`` tags only -- each is a stable release.
    * insider: every version tag counts. Prerelease tags are that lane's
      own releases; a BARE tag also caps it, because once ``0.5.0``
      shipped stable, cutting more ``0.5.0-insider.N`` is a hotfix on an
      old line by definition.
    * nightly: none -- nightly builds are untagged, the feed is the only
      signal.
    """
    if channel == "nightly":
        return []
    self_norm = self_tag.lstrip("v")
    out = []
    for tag in tags:
        norm = tag.strip().lstrip("v")
        if not norm or norm == self_norm:
            continue
        key = parse_version(norm)
        if key is None:
            continue
        if channel == "stable" and key[3] != 1:
            continue
        out.append(norm)
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new", required=True, help="version this run wants to publish")
    parser.add_argument("--channel", required=True, choices=("nightly", "insider", "stable"))
    parser.add_argument(
        "--current-file",
        default="",
        help="path to the current feed content fetched via the CDN ('' = none)",
    )
    parser.add_argument(
        "--tags-file",
        default="",
        help="path to a newline list of repo tags ('' = none)",
    )
    parser.add_argument(
        "--self",
        default="",
        dest="self_tag",
        help="the tag this run publishes, excluded from tag candidates",
    )
    args = parser.parse_args(argv)

    new_key = parse_version(args.new)
    if new_key is None:
        print(f"error: unparseable --new version: {args.new!r}", file=sys.stderr)
        return 2

    candidates: list[tuple[str, str]] = []  # (source, version)

    if args.current_file:
        try:
            with open(args.current_file, encoding="utf-8") as fh:
                feed_version = extract_feed_version(fh.read())
        except OSError:
            feed_version = None
        if feed_version is not None:
            candidates.append(("feed", feed_version))

    if args.tags_file:
        try:
            with open(args.tags_file, encoding="utf-8") as fh:
                tags = fh.read().splitlines()
        except OSError:
            tags = []
        for tag in tag_candidates(tags, args.channel, args.self_tag):
            candidates.append(("tag", tag))

    newer = [
        (source, version)
        for source, version in candidates
        if (key := parse_version(version)) is not None and key > new_key
    ]
    if newer:
        # parse_version is non-None for every entry in `newer` (filtered above);
        # the fallback tuple only satisfies the type checker.
        source, version = max(newer, key=lambda item: parse_version(item[1]) or (0, 0, 0, 0, 0))
        print("hold")
        print(
            f"feed-guard: channel already carries {version} (from {source}), "
            f"newer than {args.new}; holding the feed pointer -- versioned "
            "assets publish, the channel does not move backward",
            file=sys.stderr,
        )
        return 3

    print("advance")
    checked = ", ".join(f"{v} ({s})" for s, v in candidates) or "none found"
    print(
        f"feed-guard: {args.new} is newest among candidates [{checked}]; advancing",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
