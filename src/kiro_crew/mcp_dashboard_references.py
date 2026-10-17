"""Resolving what a ``kirocrew-dashboard`` tool call names into the ids its endpoints take.

A folder (an id, or a human path walked segment by segment and created where the tool may
create), a live session (its slot key, a ``dashboard:`` key or a unique title), a tag and a
board column (an id or an exact name) each resolve to one id or to a refusal that names the
candidates, never to a guess. ``_get_rows`` reads the list endpoints these references
resolve against. :mod:`kiro_crew.mcp_dashboard` binds every name defined here, so each one
reads, imports and patches as ``kiro_crew.mcp_dashboard.<name>``.
"""

from __future__ import annotations

import re as _re
from collections.abc import Callable

from kiro_crew.mcp_dashboard_folder_tree import _chat_folder_children, _chat_folder_paths

# The authenticated loopback client to the gateway is ``mcp_core``'s request
# helpers, reached through the ``DashboardClient`` port. Importing them costs
# 341ms/40MB in this process (measured) — under ``mcp_computer``'s own import
# cost, because mcp_core's heavy dependencies are function-local.
from kiro_crew.mcp_tools.dashboard_client import DashboardClient, DashboardError
from kiro_crew.platform import redact_via_context as redact

# The folder endpoints store ``name[:100]``. Mirroring the number here is what
# lets this server refuse an overlong name instead of writing one it cannot
# address afterwards; a mismatch shows up as the duplicate-creation the
# too-long-segment test pins.
_MAX_FOLDER_NAME = 100


def _get_rows(client: DashboardClient, path: str) -> tuple[list[dict], str | None]:
    """GET a gateway endpoint whose success body is a JSON **array**.

    The routes this server reads as lists (``/api/chat/folders``,
    ``/api/chat/slots``) answer a refusal as an object, so the two shapes are
    split apart here. Returns ``(rows, None)`` on success and ``([], error)``
    otherwise. A body that is neither an array nor a refusal is reported as an
    error rather than read as empty: "the tree is empty" and "the endpoint is
    broken" must not render identically.
    """
    try:
        payload: object = client.get(path)
    except DashboardError as refused:
        return [], str(refused.error)
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)], None
    return [], f"unexpected response shape from {path}"


# Sidebar folder ids are minted as ``uuid.uuid4().hex[:12]``
# (``chat_folders.api_chat_folder_create``), so an id-shaped reference is
# recognizable and must never be auto-created as a folder NAME.
_CHAT_FOLDER_ID_RE = _re.compile(r"[0-9a-f]{12}")


def _ambiguous_segment_error(seg: str, matches: list[dict]) -> str:
    """Refusal naming the duplicate folders, so the caller can pick one by id."""
    ids = ", ".join(str(m.get("id") or "?") for m in matches)
    return (
        f"{len(matches)} folders named {redact(seg)} share the same parent "
        f"({ids}) — pass the folder id instead of a path"
    )


