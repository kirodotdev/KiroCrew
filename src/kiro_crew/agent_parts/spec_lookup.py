"""Finding an agent's spec file on disk and reading it safely.

The capped read, the path-shape checks, ``agent_spec_path`` and the Markdown spec
beside it, a conflicting spec for one name, and the checkout's own spec that shadows
an installed one. New work on locating or reading a spec goes here.

Composed onto ``kiro_crew.agent``, the import path and patch target of every name
here; see :mod:`kiro_crew.agent_parts`.
"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from kiro_crew.agent import (
        AmbiguousAgentSpecError,
        SpecDirectoryOverflowError,
        _declared_project_agent_name,
        _read_agent_spec,
        agent_spec_candidates,
        hooks,
        is_markdown_spec,
        is_registered_agent_name,
        is_sensitive_canonical_path,
        is_sensitive_path,
        iter_agent_spec_files,
        kiro_agents_dir_path,
        live_agent_spec_names,
        logger,
        parse_agent_spec_bytes,
        platform_compat,
        project_agent_files,
        project_agent_name,
    )


def _read_spec_capped(path: Path) -> dict | None:
    """Parse an agent spec through the hardened, SIZE-CAPPED read gate.

    ``agent_discovery._read_agent_spec`` is what that module documents as the one
    reader for both agent scopes: it reads via ``hooks.safe_read_file_bytes``, so
    a multi-gigabyte "agent config" in a user-writable, tool-shared directory is
    refused at the cap instead of being slurped into memory, and it also rejects
    non-UTF-8 bytes, AppleDouble sidecars and JSON that is not an object.

    A thin wrapper rather than a direct call at each site, so the reason the
    capped reader is used lives in one place.
    """
    return _read_agent_spec(path, operation="agent_spec_lookup", source="unknown")


def _spec_path_is_safe(path: Path, agents_dir: Path) -> bool:
    """True when *path* is a real file inside *agents_dir*, safe to read and rewrite.

    A spec is read and then written back, so a SYMLINK is refused rather than
    followed. Following one would read the target and write a modified copy into
    the agents directory, which launders the contents of a file the reader may
    not otherwise be allowed to open -- a governance-fenced path, for instance --
    into a location that is freely readable. (The rewrite itself does not corrupt
    the target: ``_atomic_json_write`` goes through ``os.replace``, which swaps
    the link rather than writing through it. The copy-out is the problem.)

    Also refuses a resolved path that leaves the agents directory, and any
    sensitive path, which is the same fence ``kiro_crew.agent`` already applies before
    touching a resolved path elsewhere.

    A metadata probe that itself fails (``OSError``) is refused too: for a
    caller about to rewrite in place, not knowing the shape is the same as a
    bad one. A caller that must tell the two apart -- the conductor installer,
    which leaves a spec alone rather than lose its entries over a probe that may
    succeed next time -- asks :func:`_spec_path_shape_refusal` directly.
    """
    try:
        return _spec_path_shape_refusal(path, agents_dir) is None
    except OSError:
        return False


def _spec_path_shape_refusal(path: Path, agents_dir: Path) -> str | None:
    """Why *path* is not a plain spec file inside *agents_dir*, or ``None`` when it is.

    The one implementation of :func:`_spec_path_is_safe`'s fence, kept apart so
    its two outcomes stay distinct: a SHAPE that is refused for good (a link at
    the spec's name, a path that resolves outside the agents directory or onto a
    sensitive target) comes back as the reason, while a probe that failed to read
    the metadata at all -- a stale NFS handle, an I/O error on the directory --
    raises the ``OSError`` for the caller to classify. Collapsing both to "unsafe"
    is right for a caller that only needs permission to rewrite; it is wrong for
    one that would REPLACE the file on that answer, because then a transient error
    on the probe would destroy a spec that was intact.
    """
    if path.is_symlink():
        return "a link stands at the spec's name"
    resolved = path.resolve()
    if resolved.parent != agents_dir.resolve():
        return "the path resolves outside the agents directory"
    if is_sensitive_path(str(resolved)):
        return "the path resolves to a sensitive target"
    return None


def agent_spec_path(name: str, *, agents_dir: Path | None = None) -> Path | None:
    """Return the kiro spec file for *name*, or ``None`` if absent.

    ``agents_dir`` selects one explicit scope for callers that resolve the
    provider's cwd before the user registry. Omission keeps the user-level
    behavior; parsing, unreadable-file handling and ambiguity rules are shared.

    Prefers ``<agents dir>/<name>.json`` or ``<name>.md`` and falls back to a
    scan for a spec whose ``name`` field matches, mirroring how the dashboard's
    per-agent handler resolves an agent to a file (a spec's filename and its
    ``name`` are not required to agree). Both on-disk forms are scanned (see
    :mod:`kiro_crew.agent_spec_format`); a markdown result is a READ-ONLY
    resolution, and every writer that receives one refuses rather than
    serializing JSON over a markdown file.

    A malformed or path-shaped *name* returns ``None``, so a traversal such as
    ``../../something`` cannot escape the agents directory. Symlinked and other
    unsafe candidates are also refused; see :func:`_spec_path_is_safe`.

    A DECLARED ``name`` wins over a matching filename, which is the order the
    other two resolvers already use (``_resolve_named_agent_model`` and the
    dashboard's per-agent handler both test ``data["name"] == agent`` before the
    stem). Preferring the filename would let ``<name>.json`` that declares a
    DIFFERENT agent be selected, and since the caller then writes to it, that
    clears the wrong agent's pin while the requested one stays pinned. The
    filename is accepted only when no spec declares this name -- see below.

    Raises :class:`~kiro_crew.agent_discovery.AmbiguousAgentSpecError` (a
    ``ValueError``) when TWO safe specs declare the same name. The runtime
    iterates the directory unordered, so which of them is live is undefined, and
    a writer cannot pick without risking clearing the pin nothing is reading.
    """
    return _resolve_agent_spec(name, agents_dir, json_only=False)


#: At most this many entries of a pinned agents directory are listed by
#: :func:`pinned_agent_spec_path`. The directory is agent-writable, and its
#: caller runs on every turn, so the listing must not grow with whatever an
#: agent writes there; past the cap the scope is reported unknown instead.
PINNED_SPEC_DIR_MAX_ENTRIES = 4096

#: At most this many bytes, summed over every spec it reads, are read by one
#: :func:`pinned_agent_spec_path` resolve. A declared ``name`` beats a matching
#: filename, so the resolve parses every listed spec; without an aggregate cap
#: the entry cap times the per-file cap would let an agent-written directory
#: cost gigabytes of reads per turn. Real agents directories are a few dozen
#: kilobytes; past the cap the scope is reported unknown instead.
PINNED_SPEC_DIR_MAX_TOTAL_BYTES = 8 * 1024 * 1024


#: The errnos a pinned read raises for an entry it refuses to read at all: a
#: link at the name (``ELOOP``; ``EMLINK`` from ``O_NOFOLLOW`` on FreeBSD), a
#: hardlink or non-regular file (``EINVAL``), a directory (``EISDIR``).
_REFUSED_SPEC_ENTRY_ERRNOS = frozenset({errno.ELOOP, errno.EMLINK, errno.EINVAL, errno.EISDIR})


def pinned_agent_spec_path(
    name: str,
    *,
    agents_dir: Path,
    pinned: platform_compat.PinnedDirectory,
    json_only: bool,
) -> Path | None:
    """:func:`agent_spec_path` resolved through *pinned*, a handle the caller
    opened on *agents_dir*; with *json_only*, among ``*.json`` specs alone (the
    form kiro-cli reads).

    For a caller that screened *agents_dir* and must not reopen it by name: the
    listing and every spec read go through the handle (see
    :func:`_resolve_agent_spec`). Same selection, safety and ambiguity rules.
    Raises :class:`SpecDirectoryOverflowError` when the directory holds more
    than :data:`PINNED_SPEC_DIR_MAX_ENTRIES` entries or its specs sum to more
    than :data:`PINNED_SPEC_DIR_MAX_TOTAL_BYTES`, and ``OSError`` when a spec in
    it exists but cannot be read, since that spec may be the one resolved.
    """
    return _resolve_agent_spec(name, agents_dir, json_only=json_only, pinned=pinned)


def _named_spec_documents(agents_dir: Path, *, json_only: bool) -> Iterator[tuple[Path, dict]]:
    """Every safe, parsed spec of *agents_dir*, listed and read by path."""
    for spec_path in iter_agent_spec_files(agents_dir):
        if json_only and is_markdown_spec(spec_path):
            continue
        if not _spec_path_is_safe(spec_path, agents_dir):
            continue
        try:
            data = _read_spec_capped(spec_path)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            yield spec_path, data


def _pinned_spec_documents(
    pinned: platform_compat.PinnedDirectory, agents_dir: Path, *, json_only: bool
) -> Iterator[tuple[Path, dict]]:
    """:func:`_named_spec_documents` through *pinned*, never reopening *agents_dir*.

    *agents_dir* is the canonical path the pin was opened on and is used only to
    name the results. The listing, the twin probe and every read go through the
    handle; a link, a hardlink or any non-regular entry is refused by the read
    itself, so the per-entry guards :func:`_spec_path_is_safe` asks of a path
    are answered by the open. The sensitive-path fence is asked of the
    already-canonical name, which no link can redirect, through
    :func:`~kiro_crew.security.is_sensitive_canonical_path`.

    The listing is bounded by :data:`PINNED_SPEC_DIR_MAX_ENTRIES` and the reads
    by :data:`PINNED_SPEC_DIR_MAX_TOTAL_BYTES` summed over every spec; a
    directory past either raises :class:`SpecDirectoryOverflowError` rather than
    being materialized or read whole.
    """

    def _present(entry: str) -> bool:
        try:
            pinned.lstat(entry)
        except OSError:
            return False
        return True

    listed = pinned.names_bounded(PINNED_SPEC_DIR_MAX_ENTRIES)
    if listed is None:
        raise SpecDirectoryOverflowError(
            f"more than {PINNED_SPEC_DIR_MAX_ENTRIES} entries in {str(agents_dir)!r}"
        )
    remaining = PINNED_SPEC_DIR_MAX_TOTAL_BYTES
    for entry in live_agent_spec_names(listed, entry_exists=_present):
        if json_only and is_markdown_spec(entry):
            continue
        # The hardened reader skips AppleDouble sidecars by name.
        if entry.startswith("._"):
            continue
        spec_path = agents_dir / entry
        if is_sensitive_canonical_path(str(spec_path)):
            continue
        # The read is capped at what the aggregate budget has left, and refused
        # from the descriptor's own size (EFBIG), so no spec is read past it.
        try:
            raw = pinned.read_bytes(entry, max_bytes=min(hooks.MAX_FILE_BYTES, remaining))
        except OSError as exc:
            if exc.errno == errno.EFBIG:
                raise SpecDirectoryOverflowError(
                    f"specs in {str(agents_dir)!r} exceed the "
                    f"{PINNED_SPEC_DIR_MAX_TOTAL_BYTES}-byte read budget"
                ) from exc
            # Removed between the listing and the read: no spec here.
            if exc.errno == errno.ENOENT:
                continue
            # A link, a hardlink or a non-regular entry is refused by the open
            # or the descriptor check, and is skipped exactly as
            # _spec_path_is_safe skips it on the by-name path: the resolver
            # never selects such an entry, so its presence says nothing about
            # which spec resolves.
            if exc.errno in _REFUSED_SPEC_ENTRY_ERRNOS:
                continue
            # A spec that exists but cannot be read (a revoked permission) may
            # be the one that declares the name, so which file resolves is
            # unknown. Raised rather than skipped, which would read as that spec
            # being absent.
            raise
        remaining -= len(raw)
        # A document nested past the parser's depth raises RecursionError; it
        # is a syntax error like any other and is skipped the same way.
        try:
            data = parse_agent_spec_bytes(raw, spec_path)
        except (ValueError, RecursionError):
            continue
        if isinstance(data, dict):
            yield spec_path, data


def _resolve_agent_spec(
    name: str,
    agents_dir: Path | None,
    *,
    json_only: bool,
    pinned: platform_compat.PinnedDirectory | None = None,
) -> Path | None:
    """The resolver behind :func:`agent_spec_path` and :func:`pinned_agent_spec_path`.

    With *pinned* -- a :class:`~kiro_crew.platform_compat.PinnedDirectory` the
    caller opened on *agents_dir* -- the listing and every spec read go through
    that handle and the directory is never reopened by its name, so a name
    swapped for a link (a Windows junction to a UNC share) after the caller's
    screen is not followed. The selection rules are the same either way.
    """
    if not is_registered_agent_name(name):
        return None
    if pinned is not None:
        if agents_dir is None:
            raise TypeError("a pinned resolve needs the path the pin was opened on")
        specs = _pinned_spec_documents(pinned, agents_dir, json_only=json_only)
    else:
        if agents_dir is None:
            from kiro_crew.config.paths import spec_dir_for

            agents_dir = spec_dir_for(name, kiro_agents_dir_path())
        if not agents_dir.is_dir():
            return None
        specs = _named_spec_documents(agents_dir, json_only=json_only)

    direct = set(agent_spec_candidates(agents_dir, name))
    declared_matches: list[Path] = []
    fallbacks: list[Path] = []
    for spec_path, data in specs:
        declared = data.get("name")
        if declared == name:
            declared_matches.append(spec_path)
        elif spec_path in direct:
            # Right filename. Accepted as the fallback even when it declares a
            # DIFFERENT name, because the runtime resolver matches on
            # `data["name"] == agent OR path.stem == agent` -- so with nothing
            # declaring this name, the stem match makes THIS file the live spec,
            # and refusing it would leave a live pin unresettable, which is the
            # bug this change exists to fix. Only used when no declared match is
            # found, and a declared match alongside it is the ambiguity the
            # caller refuses rather than resolves.
            fallbacks.append(spec_path)
    if len(declared_matches) > 1:
        # Paths are repr'd: a filename in this user-writable, tool-shared
        # directory is untrusted input, and this message is printed to a terminal.
        # The typed subclass lets a caller that can answer its own question
        # despite the ambiguity (the spawn gate, see require_fork_governance)
        # tell it apart from every other ValueError; ``except ValueError``
        # callers are unaffected.
        raise AmbiguousAgentSpecError(
            f"{len(declared_matches)} specs declare the name {name!r}: "
            f"{', '.join(repr(str(p)) for p in declared_matches)}. The runtime iterates the "
            f"directory unordered, so which one is live is undefined -- remove or rename "
            f"one before resetting."
        )
    if declared_matches:
        return declared_matches[0]
    # At most one: the scan already drops a ``<name>.md`` shadowed by its
    # ``<name>.json`` twin (JSON wins), so both never reach this list.
    return fallbacks[0] if fallbacks else None


def markdown_spec_for_agent(agent: str, work_dir: str | Path | None = None) -> Path | None:
    """The markdown file *agent* is defined in when NO JSON spec claims it, else ``None``.

    Only the KAS backend reads the markdown form; kiro-cli discovers ``*.json``
    alone, so a markdown-only agent selected there is not the active mode after
    ``session/new`` and the runtime's activation guard refuses the session. That
    guard asks this on its refusal branch, to name the file in its message
    instead of prescribing a JSON repair. The question is therefore whether
    kiro-cli, which does not see markdown at all, finds a JSON spec for the
    name anywhere in its own resolution order -- the project checkout's
    ``.kiro/agents`` first, then the user directory. A project ``foo.md``
    beside a user-level ``foo.json`` is NOT markdown-only: kiro-cli skips the
    markdown file and runs the JSON one, so this returns ``None``. A markdown
    file is returned only when both scopes hold no JSON spec for the name; the
    project file is named when both scopes have one.

    Never raises: an ambiguous or unreadable resolution is ``None`` here, and
    the resolver that owns that refusal reports it on its own path.
    """
    project_markdown: Path | None = None
    try:
        if work_dir:
            for spec in project_agent_files(
                work_dir, operation="markdown_spec_lookup", source="unknown"
            ):
                if project_agent_name(spec) != agent:
                    continue
                if not is_markdown_spec(spec):
                    return None
                if project_markdown is None:
                    project_markdown = spec
        path = agent_spec_path(agent)
    except (OSError, ValueError):
        return None
    if path is not None and not is_markdown_spec(path):
        return None
    if project_markdown is not None:
        return project_markdown
    return path


def _conflicting_spec_for(name: str, chosen: Path, agents_dir: Path) -> Path | None:
    """Return a DIFFERENT safe spec whose FILENAME also claims *name*.

    The runtime resolver (``KiroCrewConfig._resolve_named_agent_model``) accepts
    EITHER a declared-name match or a filename match -- ``data["name"] == agent
    or path.stem == agent`` -- and iterates ``glob("*.json")``, which is
    unordered. So when ``<name>.json`` declares a different agent AND another
    file declares *name*, which of the two the runtime actually uses is
    UNDEFINED: it is whichever the filesystem yields first.

    A reset cannot pick correctly in that state. Clearing either one can leave
    the live pin in place and strip the model from a spec nothing is reading, so
    the caller refuses instead of guessing.
    """
    # The JSON filename only: a ``<name>.md`` beside a chosen ``<name>.json`` is
    # shadowed (JSON wins), not a competing claimant; a chosen ``<name>.md`` has
    # no JSON twin by construction, since the twin would have been chosen.
    direct = agents_dir / f"{name}.json"
    if direct == chosen or not direct.is_file():
        return None
    if not _spec_path_is_safe(direct, agents_dir):
        return None
    return direct


def _project_shadow_of(
    agent: str,
    work_dir: str | Path | None,
    *,
    markdown_specs: bool = True,
    dispatchable_only: bool = False,
) -> Path | None:
    """A checkout's own spec for *agent*, or ``None``.

    ``<work_dir>/.kiro/agents/`` is the ONLY project location kiro-cli resolves
    ``--agent`` against (see ``docs/reference/kiro-cli/custom-agents``, and
    :func:`agent_discovery.project_agent_files`, which states the same rule for every
    other consumer). There is no parent walk to match: a spec one directory up is not
    dispatchable, so it is not a shadow. The declared ``name`` beats the filename, which
    is why the comparison goes through :func:`agent_discovery.project_agent_name` rather
    than the stem -- a file called anything at all can declare ``kirocrew-worker``.

    Two keywords narrow WHICH project files count, and the default of each is the
    behaviour this function had before they existed. Both matter to a caller asking
    "would the host dispatch this", and neither matters to one asking "does the
    checkout make any claim on this name" -- a governance refusal, say, for which a
    file in any form and any state is a claim.

    ``markdown_specs=False`` narrows to the JSON form, for a host that dispatches
    that alone. The narrowing happens INSIDE the scan, not to its result:
    ``project_agent_files`` sorts by stem and this returns the first declared-name
    match, so a differing-stem pair (``a.md`` and ``z.json``, both declaring ``foo``)
    yields the markdown file first. Filtering afterwards would answer "no shadow"
    while a dispatchable ``z.json`` sat right there. The same-stem pair needs no such
    care: ``iter_agent_spec_files`` already drops ``<stem>.md`` when ``<stem>.json``
    exists.

    ``dispatchable_only=True`` additionally skips a spec that does not PARSE.
    :func:`agent_discovery.project_agent_name` falls back to the filename stem for a
    malformed file, so a broken ``foo.json`` otherwise matches ``foo`` and shadows a
    perfectly good user-level agent of that name -- while kiro-cli, which reports it
    as an error and offers no such mode, runs the user-level one. The distinction is
    :func:`agent_discovery._declared_project_agent_name`, whose ``None`` means exactly
    "does not parse" and which already applies the filename fallback for a spec that
    parses without a ``name`` field.

    Never raises: an unreadable checkout answers "no shadow", and the caller's own
    fail-closed rule covers what it cannot see.
    """
    if not work_dir:
        return None
    try:
        for spec in project_agent_files(
            work_dir, operation="agent_project_shadow", source="unknown"
        ):
            if not markdown_specs and is_markdown_spec(spec):
                continue
            if dispatchable_only:
                if _declared_project_agent_name(spec) == agent:
                    return spec
                continue
            if project_agent_name(spec) == agent:
                return spec
    except OSError:
        logger.debug("could not scan %s for project agents", work_dir, exc_info=True)
    return None
