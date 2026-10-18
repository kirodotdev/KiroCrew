"""Listing and describing prompts: ``GET /api/prompts``.

The package SOP catalogue and the user and project prompt entries, the description
a list row shows, the lookup of one prompt by name, and the redaction a prompt
body gets before it leaves the gateway.

Served through :mod:`kiro_crew.dashboard.handlers.prompts`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers.prompts_caller_gates import (
    _audit_unread,
    _prompt_local_project,
    _sel,
)
from kiro_crew.dashboard.handlers.prompts_paths import _local_prompt_scan_root, _plain_stem_ok
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter
from kiro_crew.hooks import safe_read_file_bytes_nolink, validate_file_path
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

from ._shared import _read_session_key


def _list_aim_prompts(project_dir=None):
    """Import from parent to avoid circular — cache lives in __init__.py for test compat.

    ``project_dir`` is the caller's already-resolved local project (or ``None``)
    and is forwarded unchanged so the parent implementation appends that
    project's local prompts (see its docstring)."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg._list_aim_prompts(project_dir)


# ── Prompts (Agent SOPs) ──


def _description_from_text(text: str) -> str:
    """Description from a prompt's CONTENT: frontmatter first, first heading next.

    Text-based on purpose. The scoped read validates an inode and hands back
    that inode's bytes; re-opening the path afterwards to read metadata would
    reintroduce exactly the check-to-use window that read closes, and let a
    swapped entry answer through ``description``. Callers that already hold the
    validated bytes pass them here.

    Uses the same ``SKILL_LOADER`` grammar ``SkillsLoader._parse_frontmatter``
    uses — that method is a one-line path wrapper over this parser — so a
    block-scalar description resolves identically on both paths.
    """
    try:
        meta = parse_frontmatter(text, SKILL_LOADER)
    except ValueError:
        meta = {}
    if meta.get("description"):
        return meta["description"]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return re.sub(r"^#+\s*", "", stripped).strip()
    return ""


