"""GitHub-repo skill provider — import a SKILL.md bundle pinned to one commit.

The user already keeps skills in their own repositories; this provider makes such
a repository addressable from the same Discover surface as a public registry, so
the whole install path — the preview, the 1 MiB response cap, the SSRF screen,
the redirect allowlist, the traversal-proof bundle writer and the HUMAN-ONLY
install gate in ``dashboard/handlers/discover.py`` — is inherited rather than
rebuilt. Nothing here is a second way into the skills directory.

**Import, not subscription.** An install copies the files in and the user owns the
result; the copy is pinned to the commit it came from and nothing re-syncs it.
That posture is the whole reason this is safe to ship without a review step: a
repository that changes under the user cannot change an already-imported skill,
because no code ever looks upstream again. Re-running the import is the update
path, and it goes through the same human-only gate as the first one. Branch
tracking / re-sync is deliberately absent (see #746 for the adjacent design).

**Addressing** — ``owner/repo[@ref][:path]``:

- ``acme/widgets`` — every skill in the repository, at the default branch.
- ``acme/widgets@v2`` — at the tag/branch/commit ``v2``.
- ``acme/widgets:skills/reviewer`` — one skill directory.
- ``acme/widgets@v2:skills/reviewer`` — both.
- ``https://github.com/acme/widgets/tree/main/skills/reviewer`` — a pasted tree
  URL is accepted as the same thing. Its first segment after ``tree`` is the ref,
  so a branch name containing ``/`` must use the ``@ref`` form instead.

A *skill* is any directory holding a ``SKILL.md``, so one repository can carry
many; discovery returns one row per directory and each row's id names exactly
that directory.

**What a row's id pins.** Discovery resolves the ref to a commit once and puts the
FULL commit in every row's id, so the preview and the install fetch the commit
discovery actually showed instead of re-resolving a branch that may have moved in
between. The full 40 characters matter: an abbreviated ref is re-resolved, and a
branch whose NAME is hex can shadow a 7-character prefix, which turns a "pinned"
row into one that can be substituted. Belt and braces, ``_resolve_commit`` also
refuses when a ref we asked for IS a full SHA and the answer is a different commit
-- git resolves a ref name before an object name, so even a branch named after 40
hex characters cannot answer for that commit. A hand-typed address naming a branch
is resolved at fetch time, and either way the full commit is recorded in the
installed copy's ``.skill-import-source.json``.

**Where an import lands.** ``install_slug`` names the on-disk key, because the
handler's default -- ``_slugify(id)`` -- is not injective over these ids: it
lowercases and folds ``/``, ``@`` and ``:`` all onto ``-``, so ``foo-bar`` and
``foo/bar`` would share one key and installing the second would delete the first.
The key is ``<label>-<12 hex of sha256(owner/repo:path)>``. The digest covers the
case-sensitive identity and deliberately EXCLUDES the ref: a commit is the version,
not the identity, so re-importing at a newer commit lands on the same key and meets
the install handler's 409, which is what makes re-import an update rather than a
way to accumulate copies.

**A bundle is complete, or it is refused.** Every reason a file could be left out
-- a failed fetch, a body that is not UTF-8, a per-file or total size ceiling, a
file count over the ceiling, two names that collide where case is ignored, a name
outside the allowed shape -- refuses the whole import and logs why, rather than
writing a subset.

**One allowlist decides every path.** A segment must match
``^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$``, at most four levels deep, and no two paths may
be equal under ``casefold``. That is deliberately NARROWER than any filesystem: a
repository carrying ``my file.md`` or a non-ASCII name is refused outright, with the
file named. The alternative -- admit everything writable -- means maintaining a
refusal list, and every round of review found another member of it (Windows-illegal
characters, control characters, trailing dots and spaces, byte-versus-character
limits, Unicode normalisation, reserved device names, component length). Saying what
is permitted leaves nothing to enumerate. Only three rules survive alongside it,
because the shape genuinely permits them: ``..`` anywhere (the writer refuses it), a
trailing dot (Windows strips it), and a reserved device name. A skill missing a file its own instructions
reference does not fail at import; it fails later, somewhere else, as a puzzle.
The only deliberate omissions are ``_EXCLUDED_NAMES``, which are never skill
content. ``fetch_skill_bundle`` can only answer ``None``, so the reason reaches
the log and not yet the user -- an error channel on the Protocol is follow-up work.

**Rate limit.** Requests are unauthenticated, which GitHub limits to 60 per hour
per IP; one discover/preview/install cycle costs two API calls plus one raw fetch
per file. Authenticated fetches (and caching the resolved tree) are follow-up
work, not a gap in the trust posture.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable

from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter
from kiro_crew.skill_providers import _http
from kiro_crew.skill_providers.base import SkillSearchResult

logger = logging.getLogger(__name__)

# GitHub REST API base (no trailing slash).
_API_BASE = "https://api.github.com"

# Raw file host. Blob contents are read from here rather than through the
# contents API: the raw host returns the bytes directly, so a file does not
# arrive base64-inflated inside a JSON envelope that the 1 MiB cap then has to
# cover, and the URL is pinned to the resolved commit.
_RAW_BASE = "https://raw.githubusercontent.com"

# Hosts a fetch may be REDIRECTED to -- exactly the two this module fetches from,
# and nothing else. Every URL starts at ``_API_BASE`` or ``_RAW_BASE``, both
# screened by ``_is_internal_url`` first, and neither endpoint has an observed
# redirect: the API answers directly and the raw host serves bytes directly.
#
# The CDN hosts that ``skillsh`` allows (``codeload``, ``objects``, ``media``,
# ``github.com``) are its bundle-download redirect targets, not ours, so they are
# deliberately absent. ``https://github.com/...`` appears in a result row's
# ``repo_url`` and in the pin record, but those are shown to a person, never
# fetched. Add a host here only for a redirect actually observed from an api or
# raw fetch; until then an unexpected redirect fails the fetch, which is the
# answer we want.
_ALLOWED_HOSTS = frozenset({"api.github.com", "raw.githubusercontent.com"})

# Address grammar. Every part is validated before it reaches a URL: the owner and
# repository shapes are GitHub's own, a ref may not contain a path-traversal or
# an empty segment, and a path segment is held to the same characters the skills
# loader can represent on every platform.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,99}$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Characters no path segment may contain. The first group is what Windows
# genuinely cannot create -- a write would raise OSError out of the bundle
# writer's ``asyncio.to_thread``, where nothing catches it -- and ``/`` is the
# separator itself. Everything NOT listed is allowed ON PURPOSE: a space, a plus,
# a bracket, a non-ASCII letter are all ordinary in a repository and all writable,
# and refusing them breaks the complete-or-refused promise from the other side, by
# making a perfectly good skill unimportable.
# The ONE shape a bundle path segment may take. An allowlist rather than a list of
# refusals, because a refusal list is something a reviewer can keep extending: every
# round found another member (Windows-illegal characters, control characters,
# trailing dots and spaces, byte-vs-character limits, Unicode normalisation forms,
# case variants). This says what is permitted instead, so the set of things left to
# enumerate is empty.
#
# ASCII letters and digits, dot, underscore, hyphen; must START with a letter or
# digit; at most 64 characters. Deliberately NARROWER than what a filesystem can
# store: a repository carrying ``my file.md`` or ``\u65e5\u672c\u8a9e.md`` is refused
# outright, with the file named, rather than imported. That is a real cost, and it
# buys the property that matters more -- an import either lands complete or says
# exactly why it cannot.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Directory depth inside one skill. Four levels covers every shape a skill actually
# has (``rules/``, ``scripts/``, ``assets/`` and one more) and bounds the total path
# without a second length rule.
_MAX_DEPTH = 4

# MS-DOS device names. Windows still reserves them at every directory level and
# with ANY extension, so ``CON.md`` is as unopenable as ``CON`` -- a write raises
# OSError out of the bundle writer's ``asyncio.to_thread``, where nothing catches
# it, and on the overwrite path that happens AFTER the old skill was removed.
_RESERVED_SEGMENT_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)

# A REF segment stays strict -- no leading dash -- because a ref is typed by a
# person and reads like an option when it starts with one. Git forbids a space in
# a ref anyway, so the permissiveness above does not apply here. Matched in ONE go
# rather than split on "/", so an empty, leading, trailing or doubled separator is
# refused by the shape itself and no filesystem-separator assumption is expressed.
_REF_SEGMENT = r"\.?[A-Za-z0-9_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_][A-Za-z0-9_-]*)*"
_REF_RE = re.compile(rf"^{_REF_SEGMENT}(?:/{_REF_SEGMENT})*$")

# An owner/repo address, optionally in pasted tree/blob-URL form.
_ADDRESS_RE = re.compile(
    r"^(?P<owner>[^/@:]+)/(?P<repo>[^/@:]+)(?:/(?P<kind>tree|blob)/(?P<rest>.+))?$"
)

# The ref's ceiling, applied before its pattern runs so a regex never sees an
# unbounded string. Paths need no equivalent: ``_SEGMENT_RE`` caps each segment and
# ``_MAX_DEPTH`` caps how many there are.
_MAX_REF_CHARS = 256

# The installed skill's on-disk key comes from ``install_slug`` below, NOT from
# the address, and there is deliberately no length bound on the address either:
# bounding the length only addresses TRUNCATION, and ``foo-bar`` collides with
# ``foo/bar`` at any length because ``_slugify`` folds ``/`` onto ``-``. A digest
# is the only thing that closes both.
#
# 12 hex is 48 bits inside one user's own skills directory, and the label keeps
# the key recognisable on disk. 32 + 1 + 12 = 45 characters, comfortably inside
# the handler's 64-character budget, so the key is never truncated.
_KEY_DIGEST_CHARS = 12
_KEY_LABEL_CHARS = 32

# Prefixes stripped so a pasted repository or tree URL parses as an address.
_URL_PREFIXES = (
    "https://github.com/",
    "http://github.com/",
    "https://www.github.com/",
    "www.github.com/",
    "github.com/",
)

# Discovery ceilings. A repository may hold any number of skills; a search
# response is bounded so one address cannot turn into an unbounded fan of raw
# fetches, and the caller's own ``limit`` narrows it further.
_MAX_SKILLS_PER_REPO = 20

# One bundle's ceilings. ``_MAX_BUNDLE_BYTES`` is the running total across every
# file, deliberately equal to the per-response cap: the discover handler's own
# guard is 5 MiB, so this is the one that binds first.
_MAX_BUNDLE_FILES = 50
_MAX_BUNDLE_BYTES = _http.MAX_RESPONSE_BYTES

# The pin record written into every imported skill directory. Dot-prefixed to
# match the loader's other sidecar (``.builtin-skill-provenance``) and so it
# never reads as part of the skill's own content.
PIN_FILENAME = ".skill-import-source.json"

# Git plumbing is not skill content, so these are dropped silently and by design.
# PIN_FILENAME is deliberately NOT here: dropping a repository's own file of that
# name would be one more silent omission, so it is reserved in the collision check
# instead, which refuses the import and says which file caused it.
_EXCLUDED_NAMES = frozenset({".gitattributes", ".gitignore", ".gitmodules"})


@dataclass(frozen=True)
class RepoSpec:
    """One parsed ``owner/repo[@ref][:path]`` address."""

    owner: str
    repo: str
    ref: str = ""
    """Requested ref — a branch, tag, or (abbreviated) commit. '' = default branch."""

    path: str = ""
    """Repository-relative directory. '' = repository root."""

    @property
    def repo_slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    def address(self, *, ref: str | None = None, path: str | None = None) -> str:
        """Re-render this address, optionally substituting the ref or path."""
        use_ref = self.ref if ref is None else ref
        use_path = self.path if path is None else path
        out = f"{self.owner}/{self.repo}"
        if use_ref:
            out += f"@{use_ref}"
        if use_path:
            out += f":{use_path}"
        return out


def _segments(path: str) -> tuple[str, ...]:
    """The path's segments, read as a POSIX path because that is what it is.

    A repository path is ``/``-separated on every host, so ``PurePosixPath`` states
    the flavour instead of assuming the local separator. It collapses a doubled
    separator, which is why ``_path_problem`` tests for one itself.
    """
    return PurePosixPath(path).parts


def _path_problem(path: str) -> str:
    """Why *path* cannot be imported, or ``''`` when it can.

    A REASON rather than a boolean, because the answer is user-facing now: a path
    this refuses refuses the whole bundle, so it has to say which file and why
    instead of leaving a silent gap.

    One allowlist (:data:`_SEGMENT_RE`) plus the three things it cannot express, and
    deliberately narrower than what a filesystem can store. The alternative --
    admit everything writable -- means carrying a refusal list that grows every time
    someone thinks of another platform quirk, and each addition is another chance to
    accept something the writer then drops silently. Refusing a name outright, with
    the name in the message, is the failure mode worth having.

    ``''`` is the repository root and always fine.
    """
    if not path:
        return ""
    segments = _segments(path)
    # The path must be exactly what its own segments rejoin to. ``_segments`` reads a
    # POSIX path, which normalises a doubled, leading or trailing separator away --
    # so without this the malformed ``a//b.md``, ``/a.md`` and ``a/`` all pass by
    # being quietly rewritten. One comparison covers every such shape, which is why
    # there is no separate rule for any of them.
    if "/".join(segments) != path:
        return "its path is not in normal form (an empty, leading or trailing '/')"
    if len(segments) > _MAX_DEPTH:
        return f"it is {len(segments)} levels deep, over the {_MAX_DEPTH}-level limit"
    # The install writer refuses any path CONTAINING "..", bluntly and with no log,
    # and the segment shape permits it (``a..b.md`` is letters, dots and letters),
    # so this rule has to stay: a file accepted here and dropped there installs a
    # skill missing one of its own files.
    if ".." in path:
        return "its name contains '..', which the bundle writer refuses"
    for segment in segments:
        if not _SEGMENT_RE.match(segment):
            return (
                f"{segment[:48]!r} is not an allowed name: ASCII letters, digits, "
                "'.', '_' and '-' only, starting with a letter or digit, at most "
                "64 characters"
            )
        # Also permitted by the shape, and also not survivable: Windows strips a
        # trailing dot, so ``notes.`` and ``notes`` are two entries here and one
        # file there.
        if segment.endswith("."):
            return f"{segment!r} ends in a dot, which Windows strips"
        stem = segment.partition(".")[0].casefold()
        if stem in _RESERVED_SEGMENT_STEMS:
            return f"{segment!r} uses the reserved device name {stem!r}"
    return ""


def _valid_relative_path(path: str) -> bool:
    """Whether *path* can be imported at all. See :func:`_path_problem`."""
    return not _path_problem(path)


def _valid_ref(ref: str) -> bool:
    """True iff *ref* is a safe git ref to place in a URL path."""
    if not ref:
        return True  # default branch
    return len(ref) <= _MAX_REF_CHARS and bool(_REF_RE.match(ref))


def parse_repo_spec(raw: Any) -> RepoSpec | None:
    """Parse one address into a :class:`RepoSpec`, or None if it is not one.

    None is the answer for every string that is not a repository address, which
    is what makes this provider safe to leave in the aggregate search: a plain
    search term ("docker compose") parses to None and costs no network call.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    lowered = text.lower()
    for prefix in _URL_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.strip("/")
    if not text:
        return None

    head, has_path, path = text.partition(":")
    repo_part, has_ref, ref = head.partition("@")
    matched = _ADDRESS_RE.match(repo_part)
    if matched is None:
        return None
    owner, repo = matched.group("owner"), matched.group("repo")

    if matched.group("kind") is not None:
        # A pasted tree/blob URL: owner/repo/tree/<ref>/<path...>. It carries its
        # own ref and path, so combining it with @ref or :path is ambiguous and
        # refused rather than silently resolved one way. The first remaining
        # segment is the ref and the rest is the path, which is why a branch name
        # containing "/" needs the @ref form.
        if has_ref or has_path:
            return None
        ref, _, path = matched.group("rest").partition("/")
        # A pasted URL is percent-encoded, so ``rules/my file.md`` arrives as
        # ``rules/my%20file.md``; left encoded it would be quoted again into
        # ``%2520`` and never resolve. Decoding happens BEFORE validation, never
        # after -- ``%2e%2e`` becomes ``..`` and has to meet the same refusal as a
        # literal one.
        ref = urllib.parse.unquote(ref)
        path = urllib.parse.unquote(path)

    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    # rstrip only: a TRAILING slash is a harmless paste artefact ("…/reviewer/"),
    # but stripping a LEADING one would silently turn the malformed ":/absolute"
    # into the perfectly valid relative "absolute". An address that starts at the
    # root is refused below, not corrected.
    ref = ref.rstrip("/")
    path = path.rstrip("/")

    if not _OWNER_RE.match(owner) or not _REPO_RE.match(repo):
        return None
    if not _valid_ref(ref) or not _valid_relative_path(path):
        return None
    return RepoSpec(owner=owner, repo=repo, ref=ref, path=path)


