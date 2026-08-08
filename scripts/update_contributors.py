#!/usr/bin/env python3
"""Insert merged-PR authors into the README Contributors block.

Usage:
    # Add contributors read as JSON from stdin ([{"login", "name"}, ...]).
    # This is the path the workflow uses:
    gh api ... | python3 scripts/update_contributors.py

    # Add a single contributor from flags (a manual/debug convenience):
    python3 scripts/update_contributors.py --login octocat --name "The Octocat"

    # Self-test (no repository or network needed):
    python3 scripts/update_contributors.py --test

Exit codes:
    0  README updated (or already up to date — idempotent no-op)
    1  malformed input / README block not found / validation failure
    2  environment error (README unreadable)

The script is pure and network-free: the caller (the workflow) does all
``gh``/GitHub I/O and pipes the resulting author records here. Contributor
data is PR-controlled, so every login is validated against GitHub's username
grammar and every display name is HTML-escaped before it reaches the README.
Existing entries are preserved byte-for-byte, so hand-curated display names and
inline comments (e.g. a ``wokeignore`` marker) survive a re-run untouched.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_README = _REPO_ROOT / "README.md"

# The Contributors block is a contiguous run of one-entry-per-line anchor tags,
# sorted by GitHub login case-insensitively. New entries are inserted at their
# sorted position; existing lines are never moved or rewritten, so a name a
# maintainer added by hand (even out of strict order) is left exactly in place.
# We locate the run by these anchors rather than by heading text so the block
# can move within the file.
_ENTRY_RE = re.compile(
    r'^<a href="https://github\.com/(?P<login>[^"/]+)" title=".*?">'
    r'<img src="https://github\.com/[^"]+\.png\?size=64"'
    r' width="64" height="64" alt=".*?" /></a>'
)

# GitHub usernames: 1-39 chars, alphanumeric or single hyphens, no leading/
# trailing hyphen. We reject anything else rather than escape it — a login that
# is not a real GitHub handle cannot resolve to a real avatar or profile, so
# admitting it would only inject a broken (or hostile) link.
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

# Any link to a GitHub PROFILE anywhere in the README, in whatever form a human
# might type it: an ``<a href>`` or bare text, with or without scheme/``www.``,
# and with an optional trailing slash, query, or fragment. This is how we
# recognize a contributor a maintainer credited by hand OUTSIDE the sorted block
# (a prose "thanks, see github.com/x", a second entry sharing a line, a
# non-standard anchor) so we never re-add someone already credited in any form.
# The login is one whole path segment: the trailing ``/?`` allows a single
# trailing slash, and the negative lookahead then rejects a DEEPER segment, so a
# repository or badge link (``…/owner/repo/releases``, ``…/owner/repo/``) is
# never mistaken for a profile (which would wrongly skip a real new author of
# that repo-owner's name).
_PROFILE_HREF_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/" r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/?(?![A-Za-z0-9/-])",
    re.IGNORECASE,
)


def _entry_line(login: str, name: str) -> str:
    """Render one contributor anchor line.

    ``name`` is the human display text (falls back to the login) and is
    HTML-escaped for both the ``title`` and ``alt`` attributes. ``login`` is
    already validated against ``_LOGIN_RE`` so it needs no escaping. The workflow
    supplies each author's real display name (from the GitHub user profile), so
    the escaping and whitespace-collapse below run on live, user-controlled
    input — they are a hard floor guaranteeing one well-formed single-line entry
    regardless of what a name contains.

    Whitespace is collapsed to single spaces first. This is a hard invariant,
    not cosmetics: one entry MUST render to exactly one line, but a display name
    can carry a line-boundary character (``\\n``, ``\\r``, U+2028/U+2029, NEL,
    vertical tab, form feed) that ``str.splitlines()`` — used to re-parse the
    block — would split on and ``html.escape`` does NOT neutralize. A multi-line
    entry would break the contiguous-run invariant and wedge every later run.
    """
    display = " ".join(name.split()) or login
    esc = html.escape(display, quote=True)
    return (
        f'<a href="https://github.com/{login}" title="{esc}">'
        f'<img src="https://github.com/{login}.png?size=64"'
        f' width="64" height="64" alt="{esc}" /></a>'
    )


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, preserving line endings.

    A plain ``open(path, "w")`` truncates the file before the write, so a
    failure partway through (full disk) would leave the README truncated or
    empty. Instead we write a temp file in the same directory (so ``os.replace``
    is a same-filesystem atomic rename) and swap it in only after a clean close;
    the original is untouched on any failure. ``newline=""`` keeps CRLF/LF
    exactly as ``add_contributors`` produced them.
    """
    directory = path.parent
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".contributors-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        # Leave the original intact and don't leak the temp file.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _find_block(lines: list[str]) -> tuple[int, int]:
    """Return the [start, end) line span of the contributor anchor run.

    Raises ``ValueError`` if no contributor block is present, so the caller
    fails loud rather than silently appending to the wrong place.
    """
    start = end = -1
    for i, line in enumerate(lines):
        if _ENTRY_RE.match(line):
            if start == -1:
                start = i
            end = i + 1
    if start == -1:
        raise ValueError("no contributor anchor block found in README")
    # The block must be a single contiguous run; a gap means our anchor regex is
    # matching something unrelated and we should not guess where to insert.
    for line in lines[start:end]:
        if not _ENTRY_RE.match(line):
            raise ValueError("contributor block is not a contiguous run of entries")
    return start, end