def _resolve_chat_folder_ref(
    ref: str,
    folders: list[dict],
    *,
    create_missing: bool,
    client: DashboardClient | None = None,
    session_key: str | None = None,
    before_create: Callable[[str], str | None] | None = None,
) -> tuple[str, list[str], str | None]:
    """Resolve a sidebar-folder reference to a folder id. THE resolution chokepoint.

    Every tool addresses a folder through here, so create, move and move-session
    can never disagree about what a reference means. Returns
    ``(folder_id, created_names, error)``; ``""`` = top level (empty ref or
    ``"root"``). ``created_names`` is returned even alongside an error so a
    partial mkdir -p is reported rather than silently left behind.

    Three things a chat-folder reference can mean, in order:

    * **An id.** Unambiguous. An id-shaped ref that does not exist is a lookup
      failure even when ``create_missing`` — ids are minted server-side, so
      creating a folder literally named after the hex id is never what a caller
      meant.
    * **A full human path.** A folder NAME may itself contain ``/`` (the
      sidebar permits it), so ``A/B`` can be one folder named ``A/B`` as well as
      ``B`` inside ``A``, and the tree renders both identically. Both readings
      are computed; when they disagree, or when either is itself duplicated, the
      reference is refused rather than resolved to whichever came first.
    * **A path to walk**, segment by segment, creating what is missing when
      ``create_missing``.

    Pure apart from the creation leg: the caller already holds the folder list,
    and re-fetching per reference would let the tree shift between the two
    lookups of a single move.
    """
    ref = str(ref or "").strip()
    if not ref or ref.lower() == "root":
        return "", [], None
    if any(str(f.get("id") or "") == ref for f in folders):
        return ref, [], None
    if _CHAT_FOLDER_ID_RE.fullmatch(ref):
        return "", [], f"folder not found: {redact(ref)}"

    # Reading 2: a folder whose own name (or ancestry) renders to exactly this path.
    paths = _chat_folder_paths(folders)
    exact = sorted(fid for fid, p in paths.items() if p.strip().lower() == ref.lower())
    if len(exact) > 1:
        return (
            "",
            [],
            (
                f"{len(exact)} folders render the same path {redact(ref)} "
                f"({', '.join(exact)}) — pass the folder id instead of a path"
            ),
        )

    # Reading 3: walk the segments. When the exact reading already resolved, the
    # walk runs resolve-only — there is nothing to create, and creating before
    # the two readings are compared would mutate the tree on a reference we are
    # about to refuse.
    walked, created, walk_err = _walk_chat_folder_segments(
        ref,
        folders,
        create_missing=create_missing and not exact,
        client=client,
        session_key=session_key,
        before_create=before_create,
    )
    if walk_err:
        return "", created, walk_err

    if exact and walked and walked != exact[0]:
        return (
            "",
            [],
            (
                f"{redact(ref)} is ambiguous: it is both a folder's own name "
                f"({exact[0]}) and a nested path ({walked}) — pass the folder id"
            ),
        )
    if exact:
        return exact[0], [], None
    if walked:
        return walked, created, None
    if not create_missing:
        return "", [], f"folder not found: {redact(ref)}"
    # create_missing with nothing walked means every segment was created and the
    # walk returned the leaf, so this is unreachable via the tools; keep the
    # refusal rather than returning the library root by accident.
    return "", created, f"folder not found: {redact(ref)}"