class GitHubRepoProvider:
    """Provider that imports skills from a GitHub repository, pinned to a commit.

    Takes no configuration. There is no ``enabled`` flag -- the composed
    ``discovery`` policy gate in ``_build_registry`` is the off-switch, and a
    second way to disable one provider is a second thing that has to stay true --
    and no settable ``api_base``, because every URL this module builds starts at
    ``_API_BASE`` or ``_RAW_BASE`` and both have to stay inside
    ``_ALLOWED_HOSTS``. A config object holding one unsettable field is a shape
    without a requirement.
    """

    @property
    def api_base(self) -> str:
        """The API base this provider fetches from -- the policy allowlist key.

        A property rather than a bare constant because the policy gate allowlists
        a source by URL, so every provider has to be able to state its own.
        """
        return _API_BASE

    @property
    def name(self) -> str:
        return "github"

    @property
    def display_name(self) -> str:
        return "GitHub repo"

    def install_slug(self, skill_id: str) -> str:
        """The local key for *skill_id* -- a digest of the skill's identity.

        Optional protocol surface the discover handler probes for (see
        ``base.py``). It exists because the handler's default, ``_slugify(id)``, is
        NOT injective over these ids: it lowercases and folds ``/``, ``@`` and
        ``:`` all onto ``-``, so ``foo-bar/SKILL.md`` and ``foo/bar/SKILL.md``
        produce ONE key and installing the second deletes the first. No id this
        provider could invent avoids that, which is why the key is computed here
        rather than read off the address.

        Identity is ``owner/repo:path`` and deliberately EXCLUDES the ref: a commit
        is the version, not the identity. Re-importing the same skill at a newer
        commit therefore lands on the same key and meets the install handler's
        existing 409, so the user is asked to confirm an update -- which is what
        makes re-import the documented update path instead of a way to accumulate
        copies.
        """
        spec = parse_repo_spec(skill_id)
        if spec is None:
            return ""
        identity = f"{spec.owner}/{spec.repo}:{spec.path}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:_KEY_DIGEST_CHARS]
        label = _key_label(posixpath.basename(spec.path) or spec.repo)
        return f"{label}-{digest}" if label else digest

    def is_available(self) -> bool:
        """Always ready: it needs no credential, and the policy gate is the switch.

        The provider reaches GitHub unauthenticated, so there is no configuration
        that could be missing and nothing to probe. ``provider_available`` still
        calls this because it is protocol surface every provider carries.
        """
        return True

    async def search(self, query: str, *, limit: int = 20) -> list[SkillSearchResult]:
        """Resolve *query* as a repository address and list the skills it holds.

        This provider has no catalog to search: a query that is not an address
        yields nothing without touching the network, so leaving it in the
        aggregate fan-out costs a regex on every unrelated search.
        """
        spec = parse_repo_spec(query)
        if spec is None:
            return []

        commit = await self._resolve_commit(spec)
        if commit is None:
            return []
        blobs = await self._list_blobs(spec, commit)
        if not blobs:
            return []

        skill_dirs = _skill_directories(blobs)
        if not skill_dirs:
            return []
        capped = skill_dirs[: max(1, min(limit, _MAX_SKILLS_PER_REPO))]

        # One raw fetch per skill, concurrently: the frontmatter is what gives a
        # row its real name and description, and it is the same file the preview
        # and the install will read. A failed fetch degrades that row to its
        # directory name rather than dropping it — the skill is genuinely there.
        heads = await asyncio.gather(*[self._fetch_skill_md(spec, commit, d) for d in capped])

        results: list[SkillSearchResult] = []
        for skill_dir, head in zip(capped, heads, strict=True):
            full_path = posixpath.join(spec.path, skill_dir) if skill_dir else spec.path
            # The FULL commit, never an abbreviation. A 7-hex ref gets re-resolved
            # at preview and install time, and a branch whose NAME is hex can
            # shadow that prefix -- so the row would name one commit and the
            # install fetch another. The full SHA, plus the equality check in
            # ``_resolve_commit``, closes that substitution.
            address = spec.address(ref=commit, path=full_path)
            problem = _path_problem(full_path)
            if problem:
                # Offering a row that cannot be installed is worse than omitting
                # it: the user would only find out at the refusal.
                logger.warning("Skipping GitHub skill %r because %s", address, problem)
                continue
            fallback_name = posixpath.basename(full_path) or spec.repo
            meta = _frontmatter_of(head)
            results.append(
                SkillSearchResult(
                    id=address,
                    name=meta.get("name") or fallback_name,
                    description=meta.get("description", ""),
                    provider=self.name,
                    # Carries the FULL pinned commit and is directly viewable, so
                    # the row itself shows what a row's id abbreviates.
                    repo_url=_tree_url(spec, commit, full_path),
                    author=spec.owner,
                    tags=[],
                )
            )
        return results

    async def fetch_skill_content(self, skill_id: str) -> str | None:
        """Fetch one skill's instruction file. See ``fetch_skill_bundle``."""
        bundle = await self.fetch_skill_bundle(skill_id)
        if bundle is None:
            return None
        # Same precedence the install writer applies (SKILL.md, else AGENTS.md
        # copied to SKILL.md, else any markdown), so a preview reads the file the
        # installed skill will actually expose.
        for wanted in ("SKILL.md", "AGENTS.md"):
            named = next((f for f in bundle if f[0] == wanted), None)
            if named:
                return named[1]
        any_md = next((f for f in bundle if f[0].endswith(".md")), None)
        return any_md[1] if any_md else None

    async def fetch_skill_bundle(self, skill_id: str) -> list[tuple[str, str]] | None:
        """Fetch one skill directory as ``(relative_path, content)`` pairs.

        *skill_id* must address a single skill — a directory that itself holds a
        ``SKILL.md``. Files belonging to a NESTED skill are excluded, so
        importing a repository's root when it carries several skills cannot rake
        all of them into one install.

        The last entry is always :data:`PIN_FILENAME`, recording the full commit
        this content came from. It is appended here rather than written by the
        install handler so the preview lists exactly the files the install will
        write, and a repository's own file of that name is dropped instead of
        being trusted as provenance.
        """
        spec = parse_repo_spec(skill_id)
        if spec is None:
            return None
        commit = await self._resolve_commit(spec)
        if commit is None:
            return None
        blobs = await self._list_blobs(spec, commit)
        if not blobs:
            return None

        own_files = _own_skill_files(blobs)
        if own_files is None:
            logger.debug(
                "GitHub address %r holds no SKILL.md at its root; refusing bundle",
                spec.address(),
            )
            return None

        wanted = [
            (path, size)
            for path, size in own_files
            if posixpath.basename(path) not in _EXCLUDED_NAMES
        ]
        if not wanted:
            return _refuse(spec, "it holds no importable file")
        if len(wanted) > _MAX_BUNDLE_FILES:
            return _refuse(
                spec, f"it holds {len(wanted)} files, over the {_MAX_BUNDLE_FILES} ceiling"
            )
        oversized = [path for path, size in wanted if size > _MAX_BUNDLE_BYTES]
        if oversized:
            return _refuse(spec, f"{oversized[0]!r} is over the {_MAX_BUNDLE_BYTES}-byte ceiling")
        # One question for the whole set, and PIN_FILENAME is part of it: the pin is
        # appended AFTER this point, so checking only the repository's own paths
        # would let a case-variant of our filename through and then overwrite it.
        # Refusing by NAME rather than filtering is the point -- a path the writer
        # cannot write is not a file to skip quietly, it is a reason this import
        # cannot be complete.
        problem = _materialisation_problem([path for path, _ in wanted])
        if problem:
            return _refuse(spec, problem)

        fetched = await asyncio.gather(
            *[self._fetch_blob(spec, commit, path) for path, _ in wanted]
        )

        bundle: list[tuple[str, str]] = []
        total = 0
        for (path, _), raw in zip(wanted, fetched, strict=True):
            # None is a FAILED fetch, which is distinct from bytes that are not
            # text: the first means the file exists and we could not read it, and
            # installing without it would report success for a skill missing a
            # file its own instructions may reference.
            if raw is None:
                return _refuse(spec, f"{path!r} could not be fetched")
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                return _refuse(spec, f"{path!r} is not UTF-8 text")
            total += len(raw)
            if total > _MAX_BUNDLE_BYTES:
                return _refuse(spec, f"its files total over {_MAX_BUNDLE_BYTES} bytes")
            bundle.append((path, content))

        # The writer copies AGENTS.md to SKILL.md only when SKILL.md is ABSENT, so a
        # present-but-empty SKILL.md shadows a perfectly good AGENTS.md and installs
        # a skill the loader lists and the agent cannot use. Checking "either one has
        # content" is not enough for that reason.
        files = dict(bundle)
        if "SKILL.md" in files:
            if not files["SKILL.md"].strip():
                return _refuse(spec, "its SKILL.md is empty")
        elif not files.get("AGENTS.md", "").strip():
            return _refuse(spec, "it has no usable instruction file")

        bundle.append((PIN_FILENAME, _pin_record(spec, commit, bundle)))
        return bundle

    # ---- GitHub plumbing -------------------------------------------------

    async def _resolve_commit(self, spec: RepoSpec) -> str | None:
        """Resolve *spec*'s ref to a full commit SHA, or None.

        Asks for the ``sha`` media type rather than the commit object: the object
        carries the commit's whole file list, which for a large merge exceeds the
        1 MiB response cap and would fail to resolve a perfectly good ref.
        ``HEAD`` stands for the repository's default branch, so the default case
        costs no extra request.
        """
        ref = spec.ref or "HEAD"
        url = (
            f"{_API_BASE}/repos/"
            f"{urllib.parse.quote(spec.owner)}/{urllib.parse.quote(spec.repo)}"
            f"/commits/{urllib.parse.quote(ref, safe='/')}"
        )
        raw = await _fetch_text(url, accept="application/vnd.github.sha")
        if not isinstance(raw, str):
            return None
        sha = raw.strip().lower()
        # GitHub is external input: only a full 40-hex SHA may become part of a
        # URL and of the recorded pin.
        if not _FULL_SHA_RE.match(sha):
            return None
        # When the ref we ASKED for is itself a full SHA, the answer has to be that
        # SHA. Git resolves a ref name before an object name, so a branch named
        # after 40 hex characters would otherwise answer with its own tip and
        # quietly substitute the content a pinned row promised.
        requested = ref.lower()
        if _FULL_SHA_RE.match(requested) and sha != requested:
            logger.warning(
                "Refusing %r: a ref named like a commit resolved to %s instead",
                spec.address(),
                sha,
            )
            return None
        return sha

    async def _list_blobs(self, spec: RepoSpec, commit: str) -> list[tuple[str, int]] | None:
        """List ``(path, size)`` for every blob under *spec*'s path at *commit*.

        Paths are relative to ``spec.path``, because the tree is requested as
        ``<commit>:<path>`` when a path is given — that both narrows the response
        under the 1 MiB cap and makes the returned paths the bundle's own
        relative paths.

        A ``truncated`` tree is refused outright. Using a partial listing would
        silently install a skill missing files, which is worse than reporting
        that the repository is too large to import.
        """
        tree_ref = f"{commit}:{spec.path}" if spec.path else commit
        url = (
            f"{_API_BASE}/repos/"
            f"{urllib.parse.quote(spec.owner)}/{urllib.parse.quote(spec.repo)}"
            f"/git/trees/{urllib.parse.quote(tree_ref, safe=':/')}?recursive=1"
        )
        data = await _fetch_json(url)
        if not isinstance(data, dict):
            return None
        if data.get("truncated") is True:
            logger.warning(
                "GitHub tree for %r is truncated; refusing a partial import",
                spec.address(),
            )
            return None
        entries = data.get("tree")
        if not isinstance(entries, list):
            return None

        blobs: list[tuple[str, int]] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "blob":
                continue
            path = entry.get("path")
            if not isinstance(path, str) or not path:
                continue
            # Deliberately NOT filtered here. The listing reports what the
            # repository HAS; whether a path can be imported is decided once, in
            # ``fetch_skill_bundle``, where an unwritable path refuses the bundle
            # by name. Dropping it here would make that refusal unreachable and
            # install the skill without the file instead.
            try:
                size = int(entry.get("size", 0) or 0)
            except (TypeError, ValueError):
                size = 0
            blobs.append((path, size))
        return blobs

    async def _fetch_blob(self, spec: RepoSpec, commit: str, rel_path: str) -> bytes | None:
        """Fetch one blob's raw bytes from the raw host, pinned to *commit*.

        Bytes rather than text so the caller can tell a FAILED fetch (``None``)
        from a body that is simply not UTF-8 -- one is an error to refuse on, the
        other a fact about the file, and a text fetch collapses both to ``None``.
        """
        full_path = posixpath.join(spec.path, rel_path) if spec.path else rel_path
        return await _fetch_bytes(_raw_url(spec, commit, full_path))

    async def _fetch_skill_md(self, spec: RepoSpec, commit: str, skill_dir: str) -> str | None:
        """Fetch the SKILL.md of one discovered skill directory, or None.

        Discovery only wants the frontmatter, so a file that is unreadable or not
        text degrades that ROW to its directory name; unlike an install, showing
        a skill that is really there costs nothing.
        """
        rel = posixpath.join(skill_dir, "SKILL.md") if skill_dir else "SKILL.md"
        raw = await self._fetch_blob(spec, commit, rel)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None