def _extract_sop_description(path: Path) -> str:
    """Description for a path the caller has NOT already read — the PACKAGE SOP walk.

    A user-prompt entry must use :func:`_gated_sop_description` instead, which
    pins its read inside the prompt root the entry was gated against; the scoped
    detail read must use :func:`_description_from_text`, on the bytes its read
    gate validated. This one is for the roots the platform seam supplies rather
    than a directory a checkout can write.

    Read through ``hooks.safe_read_file_bytes_nolink`` rather than by name. Every
    caller arrives here having already decided that *path* names a prompt, and a
    decision made about a NAME does not describe the file a later open by that
    name returns. Two things break the equivalence, and only one of them needs a
    race: the entry can be replaced between the decision and this open, and a
    HARDLINK needs no replacement at all — it shares its target's inode, so
    ``realpath`` yields the alias's own name, ``is_symlink()`` is False and
    ``is_sensitive_path`` sees an ordinary ``*.md`` sitting inside the prompt
    directory while the bytes belong to whatever it aliases. That is decisive
    here because a project's ``.kiro/prompts`` holds content the user CLONED and
    this description is PUBLISHED in the listing: an alias of ``~/.aws/credentials``
    would have that file's first ``#`` comment line served as a prompt's
    description, and its ``~/.ssh/config`` sibling likewise. The gate opens FIRST
    with ``O_NOFOLLOW`` and judges the descriptor it actually read — refusing
    ``st_nlink > 1``, a non-regular inode, and an ``is_sensitive_path`` target —
    so the inode described is the inode validated. ``st_nlink`` is the only
    signal a second name for a protected inode leaves, and it is readable only on
    a descriptor.

    ``within_root`` is the canonical path's OWN parent, and it is what carries
    this guarantee onto Windows, where ``O_NOFOLLOW`` does not exist at all: the
    gate asks for it with ``getattr``, so there the open follows a leaf swapped
    for a link and only the fd-real-path check
    (``GetFinalPathNameByHandleW``) can still see that the inode opened is not the
    one resolved. Deriving the root from the canonicalized path rather than from
    an authorization is deliberate and sufficient for that job — a link the
    caller's entry legitimately points at is already followed by the
    canonicalization, so its target's own directory is the root, and only a
    substitution landing AFTER it escapes.

    A refusal yields NO DESCRIPTION rather than dropping the entry. Whether a
    prompt exists is the caller's decision, not this function's, and a library
    that lost a file because its metadata was refused would hide a name the
    scoped read still serves. That also keeps an unreadable prompt (a bad mode,
    a transient error) listed with an empty description, exactly as the by-name
    read did — but it is SEL-recorded, because an entry that lists with no
    description is otherwise identical to a prompt that simply has none, and a
    planted alias would leave the operator nothing to find. The decode failure
    below is not recorded: it is a property of bytes the gate already admitted,
    not a withheld read.

    ``allow_truncate`` keeps the gate's 50 MB cap a bound rather than a refusal:
    a description is frontmatter or the first heading, both at the head of the
    file, so a truncated read answers the same question, while raising would turn
    one oversized file into a 500 for the whole listing — something the
    unbounded by-name read could not do.
    """
    # Strict decode: a file that is not UTF-8 has no description we can trust,
    # and replacement characters would put mojibake in the list.
    try:
        canonical = validate_file_path(str(path))
        if canonical is None:
            _audit_unread("api_prompts", "prompt", "blocked", {"path": str(path)})
            return ""
        raw = safe_read_file_bytes_nolink(
            canonical, within_root=os.path.dirname(canonical), allow_truncate=True
        )
        if raw is None:
            _audit_unread("api_prompts", "prompt", "error", {"path": str(path)})
            return ""
        return _description_from_text(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        return ""


def _gated_sop_description(path: Path, root_real: Path) -> str:
    """Description for a user-prompt entry, read through the no-link gate.

    :func:`_extract_sop_description` derives its ``within_root`` from the
    canonical path's own parent, which is the only root available to it — it
    spans the package SOP roots, which are plural and come from the platform
    seam. A user-prompt entry has a single authorizing root, so this one is given
    it: *root_real* is the prompt root RESOLVED ONCE by
    :func:`_local_prompt_scan_root`, not the caller-addressed directory. That
    distinction is the point. ``within_root`` is realpath'd inside the gate, so
    passing the as-addressed root would resolve through a root swapped for a link
    and admit the whole directory the swap named; passing the pinned value refuses
    it, because the opened descriptor's real path is then not inside the root the
    entry was approved against.

    The gate opens first with ``O_NOFOLLOW`` and validates the descriptor it
    actually read, so the inode described is the inode approved even though the
    minting gate reached its verdict by ``lstat`` on a name. Without that, an
    entry swapped for a symlink between that ``lstat`` and this open would get its
    TARGET's first heading — or its ``description`` frontmatter — published in the
    listing, for a file every read verb on this API refuses; a project's
    ``.kiro/prompts`` is content the user cloned rather than authored, so that
    swap is a shape the directory's author can arrange. The scoped read derives
    its own description from validated bytes for exactly this reason.

    A refusal or an unreadable file yields an EMPTY description rather than a
    refusal to name the file: the entry's contract is that a bad mode or a
    transient I/O error surfaces as the read path's own error instead of a prompt
    silently vanishing from the user's library. That identical answer is why a
    withheld read is SEL-recorded — an entry listing with no description is
    otherwise byte-identical to a prompt that simply has none, so a planted alias
    would leave the operator nothing to find. Strict decode, so a file that is not
    UTF-8 yields no description rather than mojibake; that one is NOT recorded,
    being a property of bytes the gate already admitted.

    ``allow_truncate`` rather than a byte cap, for the reason
    :func:`_extract_sop_description` gives: a description is frontmatter or a
    first heading, both at the head of the file, so a truncated read answers the
    same question — while a cap that RAISED would cost an oversized prompt the
    description the by-name read gave it, and would make one oversized file the
    listing's problem rather than that prompt's.
    """
    raw = safe_read_file_bytes_nolink(str(path), within_root=str(root_real), allow_truncate=True)
    if raw is None:
        _audit_unread("api_prompts", "prompt", "error", {"path": str(path)})
        return ""
    try:
        return _description_from_text(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return ""


def _local_prompt_entry(name: str, project_dir: Path | None) -> dict[str, Any] | None:
    """The single local prompt *name* can select, by exact stem — one open, not a listing.

    ``_find_prompt`` is reached once per turn that starts with ``@``, so resolving
    "local" by listing ``<project>/.kiro/prompts`` the way the prompts tab does
    would put a ``read_text`` per file — a description extraction for every prompt
    in the directory — on that turn, in a directory the gateway does not own and
    that may be network-backed. Being off the event loop bounds the blast radius
    of a slow read to the one turn; it does not make the read free. A local
    entry's ``name`` and ``fullName`` are both the
    file stem, so exactly one entry in that directory can ever match: find that
    one entry and gate it. The cost is then O(1) in the number of prompts the
    project keeps there rather than linear in it: the root gate's ``lstat`` and
    two ``resolve``s, one ``getdents``, the entry gate's own stats, and — only on
    a HIT — one ``read_text`` of the matched file for its description. The
    caller reads that same file again for the body it substitutes, so a resolved
    ``@mention`` opens the matched prompt twice; a MISS opens nothing.

    The candidate path is built from the **directory's own** entry name, never by
    joining *name* onto the prompt root. The two spellings would open the same
    inode — ``_plain_stem_ok`` runs first and rejects a separator, a ``..`` and a
    dotfile, so a joined name could not leave the directory either — but only the
    enumerated form makes that unconditional rather than a property of the
    predicate, and it is what lets a path-traversal analysis see it. ``@sub/foo``
    and ``@../escape`` are therefore misses that never reach the filesystem at
    all. ``os.scandir`` needs no ``stat`` to answer a name, so the comparison
    costs one ``getdents`` regardless of directory size.

    The ROOT is gated and pinned by ``_local_prompt_scan_root`` and the ENTRY by
    the parent package's ``_prompt_dir_entry`` — the same two gates the listing scan and
    every serving verb use, in the same order, so a name resolved here and a name
    resolved by the scan are refused on identical grounds. That includes
    ``_prompt_dir_entry``'s ``RuntimeError`` catch, since a symlink loop is
    exactly the kind of entry an untrusted checkout ships.
    """
    if project_dir is None or not _plain_stem_ok(name):
        return None
    import kiro_crew.dashboard.handlers as _pkg

    # Same root gate as the scoped read, both write verbs and the listing scan:
    # a prompt root the REPOSITORY redirected out of the project names files the
    # user never authored, and _prompt_dir_entry cannot see that — it compares an
    # entry against the root it is GIVEN, and a redirected directory makes
    # everything inside it look confined once that root is re-resolved. So the
    # root is resolved ONCE here and every entry is gated against that pinned
    # value; a swap landing after it costs this lookup its answer instead of
    # redirecting it. Its lstat plus two resolves are the only filesystem work
    # this adds, and they are O(1) in directory size.
    roots = _local_prompt_scan_root(Path(project_dir))
    if roots is None:
        return None
    prompts_dir, root_real = roots
    target = f"{name}.md"
    try:
        with os.scandir(prompts_dir) as entries:
            for entry in entries:
                if entry.name == target:
                    return _pkg._prompt_dir_entry(prompts_dir / entry.name, root_real, "local")
    except (OSError, ValueError, RuntimeError):
        # A missing or unreadable prompt root is a miss, never an unaudited 500
        # (a link-redirected one is already refused above). A name carrying an
        # embedded NUL (``%00`` in the URL
        # path) needs no case of its own: it is compared against directory entry
        # names rather than joined into a path, and no real entry name can hold
        # one, so it simply does not match.
        return None
    return None


def _redact_prompt(p: dict[str, Any]) -> None:
    """Redact credential patterns and exfiltration URLs from prompt metadata."""
    for field in ("description", "path"):
        p[field], _ = redact_credentials(p[field])
        p[field], _ = redact_exfiltration_urls(p[field])


async def api_prompts(request: web.Request) -> web.Response:
    """GET /api/prompts — list available prompts and agent SOPs."""
    # _list_aim_prompts() walks the edition package tree (rglob *.sop.md +
    # per-file resolve/read + frontmatter parse) on a cold cache — blocking FS
    # work that can stall the event loop on a large tree. It has a 5s TTL cache,
    # but the cold/expired build must run off the loop. (The cache lives in the
    # parent package; the executor call still benefits from it on warm builds.)
    # Resolve the local project on the loop (_prompt_local_project only reads
    # state._slots, non-blocking) and capture it into the executor job, so
    # "local" entries come from the requester's own checkout rather than the
    # process-wide KIROCREW_PROJECT_DIR — which on a source install names the
    # Kiro Crew tree itself and on a wheel install names nothing, so a prompt
    # the user authored in their project would never be listed here.
    state: DashboardState = request.app["state"]
    session_key = _read_session_key(request)
    project_dir = _prompt_local_project(request, state, session_key)
    prompts = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), functools.partial(_list_aim_prompts, project_dir)
    )
    home = str(Path.home())
    for p in prompts:
        _redact_prompt(p)
        p["path"] = p["path"].replace(home, "~")
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_prompts_list",
        tool_kind="prompt",
        outcome="ok",
        metadata={"count": len(prompts)},
    )
    return web.json_response(prompts)


def _find_prompt(raw_name: str, project_dir=None) -> dict[str, Any] | None:
    """Resolve a prompt by bare name, fullName, or ``package/name``.

    ``project_dir`` is the caller's already-resolved local project (or ``None``).
    The project-independent half (package SOPs + global user prompts) is searched
    by list, under the 5s cache; the local half is resolved by exact name through
    :func:`_local_prompt_entry`, which costs one directory read and one open
    instead of a description read per prompt in the directory. Ordering
    is preserved either way — the project-independent half wins a stem collision,
    exactly as it did when both halves were one list — and this is what keeps the
    per-turn ``@mention`` path free of an unbounded, uncacheable directory walk.

    A ``package/name`` spelling can only ever name a package SOP (a user prompt's
    ``package`` is ``""``), so it skips the local lookup rather than probing the
    project for a path it could not return."""
    pkg_filter = ""
    name = raw_name
    if "/" in raw_name:
        pkg_filter, name = raw_name.split("/", 1)
    for p in _list_aim_prompts():
        if pkg_filter and p["package"] != pkg_filter:
            continue
        if p["name"] == name or p["fullName"] == name:
            return p
    if pkg_filter:
        return None
    return _local_prompt_entry(name, project_dir)