def _walk_chat_folder_segments(
    ref: str,
    folders: list[dict],
    *,
    create_missing: bool,
    client: DashboardClient | None = None,
    session_key: str | None = None,
    before_create: Callable[[str], str | None] | None = None,
) -> tuple[str, list[str], str | None]:
    """Walk a ``/``-separated path segment by segment. ONE walk, two modes.

    Returns ``(folder_id, created_names, error)``; ``folder_id`` is ``""`` when a
    segment is missing and ``create_missing`` is false. Duplicate siblings are
    refused in BOTH modes — resolving to the first match would act on an
    arbitrary folder, and creating under it would bury the new folder in
    whichever duplicate happened to come first.

    ``client`` makes the creates (and the re-read after a lost race), so the
    create mode needs one. ``folders`` is appended in place for each created row
    so a later path render sees it. Created names come back even alongside an error, so a partial
    mkdir -p is reported rather than silently left behind.

    Two checks run before the FIRST folder is created, so a walk that would
    stop part-way stops with nothing made. Every segment's length is tested up
    front. ``before_create`` is called once, with the id of the deepest folder
    that already exists, just before the first create; an error it returns
    ends the walk there. It is how ``session_create`` asks whether the create
    itself would be refused before any folder exists for it.
    """
    walked = ""
    parent = ""
    created: list[str] = []
    checked_before_create = before_create is None
    segments = [s.strip() for s in ref.split("/") if s.strip()]
    if create_missing:
        for raw in segments:
            seg = redact(raw)
            if len(seg) > _MAX_FOLDER_NAME:
                return (
                    "",
                    created,
                    f"folder name too long ({len(seg)} chars): "
                    f"`{seg[:40]}…` — keep each path segment to "
                    f"{_MAX_FOLDER_NAME} characters or fewer",
                )
    for raw in segments:
        # Redact BEFORE the lookup, not only before the write. The name is
        # agent-authored and lands in durable state the sidebar re-renders on
        # every visit, so it gets the egress pass (same reason issue-radar
        # findings are redacted before they persist) — which means the STORED
        # name is the redacted one. Matching on the raw text would therefore
        # never find the folder this walk itself created, and every repeated
        # call would add another sibling. One value for both halves.
        seg = redact(raw)
        # The endpoint stores ``name[:100]``, so a longer segment would come back
        # under a name this walk cannot match — and the next call, still not
        # matching, would create ANOTHER truncated sibling. Refuse instead: a
        # caller who is told the limit can shorten the name, while a silent
        # truncation buries duplicates under a path nobody asked for. Measured
        # on the redacted form, since that is what gets stored and truncated.
        if len(seg) > _MAX_FOLDER_NAME:
            return (
                "",
                created,
                f"folder name too long ({len(seg)} chars): "
                f"`{seg[:40]}…` — keep each path segment to "
                f"{_MAX_FOLDER_NAME} characters or fewer",
            )
        matches = _chat_folder_children(folders, parent, seg)
        if len(matches) > 1:
            return "", created, _ambiguous_segment_error(seg, matches)
        if matches:
            walked = str(matches[0].get("id") or "")
            parent = walked
            continue
        if not create_missing:
            return "", created, None
        if not checked_before_create and before_create is not None:
            checked_before_create = True
            refused = before_create(parent)
            if refused:
                return "", created, refused
        assert client is not None  # the create mode always carries one
        try:
            made = client.post(
                "/api/chat/folders",
                {"name": seg, "parent_id": parent},
                session_key=session_key,
            )
        except DashboardError as refused:
            made = refused.body
        # The lost race is read off the code before any error, as the endpoint's
        # refusal names it, so a reply carrying the code alone is that race too.
        if made.get("code") == "folder_name_exists":
            # The endpoint refuses an agent a same-name sibling under its lock.
            # Either a concurrent walk created this segment after our read, or
            # the folder exists but this caller's view of the tree omits it (a
            # crew member reads only its own folders). Re-read once: the first
            # case resolves to the winner's folder, the second is refused
            # rather than forked into a duplicate beside the one it cannot see.
            fresh, fresh_err = _get_rows(client, "/api/chat/folders")
            if fresh_err:
                return "", created, redact(str(fresh_err))
            folders[:] = fresh
            matches = _chat_folder_children(folders, parent, seg)
            if len(matches) > 1:
                return "", created, _ambiguous_segment_error(seg, matches)
            if matches:
                walked = str(matches[0].get("id") or "")
                parent = walked
                continue
            return (
                "",
                created,
                (
                    f"a folder named `{seg}` already exists there and is not one "
                    "this session can file into, so no duplicate was created — "
                    "use a path under a folder you created, or a different name"
                ),
            )
        if made.get("error"):
            return "", created, str(made["error"])
        if made.get("reused"):
            # The endpoint handed back this caller's own same-name folder, which
            # a concurrent walk created after our read. Nothing new exists.
            made = {k: v for k, v in made.items() if k != "reused"}
            if not any(str(f.get("id") or "") == str(made.get("id") or "") for f in folders):
                folders.append(made)
            walked = str(made.get("id") or "")
            parent = walked
            continue
        folders.append(made)
        created.append(str(made.get("name") or seg))
        walked = str(made.get("id") or "")
        parent = walked
    return walked, created, None


def _resolve_chat_folder_id(ref: str, folders: list[dict]) -> tuple[str, str | None]:
    """Resolve an EXISTING sidebar folder (id or human path) to its id."""
    fid, _created, err = _resolve_chat_folder_ref(ref, folders, create_missing=False)
    return fid, err


def _ensure_chat_folder_path(
    ref: str,
    folders: list[dict],
    *,
    client: DashboardClient,
    session_key: str,
    before_create: Callable[[str], str | None] | None = None,
) -> tuple[str, list[str], str | None]:
    """Resolve a parent-folder reference, creating missing segments (mkdir -p).

    ``session_key`` is REQUIRED because this variant writes: the intermediate
    segments are real folders, and each must be created under the identity the
    caller's gate verified rather than one the client re-derives.
    """
    return _resolve_chat_folder_ref(
        ref,
        folders,
        create_missing=True,
        client=client,
        session_key=session_key,
        before_create=before_create,
    )