def _key_label(raw: str) -> str:
    """A short, readable prefix for an install key.

    Cosmetic only -- the digest carries the uniqueness -- so it is reduced to the
    characters ``_slugify`` keeps verbatim. That makes the handler's slugify a
    no-op on the finished key, so it can neither shorten nor alter it.

    The edges are stripped of ``.``, ``_`` and ``-`` as well, because the finished
    key has to START with an alphanumeric to satisfy the install handler's
    ``_SAFE_SLUG_RE``. A repository called ``.github`` would otherwise produce
    ``.github-<digest>``, which that gate refuses -- a 400 on a skill that is
    perfectly importable. When nothing survives, the caller falls back to the digest
    alone, which is hex and therefore always starts with an alphanumeric.
    """
    lowered = re.sub(r"[^a-z0-9._-]+", "-", raw.strip().lower())
    return lowered.strip("-._")[:_KEY_LABEL_CHARS].strip("-._")


def _refuse(spec: RepoSpec, because: str) -> "list[tuple[str, str]] | None":
    """Log why an import is refused and return None, the Protocol's only answer.

    ``fetch_skill_bundle`` can say nothing but ``None``, which the install handler
    renders as "not found or empty". The reason therefore lives in the log; giving
    the Protocol an error channel so the user reads it instead is follow-up work.
    A refusal is always preferred to a partial install: a skill missing a file it
    references fails later, somewhere else, as a puzzle.
    """
    logger.warning("Refusing GitHub skill %r: %s", spec.address(), because)
    return None  # typed as the bundle's Optional so callers can `return _refuse(...)`