def _coerce_records(raw: object) -> list[dict[str, str]]:
    """Normalize parsed JSON into a list of ``{"login", "name"}`` dicts."""
    if not isinstance(raw, list):
        raise ValueError("input must be a JSON array of contributor objects")
    records: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each contributor must be a JSON object")
        # A JSON null login must become "" (dropped by _LOGIN_RE), NOT the
        # string "None" — str(None) would pass the grammar and inject a
        # github.com/None entry. A null author reaches here from a deleted PR
        # account that slipped past an upstream filter.
        login = str(item.get("login") or "").strip()
        name = str(item.get("name") or "").strip()
        records.append({"login": login, "name": name})
    return records


def _load_optout(path: Path) -> set[str]:
    """Load lowercased opt-out logins from a file (one per line, ``#`` comments).

    A missing file means an empty opt-out set — the feature is opt-in and its
    absence must not break the run. Blank lines and ``#`` comments are ignored.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    out: set[str] = set()
    for line in raw.splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            out.add(entry.lower())
    return out


def add_contributors(
    text: str,
    records: list[dict[str, str]],
    optout: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Return (new_text, added_logins).

    Inserts each novel contributor into the block at its case-insensitive
    sorted position, preserving every existing line verbatim. Idempotent:
    logins already credited ANYWHERE in the README (compared case-insensitively)
    are skipped, so a name a maintainer added by hand — inside or outside the
    sorted block, in any anchor form — is never duplicated. Invalid logins are
    dropped. Returns the text unchanged when nothing is new.

    ``optout`` is a set of lowercased logins that must NEVER be added. This is
    what makes the README's removal promise keepable: because a full rebuild
    re-derives every merged author and dedups only against README PRESENCE, a
    contributor removed on request would otherwise reappear on the next run. A
    login on the opt-out list is skipped even when absent from the README.
    """
    optout = optout or set()
    # Preserve the file's original newline style and trailing-newline state.
    newline = "\r\n" if "\r\n" in text else "\n"
    had_trailing_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()

    start, end = _find_block(lines)
    # Seed the dedup set from EVERY GitHub profile link in the whole README, not
    # just the canonical block — a maintainer may credit someone in prose or a
    # differently-shaped anchor, and re-adding them would double-credit.
    existing: set[str] = {lg.lower() for lg in _PROFILE_HREF_RE.findall(text)}

    # Dedupe within the incoming batch too, keeping the first occurrence.
    seen: set[str] = set()
    added: list[str] = []
    for rec in records:
        login = rec["login"]
        if not _LOGIN_RE.match(login):
            continue
        # Skip GitHub bot accounts (login ends in "[bot]" — already excluded by
        # _LOGIN_RE since "[" is invalid — and the "-bot"/"[bot]" author type is
        # filtered by the caller; this is a defense-in-depth belt).
        key = login.lower()
        if key in existing or key in seen or key in optout:
            continue
        seen.add(key)
        added.append(login)

    if not added:
        return text, []

    # Insert each new entry at its case-insensitive sorted position WITHOUT
    # reordering existing lines. Existing rows never move relative to each
    # other, so a name added by hand (even one out of strict sort order) is
    # left exactly where a maintainer put it — automation only ever inserts,
    # never rewrites. The block is treated as sorted for placement, and when it
    # is not (a hand-added row out of order), we fall back to appending, which
    # still never disturbs an existing line.
    pending = set(added)
    new_block = list(lines[start:end])
    for rec in records:
        login = rec["login"]
        # Consume each new login exactly once, so a login repeated within the
        # incoming batch is inserted a single time.
        if login not in pending:
            continue
        pending.discard(login)
        entry = _entry_line(login, rec["name"])
        key = login.lower()
        pos = len(new_block)
        for i, line in enumerate(new_block):
            m = _ENTRY_RE.match(line)
            if m and m.group("login").lower() > key:
                pos = i
                break
        new_block.insert(pos, entry)

    new_lines = lines[:start] + new_block + lines[end:]
    result = newline.join(new_lines)
    if had_trailing_newline:
        result += newline
    return result, added