def _resolve_chat_slot_key(ref: str, slots: list[dict]) -> tuple[str, str | None]:
    """Resolve a session reference to the slot key the folder endpoint takes.

    Accepts the slot key itself, a ``dashboard:<slot>`` session key (what
    ``search_chat_history`` and the session tools hand out), or a session's
    exact title when that title is unique. Title matching is exact and
    case-insensitive — never a substring — so an ambiguous or partial name
    fails loudly with the candidate keys instead of filing the wrong session.
    """
    ref = str(ref or "").strip()
    if not ref:
        return "", "session required"
    explicit_key = ref.lower().startswith("dashboard:")
    bare = ref[len("dashboard:") :] if explicit_key else ref
    by_key = {str(s.get("key") or ""): s for s in slots if s.get("key")}
    titled = [
        str(s.get("key") or "")
        for s in slots
        if str(s.get("title") or "").strip().lower() == ref.lower()
    ]
    if bare in by_key:
        # A tab can be TITLED with another session's key, so a bare reference can
        # match one session by key and a different one by title. Preferring the
        # key silently would file the wrong session; the ``dashboard:`` prefix is
        # the caller's way to say "this is a key".
        rivals = [k for k in titled if k != bare]
        if rivals and not explicit_key:
            return "", (
                f"{redact(ref)} is one session's key and another's title "
                f"({', '.join([bare] + rivals)}) — prefix with 'dashboard:' to "
                "select the key, or pass the other session's key"
            )
        return bare, None
    if explicit_key:
        # ``dashboard:`` asserted a key, and no slot has it. Falling through to
        # title matching would honour the opposite of what the caller said and
        # could file a session that merely happens to be TITLED with that key.
        return "", (
            f"no live session has the key {redact(bare)} — call chat_folder_tree "
            "for slot keys. An ARCHIVED session cannot be moved: bring it back "
            "with session_revive first"
        )
    if len(titled) == 1:
        return titled[0], None
    if len(titled) > 1:
        return "", (
            f"{len(titled)} live sessions share the title {redact(ref)} "
            f"({', '.join(titled)}) — pass the slot key instead"
        )
    return "", (
        f"no live session matches {redact(ref)} — call chat_folder_tree for slot "
        "keys. An ARCHIVED session cannot be moved: bring it back with session_revive first"
    )


def _resolve_chat_tag_ids(refs: list[str], tags: list[dict]) -> tuple[list[str], str | None]:
    """Resolve tag references (ids or exact names) to tag ids, in call order.

    Tag ids are minted as ``uuid.uuid4().hex[:12]`` (``chat_tags.create_tag_definition``).
    A reference is a tag id or a tag's exact name, matched case-insensitively
    and never as a substring — so a partial or unknown name fails loudly naming
    the vocabulary instead of tagging with the wrong label. Ids win over names
    when a tag is NAMED like another's id, since the id is the unambiguous form.
    Duplicates collapse; an unknown reference fails the whole call, so a delta
    is applied whole or not at all.
    """
    by_id = {str(t["id"]): t for t in tags if isinstance(t.get("id"), str) and t["id"]}
    by_name: dict[str, list[str]] = {}
    for t in tags:
        nm = str(t.get("name") or "").strip().lower()
        if nm and isinstance(t.get("id"), str) and t["id"]:
            by_name.setdefault(nm, []).append(str(t["id"]))
    out: list[str] = []
    for raw in refs:
        ref = str(raw or "").strip()
        if not ref:
            continue
        tid = ""
        if ref in by_id:
            tid = ref
        else:
            named = by_name.get(ref.lower(), [])
            if len(named) > 1:
                return [], (
                    f"{len(named)} tags share the name {redact(ref)} "
                    f"({', '.join(named)}) — pass the tag id instead"
                )
            if named:
                tid = named[0]
        if not tid:
            return [], (
                f"no tag matches {redact(ref)} — call chat_tag_list for the vocabulary, "
                "or chat_tag_create to add it"
            )
        if tid not in out:
            out.append(tid)
    return out, None


def _resolve_chat_tag_column(ref: str, columns: list[dict]) -> tuple[str, str | None]:
    """Resolve a column reference (id or exact name) to a column id.

    Same rules as :func:`_resolve_chat_tag_ids`: the id wins, a name matches
    whole and case-insensitively, and a name two columns share is refused
    rather than guessed, since moving the wrong column rearranges the board.
    """
    ref = str(ref or "").strip()
    ids = [str(c.get("id")) for c in columns if isinstance(c.get("id"), str) and c["id"]]
    if ref in ids:
        return ref, None
    named = [
        str(c["id"])
        for c in columns
        if isinstance(c.get("id"), str)
        and c["id"]
        and str(c.get("name") or "").strip().lower() == ref.lower()
    ]
    if len(named) > 1:
        return "", (
            f"{len(named)} columns share the name {redact(ref)} "
            f"({', '.join(named)}) — pass the column id instead"
        )
    if not ref or not named:
        return "", (
            f"no board column matches {redact(ref)} — call chat_tag_column_list for "
            "the current columns"
        )
    return named[0], None