def _materialisation_problem(paths: "Iterable[str]") -> str:
    """Why this exact SET of files cannot be created in one directory, or ''.

    One question asked once, because every member of this family has the same
    consequence: a set that validates and then fails mid-write. On the overwrite
    path the install writer removes the previous skill BEFORE writing, and a write
    that raises there escapes an ``asyncio.to_thread`` with no handler -- so the
    old skill is gone and the new one is partial, with no recovery. Refusing up
    front is the only place that can be prevented.

    Two ways a legal git tree fails to become a directory, both about the SET rather
    than any one member:

    * two paths that differ only in case are one file on macOS and Windows, so the
      second write silently replaces the first;
    * one folded name used as BOTH a file and a directory (``Rules`` beside
      ``rules/a.md``) -- legal in git, impossible on a case-insensitive filesystem,
      and the ``mkdir`` raises.

    Everything that is wrong with a single path is :func:`_path_problem`'s, called
    here first so one refusal answers both questions. Case folding is the whole of
    the equivalence because the allowlist admits ASCII only -- there is no
    normalisation form to disagree about. ``PIN_FILENAME`` needs no reservation for
    the same reason: it starts with a dot, which no repository path may, so nothing
    can collide with it.
    """
    kinds: dict[str, tuple[str, str]] = {}
    for path in paths:
        problem = _path_problem(path)
        if problem:
            return f"{path!r} cannot be imported: {problem}"
        parts = _segments(path)
        for index, segment in enumerate(parts):
            prefix = "/".join(part.casefold() for part in parts[: index + 1])
            kind = "file" if index == len(parts) - 1 else "directory"
            previous = kinds.get(prefix)
            if previous is None:
                kinds[prefix] = (kind, path)
                continue
            previous_kind, previous_path = previous
            if previous_kind != kind:
                return (
                    f"{path!r} and {previous_path!r} need {segment!r} to be both a "
                    "file and a directory, which one filesystem cannot do"
                )
            if kind == "file":
                # Any second occurrence, EXACT or case-variant. The exact case is
                # not "the same file": the tree lists each path once, so a repeat
                # means two different contents want one path -- which is precisely
                # how a repository's own copy of PIN_FILENAME meets the pin we
                # append. Treating it as harmless is what let that through.
                if previous_path == path:
                    return f"{path!r} is claimed twice, by two different contents"
                return f"{path!r} and {previous_path!r} name one file where case is ignored"
    return ""