# --------------------------------------------------------------------------- #
# Self-test — runs without a repository or network (``--test``).
# --------------------------------------------------------------------------- #
def _self_test() -> int:
    sample = (
        "## Contributors\n\n"
        "intro\n\n"
        '<a href="https://github.com/0V" title="G2">'
        '<img src="https://github.com/0V.png?size=64" width="64" height="64" alt="G2" /></a>\n'
        '<a href="https://github.com/bob" title="Bob">'
        '<img src="https://github.com/bob.png?size=64" width="64" height="64" alt="Bob" /></a>\n'
        "\ntrailer\n"
    )
    failures = 0

    def check(name: str, cond: bool) -> None:
        nonlocal failures
        if not cond:
            failures += 1
            print(f"FAIL: {name}")
        else:
            print(f"ok:   {name}")

    # Insert in the middle, preserving alpha-fold order.
    out, added = add_contributors(sample, [{"login": "amy", "name": "Amy"}])
    check("added amy", added == ["amy"])
    logins = [m.group("login") for m in (_ENTRY_RE.match(ln) for ln in out.splitlines()) if m]
    check("order after amy", logins == ["0V", "amy", "bob"])

    # Idempotent: re-adding an existing login (any case) is a no-op.
    out2, added2 = add_contributors(out, [{"login": "AMY", "name": "Amy"}])
    check("idempotent case-insensitive", added2 == [] and out2 == out)

    # Existing lines preserved byte-for-byte (bob keeps its exact text).
    check(
        "bob line preserved",
        any(
            ln == '<a href="https://github.com/bob" title="Bob">'
            '<img src="https://github.com/bob.png?size=64" width="64" height="64" alt="Bob" /></a>'
            for ln in out.splitlines()
        ),
    )

    # HTML injection in the display name is escaped.
    out3, _ = add_contributors(sample, [{"login": "carol", "name": '"><script>x</script>'}])
    check("name escaped", "<script>" not in out3 and "&lt;script&gt;" in out3)

    # Invalid logins are dropped.
    _, added4 = add_contributors(
        sample,
        [
            {"login": "bad login", "name": "x"},
            {"login": "-bad", "name": "x"},
            {"login": "github-actions[bot]", "name": "bot"},
        ],
    )
    check("invalid logins dropped", added4 == [])

    # Empty display name falls back to the login.
    out5, _ = add_contributors(sample, [{"login": "dave", "name": ""}])
    check("empty name -> login", 'title="dave"' in out5)

    # Batch insert stays sorted and deduped.
    out6, added6 = add_contributors(
        sample,
        [
            {"login": "zed", "name": "Zed"},
            {"login": "AAA", "name": "Triple A"},
            {"login": "zed", "name": "dup"},
        ],
    )
    check("batch dedup", sorted(added6) == ["AAA", "zed"])
    logins6 = [m.group("login") for m in (_ENTRY_RE.match(ln) for ln in out6.splitlines()) if m]
    # Digits sort before letters case-insensitively, so "0V" precedes "AAA".
    check("batch order", logins6 == ["0V", "AAA", "bob", "zed"])

    # Missing block fails loud.
    try:
        add_contributors("# No contributors here\n", [{"login": "x", "name": "x"}])
        check("missing block raises", False)
    except ValueError:
        check("missing block raises", True)

    # A line-boundary char in the display name must not create a multi-line
    # entry (which would break the contiguous-run invariant on the next run).
    out7, _ = add_contributors(sample, [{"login": "erin", "name": "Er in"}])
    entry7 = [ln for ln in out7.splitlines() if 'github.com/erin"' in ln]
    check("boundary char collapsed to one line", len(entry7) == 1)
    _, added7b = add_contributors(out7, [{"login": "erin", "name": "Er in"}])
    check("boundary entry idempotent", added7b == [])

    # A null login (deleted PR author) is dropped, never "None".
    _, added8 = add_contributors(sample, _coerce_records([{"login": None, "name": None}]))
    check("null login dropped", added8 == [])

    # A name credited by hand OUTSIDE the sorted block is not re-added.
    prose = sample.replace("intro\n", 'intro — thanks <a href="https://github.com/gus">Gus</a>\n')
    _, added9 = add_contributors(prose, [{"login": "gus", "name": "Gus"}])
    check("manual credit outside block not re-added", added9 == [])

    # An opt-out login is never added, even when absent from the README, so a
    # removal request survives a full rebuild.
    _, added10 = add_contributors(sample, [{"login": "quinn", "name": "Quinn"}], optout={"quinn"})
    check("opt-out login not added", added10 == [])

    print("\nPASS" if not failures else f"\n{failures} FAILURE(S)")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Add merged-PR authors to the README Contributors block."
    )
    parser.add_argument("--test", action="store_true", help="run the built-in self-test and exit")
    parser.add_argument("--login", help="single contributor login (alternative to JSON on stdin)")
    parser.add_argument("--name", default="", help="single contributor display name (with --login)")
    parser.add_argument(
        "--readme", default=str(_README), help="path to README.md (default: repo root)"
    )
    parser.add_argument(
        "--optout",
        default=str(_REPO_ROOT / ".github" / "contributors-optout.txt"),
        help="path to the opt-out login list (one login per line, # comments)",
    )
    args = parser.parse_args(argv)

    if args.test:
        return _self_test()

    if args.login is not None:
        records = [{"login": args.login.strip(), "name": args.name.strip()}]
    else:
        try:
            raw = json.load(sys.stdin)
            records = _coerce_records(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"::error::update_contributors: {exc}", file=sys.stderr)
            return 1

    readme_path = Path(args.readme)
    try:
        # newline="" disables universal-newline translation on BOTH read and
        # write, so a CRLF file's line endings reach add_contributors intact
        # (which preserves them) instead of being silently rewritten to LF.
        with open(readme_path, encoding="utf-8", newline="") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"::error::update_contributors: cannot read {readme_path}: {exc}", file=sys.stderr)
        return 2

    optout = _load_optout(Path(args.optout))
    try:
        new_text, added = add_contributors(text, records, optout=optout)
    except ValueError as exc:
        print(f"::error::update_contributors: {exc}", file=sys.stderr)
        return 1

    if not added:
        print("No new contributors; README already up to date.")
        return 0

    try:
        _atomic_write(readme_path, new_text)
    except OSError as exc:
        # Same environment-error contract as the read path: a write failure
        # (unwritable path, full disk) is exit 2, not an uncaught traceback, and
        # the atomic write leaves the original README intact.
        print(f"::error::update_contributors: cannot write {readme_path}: {exc}", file=sys.stderr)
        return 2
    print(f"Added {len(added)} contributor(s): {', '.join(added)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
