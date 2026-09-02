"""The Disconnect ownership oracle: who owns an MCP entry and its grant.

Relocated verbatim from ``kiro_crew.dashboard.handlers.connections`` so the
destructive-decision logic — the spec census, the entry/grant identity
judgement, and the single-lock purge-and-revoke transaction — is owned and
evolved apart from the HTTP layer. The handler keeps the route-facing concerns
(auth, body parsing, mint teardown, reading the open project directories off
dashboard slot state, audit, response shape) and calls
:func:`remove_provider_entry` with that snapshot, so nothing in this module
touches dashboard state.

The public names are deliberately NOT re-exported from
``kiro_crew.connections.__init__``: a package-level re-export would put this
module on the import path of every ``from kiro_crew.connections import …``,
and the census helpers are collaborators of one destructive endpoint, not
package API. Import them from this module directly.

The docstrings and comments below carry the safety reasoning that shaped this
code across the disconnect review rounds; they moved with it unchanged. The
behavior is pinned by ``test/test_connections_disconnect.py``: the census
helpers directly, the judge and transaction through the disconnect endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_MIRROR_PREFIX = "mirror:"
_RENDERED_AGENT_FILE = "kirocrew.json"


def is_scope_label(label: str) -> bool:
    """Whether ``label`` names an mcp.json scope the purge can be restricted to.

    Agent specs (``agent:``) are the user's own files and mirrors (``mirror:``)
    are stripped by the purge unconditionally, so neither is a scope name.
    """
    return not label.startswith(("agent:", _MIRROR_PREFIX))


def _census_source_name(label: str) -> str:
    """A census label as something to SHOW the user.

    Only the ``agent:`` prefix comes off: what remains is a spec filename or, for a
    project source, its full path -- a place the user can go and look. Scope names
    (``kirocrew``, the edition's extra scopes) and ``mirror:`` labels are left
    verbatim, because they name a Kiro Crew-managed source rather than a file the
    user wrote, and stripping their prefix would make them read like one.
    """
    return label[len("agent:") :] if label.startswith("agent:") else label


@dataclass(frozen=True)
class DisconnectScope:
    """What one Disconnect did, decided and acted on inside a single lock."""

    entry_removed: bool
    grant_shared_with: tuple[str, ...]
    grant_removed: tuple[str, ...]
    census_incomplete: bool
    # The sources the census could not READ, ready to show. ``census_incomplete``
    # is the same fact as a boolean, but a card that says "fix the unreadable file"
    # while naming no file asks for a repair the user cannot locate. Only the
    # unreadable SOURCES land here: the other half of ``census_incomplete`` is an
    # entry whose URL could not be safely compared, which names no file at all, so
    # this stays empty there rather than pointing at something innocent.
    census_unreadable: tuple[str, ...]
    # The owned pairs this Disconnect actually TRIED to unlink -- the only ones
    # worth re-stat'ing. A pair deliberately kept (a sharer, or a census gap) is
    # still on disk BY DESIGN, so re-stat'ing it would report a correct refusal as
    # a surviving artifact. Restricting the re-stat to attempts is what makes a
    # survivor unambiguously a FAILED unlink.
    attempted_urls: tuple[str, ...]


def _json_spec_names(spec_dir: Path) -> list[str] | None:
    """The ``*.json`` spec names in ``spec_dir``; ``None`` when it is unreadable.

    ``os.listdir``, not ``Path.glob``: glob SUPPRESSES scan errors (an
    executable-but-unlistable directory yields zero entries with no raise), which
    reads a hidden sharer as absent. listdir raises, and a MISSING directory is
    genuine absence -- a fresh machine has no user-level agents dir and most
    checkouts have no ``.kiro/agents`` at all, so that case must not fail closed
    or no one could ever disconnect anything.
    """
    try:
        return sorted(n for n in os.listdir(spec_dir) if n.endswith(".json"))
    except FileNotFoundError:
        return []
    except OSError:
        return None


def agent_spec_sources(project_dirs: tuple[Path, ...] = ()) -> list[tuple[str, Path]]:
    """Every agent spec file that can define an MCP server, each labelled.

    Custom agent configs are the census gap that let a Disconnect delete a grant
    another agent was still using. Discovery merges exactly ONE agent spec --
    ``kirocrew.json``, see :func:`mcp_discovery._load_agent_config` -- so a spec
    the user wrote by hand, or one an app materialized, is invisible to the scope
    sweep even though kiro-cli authorizes from it like any other. Its server's
    grant is the SAME artifact pair, named by the endpoint rather than by who
    configured it, so deleting ours deauthorizes theirs.

    TWO read scopes, because kiro-cli has two. ``kiro_agents_dir`` is the
    user-level one; ``project_agents_dir`` is ``<project>/.kiro/agents``, which
    kiro-cli resolves FIRST for a session launched in that checkout. A spec
    committed in the user's repo therefore holds a grant exactly like a
    hand-written user-level one, and reading only the user-level directory
    revoked it. Project specs are ordinary ``agent:`` sources: sharers that can
    block a revoke, never purge targets (nothing in Kiro Crew writes a
    version-controlled file) and never mirrors (nothing here reflects them).
    Their labels carry the full path, because two checkouts can both hold a
    ``dev.json`` and one label per file is what keeps their entries in separate
    census buckets instead of letting the first one read hide the second.

    The whole directory is globbed rather than an allowlist consulted: the point
    is the specs Kiro Crew does NOT own. Provider agent sidecars
    (``scope.agent_mcp_file``) are included for the same reason -- the purge
    strips them, so they must be able to speak for their own entries.
    """
    from kiro_crew.config.paths import kiro_agents_dir, project_agents_dir
    from kiro_crew.dashboard.handlers.mcp import _extra_mcp_scopes

    sources: list[tuple[str, Path]] = []
    agents_dir = kiro_agents_dir()
    names = _json_spec_names(agents_dir)
    if names is None:
        # The directory itself is unreadable, so nothing under it can be
        # enumerated. Reported as one unreadable source rather than as "no custom
        # agents", which is the reading that deletes a grant. Returning early is
        # safe precisely because this answer already fails the whole census
        # closed: no project source could change the decision.
        return [(f"agent:{agents_dir.name}/", agents_dir)]
    # MIRROR vs USER SPEC. ``_purge_server_config`` strips the entry from the
    # rendered ``kirocrew.json`` and from every ``scope.agent_mcp_file`` itself,
    # so those entries are REFLECTIONS of the scopes this transaction is purging,
    # not independent grant holders. A hand-written agent spec is the opposite:
    # Kiro Crew never writes it, so its entry keeps its own grant and must be
    # able to block a revoke. Labelling both ``agent:`` made an ordinary
    # Disconnect count its own reflection as a sharer and never revoke anything.
    sources.extend(
        (
            f"{_MIRROR_PREFIX}{name}" if name == _RENDERED_AGENT_FILE else f"agent:{name}",
            agents_dir / name,
        )
        for name in names
    )
    for scope in _extra_mcp_scopes():
        if scope.agent_mcp_file is not None:
            sources.append((f"{_MIRROR_PREFIX}{scope.id}", scope.agent_mcp_file))
    # Deduped against the user-level dir and against each other: a project whose
    # checkout IS the kiro home would otherwise be read twice, and a repeated read
    # under one label merges two buckets, where first-definition-wins can drop the
    # entry that would have kept a grant alive.
    enumerated = {agents_dir}
    for project_dir in project_dirs:
        spec_dir = project_agents_dir(project_dir)
        if spec_dir in enumerated:
            continue
        enumerated.add(spec_dir)
        project_names = _json_spec_names(spec_dir)
        if project_names is None:
            sources.append((f"agent:{spec_dir}/", spec_dir))
            continue
        sources.extend((f"agent:{spec_dir / name}", spec_dir / name) for name in project_names)
    return sources


def spec_census(
    project_dirs: tuple[Path, ...] = (),
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    """Read every source that can define an MCP server: ``(specs, unreadable)``.

    ``specs`` is ``{source label: {name: spec}}`` -- the mcp.json scopes keyed by
    :func:`mcp_discovery._load_mcp_json_by_source`'s own scope names (so a caller
    can hand a subset straight back to the purge), plus one entry per agent spec
    file. ``unreadable`` names the sources that could not be read.

    ``project_dirs`` are the open projects whose ``<project>/.kiro/agents`` specs
    kiro-cli resolves ahead of the user-level directory; the default of no
    projects is honest absence, not a shortcut, because a caller with no dashboard
    state has no open checkout to read.

    Deliberately NOT ``_load_mcp_json_by_source``: that function's documented
    behaviour on an unreadable source is to warn and SKIP it, which is right for
    a view and wrong for a destructive decision -- a missing source reads as "no
    entry there", and the one entry that would have kept a grant alive is exactly
    what a skip hides. The path seam is still shared (``_mcp_sources`` plus the
    edition's ``_extra_scope_sources``), so the files scanned cannot drift from
    the ones apply and uninstall manage; only the failure handling differs.
    """
    from kiro_crew.hooks import safe_read_file
    from kiro_crew.mcp_discovery import SCOPE_KIROCREW, _extra_scope_sources, _mcp_sources

    specs: dict[str, dict[str, Any]] = {}
    unreadable: list[str] = []
    scope_sources = [(scope, path) for path, scope in _mcp_sources()]
    scope_sources += [(scope, path) for path, scope in _extra_scope_sources()]
    for label, path in scope_sources + agent_spec_sources(project_dirs):
        bucket = specs.setdefault(label, {})
        try:
            if label.endswith("/"):
                # An unenumerable-DIRECTORY sentinel from agent_spec_sources,
                # screened by its label rather than by a stat. A stat test is what
                # round 7 already had to fix once: a plain file sitting where the
                # agents directory belongs is ``is_file()``, so the sentinel would
                # be parsed as a document and a source whose entries are unknown
                # would read as a source that declares none.
                unreadable.append(label)
                continue
            if path.is_dir():
                # A directory sitting where a spec file should be: this source's
                # entries are unknown, and skipping it as "not a file" is exactly
                # the reading the sentinel above exists to prevent.
                unreadable.append(label)
                continue
            if not path.is_file():
                continue  # genuinely absent: no entries here, nothing hidden
            data = json.loads(safe_read_file(str(path)))
        except (OSError, ValueError):
            # PermissionError (an OSError) is what safe_read_file raises for a
            # sensitive path or a symlink race; a stalled mount and malformed
            # JSON land here too -- ``json.JSONDecodeError`` IS a ``ValueError``,
            # so naming it as well would catch nothing extra. Every one of them
            # means this source's entries are unknown, not absent.
            unreadable.append(label)
            continue
        if not isinstance(data, dict):
            # Valid JSON with the wrong shape ("[]", "null") can hide a sharer
            # just as well as unparseable bytes: entries unknown, not absent.
            unreadable.append(label)
            continue
        servers = data.get("mcpServers")
        if "mcpServers" not in data:
            continue  # a document that declares no entries
        if not isinstance(servers, dict):
            # An explicit null or non-object map is corruption, not a
            # declaration: entries unknown.
            unreadable.append(label)
            continue
        # First definition wins within a label, matching the merge semantics
        # of the shared loader for two paths mapped to one scope.
        for name, spec in servers.items():
            if isinstance(name, str):
                bucket.setdefault(name, spec)
    specs.setdefault(SCOPE_KIROCREW, {})
    return specs, tuple(sorted(set(unreadable)))


async def remove_provider_entry(
    slug: str, mcp_url: str, project_dirs: tuple[Path, ...]
) -> DisconnectScope:
    """Decide what this Disconnect owns and act on it, all under ONE lock.

    Three destructive acts share one judgement, so they share one read and one
    lock hold: which scopes' ``slug`` entry is actually this provider's, whether
    any OTHER entry shares the provider's grant ARTIFACTS, and -- only if nothing
    does -- unlinking the artifacts. Splitting them is what produced three
    separate data-loss paths:

    * a census that missed custom agent configs revoked a grant an agent outside
      Kiro Crew was authorized by (see :func:`agent_spec_sources`);
    * a purge that removed the name from EVERY scope while ownership was judged
      from the merged winner deleted a same-named entry in another scope that
      pointed somewhere else -- so the purge now names the scopes it matched
      (``_purge_server_config(scopes=...)``) and no other scope is touched;
    * a revoke that ran after the lock released deleted the grant of an entry
      added in between. Holding the MCP lock across the unlinks is the accepted
      cost of closing that: a config writer waits as long as two ``unlink`` calls
      against the user's home take, which is the same exposure every other writer
      under this lock already has.

    The two questions use DIFFERENT identity functions, because they protect
    different things. Entry identity is ``normalized_endpoint`` on name AND url --
    the pair the card matches on and the rule :mod:`kiro_crew.connections.l1_smoke`
    keeps -- so a user's same-named server at another endpoint is never purged.
    Grant identity is ``grant_key`` equality, because the artifacts being protected
    are FILES NAMED BY ``grant_key``, and the two functions disagree in both
    directions: ``normalized_endpoint`` keeps the query string and strips a
    trailing slash, ``grant_key`` drops the query and keeps the path verbatim. A
    ``?workspace=`` variant shares our artifact pair but is a different endpoint;
    a trailing-slash variant is the same endpoint but a different artifact pair.
    Testing the credential with the endpoint comparator would delete a shared
    grant in the first case and strand a live one (reported as a deliberate keep)
    in the second.

    The sweep reads the RAW specs, not the probe view: ``list_servers`` drops
    disabled entries outside the Kiro Crew scope, and a user's switched-off server
    still owns its grant -- deleting it because its entry is disabled would force a
    fresh consent the moment they re-enable it. The probe view is unioned in so a
    row this census cannot parse still counts.

    FAIL CLOSED, asymmetrically, because the two acts need opposite evidence. The
    revoke needs the ABSENCE of a sharer, which an unreadable source can hide, so
    one unreadable source keeps the grant. The purge acts only on POSITIVE
    evidence -- a scope whose entry it read and matched -- so an unreadable scope
    simply is not purged, and refusing the whole request would leave the user
    unable to disconnect at all because of a file that has nothing to do with this
    provider.

    Reuses the apply path's own config-side uninstall so a Disconnect and an
    ``uninstall`` apply remove byte-identical config rather than drifting into two
    definitions of "removed". A failed agent-config rebuild is logged, not raised:
    the config write has already landed by then, so failing the request would report
    a Disconnect that did not happen.

    ``project_dirs`` is REQUIRED rather than defaulted: the census reads it to
    reach ``<project>/.kiro/agents``, and a default of "no projects" on the one
    destructive caller would let a forgotten argument narrow the census back to
    the gap this parameter exists to close, silently and only for users who have
    a project-local spec.
    """
    from kiro_crew.connections.tool_aliases import normalized_endpoint
    from kiro_crew.dashboard.handlers.mcp import (
        _get_mcp_lock,
        _offload_config_write,
        _purge_server_config,
    )
    from kiro_crew.mcp_discovery import list_servers
    from kiro_crew.mcp_grant import grant_key, revoke_local_grant

    wanted = normalized_endpoint(mcp_url)
    wanted_key = grant_key(mcp_url)

    # Never a sha256 hexdigest and never None, so the comparisons below cannot
    # confuse it with either real answer.
    _UNPROVABLE = "\x00unprovable-url"
    _PROVABLE_HOST = re.compile(r"[a-z0-9._-]+")
    _PROVABLE_PATH = re.compile(r"[\x21-\x7e]*")
    # A backslash anywhere, or "." / ".." as a whole path segment. Both are shapes
    # WHATWG rewrites before hashing (backslash folds to "/", dot-segments are
    # removed) while urlsplit keeps them verbatim. One regex rather than a
    # `/`-split because splitting a URL path on a literal "/" also reads as
    # filesystem path assembly to the cross-platform scan, and this is a URL,
    # where "/" is the only separator RFC 3986 defines.
    _URL_DOT_SEGMENT_RE = re.compile(r"\\|(?:\A|/)\.\.?(?:/|\Z)")

    def _artifact_key(url: object) -> str | None:
        """``grant_key`` of a validated URL, ``None`` when the pair provably is
        not ours, or ``_UNPROVABLE`` when equality can be neither proven nor
        refuted.

        ONE pipeline: the string that is screened is BYTE-IDENTICAL to the string
        that is hashed. Round 3 guarded three malformed shapes and round 4 found a
        fourth (a trailing space after an explicit port -- ``urlsplit`` lstrips
        only) precisely because ``normalized_endpoint`` parsed ``value.strip()``
        while ``grant_key`` parsed the raw value, so the screen's guarantee never
        transferred.

        THREE-valued, because two implementations compute this key. kiro-cli
        derives the artifact pair with the WHATWG url parser, which
        percent-decodes hostnames, IDNA-maps Unicode hosts, normalizes
        dot-segments and backslashes, and percent-encodes non-ASCII paths --
        transformations ``urlsplit`` does not perform. Hashing such a URL here
        answers a question about different bytes than the ones kiro-cli hashed:
        round 7 measured ``%6dcp.notion.com`` and ``/a/../mcp`` both naming the
        registry pair over there while missing it here, with no exception
        anywhere. So key equality is asserted only inside the PROVABLE set --
        lowercase-ASCII LDH hosts and printable-ASCII paths free of ``%``,
        backslashes and dot-segments, with no scheme-default port spelled out --
        where both parsers' serializations are byte-identical by construction.
        Outside that set the answer is ``_UNPROVABLE`` and the caller fails
        closed: the grant is kept and the census reported incomplete, because
        deleting on an unprovable comparison is how a live consent dies.

        Within the provable set, ``None`` still means SKIP, on two premises that
        stay sound: unparseable junk (the round-3/4 shapes) can hold no pair
        under either parser, and a provable-charset host that Python's IDNA
        refuses (empty or >63-char label) is serialized verbatim by kiro-cli and
        differs from the registry host, so its key provably is not ours.
        """
        if not isinstance(url, str):
            return None
        candidate = url.strip()
        if normalized_endpoint(candidate) is None:
            return None
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        path = parts.path or "/"
        provable = (
            _PROVABLE_HOST.fullmatch(host) is not None
            and _PROVABLE_PATH.fullmatch(path) is not None
            and "%" not in path
            and _URL_DOT_SEGMENT_RE.search(path) is None
            and not (parts.scheme.lower() == "http" and parts.port == 80)
        )
        if not provable:
            return _UNPROVABLE
        try:
            return grant_key(candidate)
        except ValueError:
            # UnicodeError (IDNA) is a ValueError; nothing else in grant_key
            # raises it for a screened provable-charset string.
            return None

    def _judge(
        configured: list, specs: dict
    ) -> tuple[tuple[str, ...], dict[str, set[str]], tuple[str, ...], dict[str, str]]:
        """``(owned scopes, sharers PER KEY, unprovable names, owned url per key)``.

        All answers come out of the same walk over the same census, so they
        cannot be taken from two different readings of the store. Only mcp.json
        scope labels can be purged, so an agent spec contributes sharers but never
        a purge target -- Kiro Crew does not own a user's agent file.

        TWO passes, because ownership is endpoint-keyed (slash-insensitive) while
        grants are artifact-keyed (slash-sensitive): an owned entry at ``<url>/``
        holds a pair under a DIFFERENT key than the registry URL's, and revoking
        only the registry key would purge the entry while its real pair survives
        -- "Disconnected locally" over a live credential. So pass one collects
        every owned entry's provable key into ``owned_urls`` (registry key
        included), and pass two sharer-tests every non-owned entry against the
        COMPLETE owned-key set. An owned or non-owned URL outside the provable
        set lands in ``unprovable`` and the caller fails closed.

        Sharers are returned PER KEY rather than as one flag: a sharer of the
        registry pair says nothing about an owned trailing-slash pair nobody else
        uses, and collapsing them let one sharer suppress every revoke while the
        purge still removed the entries. Mirrors of the scopes being purged are
        excluded from the sharer test when the purge will run (see
        :func:`agent_spec_sources`), because the same transaction removes them.
        """
        owned: list[str] = []
        owned_urls: dict[str, str] = {wanted_key: mcp_url}
        unprovable: set[str] = set()
        others: list[tuple[str, str, object]] = []
        mirrored_slug: list[tuple[str, str, object]] = []
        # Every (name, url) the RAW census carries, with provenance. The probe view
        # is a MERGED read whose sources include the rendered agent config, so a
        # mirrored entry reappears there as a plain row with its provenance lost --
        # and judged again it can vote as an independent sharer against the very
        # pair the purge is removing. The census is authoritative; a row it already
        # represents is already judged.
        census_entries: set[tuple[str, str]] = set()

        def _collect_owned(url: object, holder: str) -> None:
            key = _artifact_key(url)
            if key == _UNPROVABLE:
                unprovable.add(holder)
            elif key is not None and isinstance(url, str):
                owned_urls.setdefault(key, url.strip())

        for label, entries in specs.items():
            for name, spec in entries.items():
                if not isinstance(spec, dict):
                    continue
                url = spec.get("url")
                if isinstance(url, str):
                    census_entries.add((name, url.strip()))
                # Ownership FIRST, then the sharer test for everything that is
                # not ours -- including entries that carry OUR name. A `notion`
                # entry at a query variant holds the same artifact pair
                # (grant_key drops the query) while failing the endpoint test,
                # and a same-named agent-spec entry is never a purge target but
                # holds a grant like any other; an if/elif keyed on the name let
                # both fall through the census entirely.
                #
                # Only mcp.json SCOPE labels can be purge targets: an agent spec
                # is the user's file, so a same-named entry there is a SHARER and
                # falls through to `others`.
                if name == slug and is_scope_label(label):
                    candidate = url.strip() if isinstance(url, str) else url
                    if normalized_endpoint(candidate) == wanted:
                        owned.append(label)
                        _collect_owned(url, f"{label}/{name}")
                        continue
                if name == slug and label.startswith(_MIRROR_PREFIX):
                    # Deferred: whether this is a holder depends on whether the
                    # purge runs at all, which is only known after this walk.
                    mirrored_slug.append((label, name, url))
                    continue
                others.append((label, name, url))
        for server in configured:
            if server.name == slug:
                candidate = server.url.strip() if isinstance(server.url, str) else server.url
                if normalized_endpoint(candidate) == wanted:
                    _collect_owned(server.url, server.name)
                    continue  # ours; owned scopes come from the raw specs walk
            if isinstance(server.url, str) and (server.name, server.url.strip()) in census_entries:
                continue  # already judged WITH provenance in the walk above
            others.append(("probe", server.name, server.url))

        # The purge strips the entry named ``slug`` from every rendered mirror
        # (``_remove_from_agent_file(<mirror>, slug)``) -- and ONLY that name. So
        # the exclusion is per ENTRY, never per file: a mirrored ``slug`` entry is
        # not a holder once the purge runs, while a mirrored entry under any OTHER
        # name survives this transaction and must take the ordinary sharer test
        # (which is why no blanket mirror skip exists below). A mirrored ``slug``
        # entry still has to contribute its artifact key first: ownership is
        # slash-insensitive while the pair is not, so a mirrored variant spelling
        # owns a pair that would otherwise be purged and never revoked.
        purge_will_run = bool(owned)
        for label, name, url in mirrored_slug:
            if not purge_will_run:
                others.append((label, name, url))  # nothing purges it; a real holder
                continue
            candidate = url.strip() if isinstance(url, str) else url
            if normalized_endpoint(candidate) == wanted:
                _collect_owned(url, f"{label}/{name}")
            # A mirrored slug entry pointing somewhere else is removed by the purge
            # and names no pair of ours: neither owned nor a sharer.

        sharers_by_key: dict[str, set[str]] = {}
        for label, name, url in others:
            key = _artifact_key(url)
            if key == _UNPROVABLE:
                unprovable.add(name)
            elif key is not None and key in owned_urls:
                # PER KEY, not one flat flag: a sharer of the registry pair says
                # nothing about an owned trailing-slash pair nobody else uses,
                # and one flag skipped that pair's revoke while purging its entry.
                sharers_by_key.setdefault(key, set()).add(name)
        return (
            tuple(sorted(set(owned))),
            sharers_by_key,
            tuple(sorted(unprovable)),
            owned_urls,
        )

    async with _get_mcp_lock():
        configured = await asyncio.to_thread(list_servers)
        specs, unreadable = await asyncio.to_thread(spec_census, project_dirs)
        owned_scopes, sharers_by_key, unprovable, owned_urls = _judge(configured, specs)
        census_gap = bool(unreadable or unprovable)
        shared = tuple(sorted({name for names in sharers_by_key.values() for name in names}))
        if owned_scopes:
            # Shielded, not a bare to_thread: a cancelled request task would release
            # the MCP lock while the worker is still rewriting the store, letting a
            # concurrent purge interleave with this stale snapshot. mcp.py ships this
            # helper for exactly that, and its docstring names the hazard.
            await _offload_config_write(_purge_server_config, slug, scopes=owned_scopes)
        else:
            logger.info(
                "Disconnect left the %r entry alone: no scope configures it at this endpoint",
                slug,
            )
        removed: list[str] = []
        attempted: list[str] = []
        if census_gap:
            # The gap is about the census as a whole -- an unreadable source or an
            # uncomparable URL could hide a sharer of ANY owned pair -- so every
            # pair is kept, not just the one a named sharer covers.
            logger.warning(
                "Disconnect kept %r's stored grant: no one can say it is ours alone "
                "(unreadable sources: %s; entries whose URL could not be safely "
                "compared: %s)",
                slug,
                ", ".join(unreadable) or "none",
                ", ".join(unprovable) or "none",
            )
        else:
            # Per owned KEY: ownership is slash-insensitive while the artifact pair
            # is not, so an owned trailing-slash variant holds its own pair, and a
            # sharer of one pair must not suppress another pair's revoke.
            for key, owned_url in owned_urls.items():
                key_sharers = sharers_by_key.get(key)
                if key_sharers:
                    # Endpoint-keyed grant, more than one entry using it. Deleting
                    # it would deauthorize a server this Disconnect was never asked
                    # to touch, and the refresh token is not recoverable locally.
                    logger.info(
                        "Disconnect kept %r's stored grant at %s: %s still use it",
                        slug,
                        owned_url,
                        ", ".join(sorted(key_sharers)),
                    )
                    continue
                # Shielded for the same reason the purge is, and for one more: a
                # cancellation that released the lock mid-unlink would reopen the
                # very window this transaction exists to close.
                attempted.append(owned_url)
                for label in await _offload_config_write(revoke_local_grant, owned_url):
                    if label not in removed:
                        removed.append(label)

        # INSIDE the lock, and shielded. A post-lock rebuild snapshots the config
        # before another Disconnect's purge and can write last, resurrecting an
        # entry whose grant that other transaction just deleted -- a configured
        # provider with a dead credential, reachable with two dashboard tabs.
        # Safe to nest: this takes the ``~/.kiro/settings/mcp.lock`` sidecar while
        # rebuild_agent_config's internal lock defaults to
        # ``~/.kiro/agents/kirocrew.lock`` (apps/bridges._mcp_lock), so they are
        # different flocks and reentrancy never arises.
        try:
            from kiro_crew.agent import rebuild_agent_config

            await _offload_config_write(rebuild_agent_config)
        except Exception:  # noqa: BLE001 — the config write already landed
            logger.warning("agent config rebuild failed after disconnect", exc_info=True)

    return DisconnectScope(
        entry_removed=bool(owned_scopes),
        grant_shared_with=shared,
        grant_removed=tuple(removed),
        census_incomplete=census_gap,
        census_unreadable=tuple(_census_source_name(label) for label in unreadable),
        attempted_urls=tuple(attempted),
    )