def _skill_directories(blobs: list[tuple[str, int]]) -> list[str]:
    """Directories (relative to the listed root) that hold a ``SKILL.md``.

    ``''`` denotes the listed root itself. Sorted so discovery order is stable
    across calls rather than following GitHub's tree order.
    """
    return sorted(
        {posixpath.dirname(path) for path, _ in blobs if posixpath.basename(path) == "SKILL.md"}
    )


def _own_skill_files(blobs: list[tuple[str, int]]) -> list[tuple[str, int]] | None:
    """Files belonging to the skill at the listed root, or None if there is none.

    A repository root holding several skills is a container, not a skill: every
    row discovery returns names a leaf directory, so this only refuses a
    hand-typed address, and refusing is what keeps one install from raking in
    every skill in the repository.
    """
    if not any(path in ("SKILL.md", "AGENTS.md") for path, _ in blobs):
        return None
    nested = {
        directory
        for directory in _skill_directories(blobs)
        if directory  # the root's own SKILL.md is not a nested skill
    }
    return sorted(
        (path, size)
        for path, size in blobs
        if not any(path.startswith(f"{directory}/") for directory in nested)
    )


def _frontmatter_of(content: str | None) -> dict[str, str]:
    """Parse *content*'s frontmatter with the skills loader's own grammar.

    Using the loader's grammar is what makes a discovered row's name and
    description equal the installed skill's — a second parser would disagree on
    block scalars. Repository content is untrusted, so a parse failure yields an
    empty mapping rather than propagating.
    """
    if not content:
        return {}
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    try:
        meta = parse_frontmatter(normalized, SKILL_LOADER)
    except Exception:
        logger.debug("Unparseable SKILL.md frontmatter from GitHub", exc_info=True)
        return {}
    if not isinstance(meta, dict):
        return {}
    return {k: v for k, v in meta.items() if isinstance(k, str) and isinstance(v, str)}


def _tree_url(spec: RepoSpec, commit: str, path: str) -> str:
    """A browsable URL for exactly the imported content, pinned to *commit*."""
    base = f"{spec.repo_url}/tree/{commit}"
    return f"{base}/{path}" if path else base


def _raw_url(spec: RepoSpec, commit: str, path: str) -> str:
    """The raw-content URL for one file, pinned to *commit*."""
    # safe="/" keeps the separators as real path segments while still encoding
    # "?", "#", space and the rest -- the grammar has already refused anything
    # that could smuggle a query string in.
    return (
        f"{_RAW_BASE}/{urllib.parse.quote(spec.owner)}/{urllib.parse.quote(spec.repo)}"
        f"/{commit}/{urllib.parse.quote(path, safe='/')}"
    )


def _pin_record(spec: RepoSpec, commit: str, bundle: list[tuple[str, str]]) -> str:
    """The JSON provenance written beside an imported skill.

    It records what the copy IS rather than what it should become: the exact
    commit, the address it was requested by, the browsable URL and the files
    taken. Nothing reads it back to re-sync — it exists so a user (or a later
    re-import) can tell where a skill came from and whether it has moved on.
    """
    record = {
        "provider": "github",
        "repo": spec.repo_slug,
        "repo_url": spec.repo_url,
        "requested_ref": spec.ref,
        "commit": commit,
        "path": spec.path,
        "source_url": _tree_url(spec, commit, spec.path),
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": [path for path, _ in bundle],
        "tracking": (
            "none — this is a pinned copy you own. Re-import the same address to "
            "pick up newer upstream content."
        ),
    }
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


# ---- this provider's binding of the shared network guards -----------------


def _audit_ssrf_blocked(url: str, host: str, canonical_host: str) -> None:
    """Emit a SEL audit event for a blocked SSRF-to-internal-IP attempt here."""
    _http.audit_ssrf_blocked("github", url, host, canonical_host)


def _is_internal_url(url: str) -> bool:
    """This provider's binding of the shared internal-address screen."""
    return _http.is_internal_url(url, audit=_audit_ssrf_blocked)


def _is_allowed_host(url: str) -> bool:
    """True iff *url* is HTTPS on a host this provider may be redirected to."""
    return _http.is_allowed_host(url, _ALLOWED_HOSTS)


def _sync_fetch_json(url: str) -> Any | None:
    """Blocking, bounded, SSRF-screened JSON fetch."""
    return _http.sync_fetch_json(
        url,
        allowed_hosts=_ALLOWED_HOSTS,
        internal_check=_is_internal_url,
        headers={"Accept": "application/vnd.github+json"},
    )


def _sync_fetch_text(url: str, accept: str | None = None) -> str | None:
    """Blocking, bounded, SSRF-screened UTF-8 text fetch."""
    return _http.sync_fetch_text(
        url,
        allowed_hosts=_ALLOWED_HOSTS,
        internal_check=_is_internal_url,
        headers={"Accept": accept} if accept else None,
    )


def _sync_fetch_bytes(url: str) -> bytes | None:
    """Blocking, bounded, SSRF-screened raw fetch."""
    return _http.sync_fetch_bytes(
        url,
        allowed_hosts=_ALLOWED_HOSTS,
        internal_check=_is_internal_url,
    )


async def _fetch_json(url: str) -> Any | None:
    """Off-loop JSON fetch. Calls the module global so a test can patch it."""
    return await _http.run_off_loop(lambda: _sync_fetch_json(url))


async def _fetch_text(url: str, *, accept: str | None = None) -> str | None:
    """Off-loop text fetch. Calls the module global so a test can patch it."""
    return await _http.run_off_loop(lambda: _sync_fetch_text(url, accept))


async def _fetch_bytes(url: str) -> bytes | None:
    """Off-loop raw fetch. Calls the module global so a test can patch it."""
    return await _http.run_off_loop(lambda: _sync_fetch_bytes(url))
