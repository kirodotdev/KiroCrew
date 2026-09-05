"""Prompts (Agent SOPs) and Skills API handlers."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import agent as _agent
from kiro_crew import pinned_fs
from kiro_crew.agent_discovery import agent_skill_globs
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter
from kiro_crew.hooks import (
    FileTooLargeError,
    safe_read_file_bytes_nolink,
    validate_file_path,
    verified_replace_file_nolink,
)
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.skill_trust import ReviewedProjectChanged as _ReviewedProjectChanged
from kiro_crew.skill_trust import (
    TrustStoreFull,
    TrustStoreUnreadable,
    canonical_key,
    grant_project_trust,
    is_key_trusted,
    list_trusted_projects,
    revoke_project_trust,
)

from ._shared import (
    _capability_manager,
    _get_skills,
    _read_session_key,
    _resolve_skill_root,
    active_project_dir,
    collect_skills_blocking,
    list_skill_tree,
    read_skill_file,
    requesting_slot_project,
)


def _list_aim_prompts():
    """Import from parent to avoid circular — cache lives in __init__.py for test compat."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg._list_aim_prompts()


logger = logging.getLogger(__name__)

MAX_PROMPT_BYTES = 100_000  # 100 KB — public constant, imported across dashboard + gateway + tests
_CODE_DASHBOARD_OWNER_REQUIRED = "dashboard_owner_required"
_CODE_SLOT_NOT_FOUND = "slot_not_found"


def _deny_non_owner_skill_trust(request: web.Request, operation: str) -> web.Response | None:
    """Restrict project-skill consent state to the configured dashboard owner."""
    if is_owner_dashboard_request(request):
        try:
            _sel().log_api_access(
                caller=str(request.get("user") or request.get("app") or "unknown"),
                operation=operation,
                outcome="allowed",
                source="dashboard",
            )
        except Exception:  # noqa: BLE001 — preserve authorized access if SEL is unwritable
            logger.debug("Could not audit allowed project-skill trust access", exc_info=True)
        return None
    try:
        _sel().log_api_access(
            caller=str(request.get("user") or request.get("app") or "unknown"),
            operation=operation,
            outcome="denied",
            source="dashboard",
            error="dashboard owner required",
        )
    except Exception:  # noqa: BLE001 — preserve the denial response if SEL is unwritable
        logger.debug("Could not audit denied project-skill trust access", exc_info=True)
    return web.json_response(
        {
            "error": "dashboard owner required",
            "code": _CODE_DASHBOARD_OWNER_REQUIRED,
        },
        status=403,
    )


def _deny_foreign_app_skill_slot(
    request: web.Request,
    state: DashboardState,
    session_key: str,
    operation: str,
) -> web.Response | None:
    """Require an app caller to own a project-bound slot selected by its header.

    Dashboard requests have owner-wide visibility. An app permission only opens
    the endpoint; it does not let the app select a foreign or unscoped slot and
    use another slot's project as a metadata/read oracle. Missing, projectless,
    and foreign slots return 404 so the isolation check does not enumerate slot
    identities or project bindings.
    """
    request_app = request.get("app", "")
    if not request_app:
        return None
    slot_name = session_key.split(":", 1)[-1] if session_key else ""
    slots = getattr(state, "_slots", {}) or {}
    slot = slots.get(slot_name) if slot_name else None
    owner = getattr(slot, "_app", "") if slot is not None else ""
    if owner == request_app and requesting_slot_project(state, session_key) is not None:
        try:
            _sel().log_api_access(
                caller=request_app,
                operation=operation,
                outcome="allowed",
                source="app_isolation",
                resources=f"slot={slot_name}",
            )
        except Exception:  # noqa: BLE001 — preserve authorized access if SEL is unwritable
            logger.debug("Could not audit allowed app skill access", exc_info=True)
        return None
    if slot is None:
        reason = "slot not found"
    elif owner == request_app:
        reason = "owned slot has no project"
    elif owner:
        reason = "app does not own this slot"
    else:
        reason = "app cannot access unscoped slots"
    try:
        _sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot_name}",
            error=reason,
        )
    except Exception:  # noqa: BLE001 — preserve the anti-enumeration response
        logger.debug("Could not audit denied app skill access", exc_info=True)
    return web.json_response({"error": "not found", "code": _CODE_SLOT_NOT_FOUND}, status=404)


def _sel():
    """Late-binding sel() — allows monkeypatching at parent package level."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


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


def _audit_unread(tool_name: str, tool_kind: str, outcome: str, metadata: dict) -> None:
    """SEL-record content a read gate would not hand back.

    A silent fallback is what makes a planted alias invisible: the surface keeps
    answering — an entry with no description, a 404 — so nothing above it can see
    that a read was refused rather than merely empty. SEL is operator-side and
    unreachable through the endpoint, so recording the event here leaves the HTTP
    response no more of an oracle than it already was.

    *outcome* is coarse on purpose, and the coarseness is the gate's rather than
    a choice: ``blocked`` is the one cause that is knowable, because
    ``validate_file_path`` rejected the name outright before any open. Everything
    else collapses into a bare ``None`` from one descriptor — a refused inode
    (hardlinked, non-regular, escaped its parent) and an ordinary read failure
    are indistinguishable there, and re-``stat``ing the path to tell them apart
    would be another by-name look at the input these reads exist to stop
    trusting. So the audit line records THAT the bytes were withheld, never why.

    Best-effort: a listing or a detail view must not fail because an audit write
    did.
    """
    try:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name=tool_name,
            tool_kind=tool_kind,
            outcome=outcome,
            metadata=metadata,
        )
    except Exception:  # noqa: BLE001 — an audit write must not break the response
        logger.debug("Could not audit an unread %s", tool_kind, exc_info=True)


def _extract_sop_description(path: Path) -> str:
    """Description for a path the caller has NOT already read — the listing walk.

    The scoped detail read must use :func:`_description_from_text` instead, on
    the bytes its read gate validated.

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
    prompts = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _list_aim_prompts
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


def _find_prompt(raw_name: str) -> dict[str, Any] | None:
    """Resolve a prompt by bare name, fullName, or ``package/name``."""
    pkg_filter = ""
    name = raw_name
    if "/" in raw_name:
        pkg_filter, name = raw_name.split("/", 1)
    for p in _list_aim_prompts():
        if pkg_filter and p["package"] != pkg_filter:
            continue
        if p["name"] == name or p["fullName"] == name:
            return p
    return None


async def api_prompt_detail(request: web.Request) -> web.Response:
    """GET/PUT/DELETE /api/prompts/{name} — read, update, or delete a prompt.

    GET resolves across all sources (package SOPs + user prompts). PUT and
    DELETE address user prompts only: bare file stem plus an explicit
    ``?scope=`` query (see the authoring section below).
    """
    if request.method in ("PUT", "DELETE"):
        return await _api_prompt_write(request)
    raw = request.match_info["name"]
    # An explicit ?scope= resolves the file directly, the same way a write does.
    # Without it this falls back to first-match across every source, so a global
    # and a project prompt sharing a stem are indistinguishable — and an editor
    # seeded from the wrong one would save it under the other's scope.
    scope = request.query.get("scope", "")
    if scope in _PROMPT_SCOPES:
        return await _api_user_prompt_detail(request, raw, scope)
    # _find_prompt() → _list_aim_prompts() does an rglob('*.sop.md') walk over the
    # (possibly large / edition-provided) prompt roots on a cold/expired cache;
    # offload it so a slow FS can't stall the event loop.
    p = await asyncio.get_running_loop().run_in_executor(discovery_executor(), _find_prompt, raw)
    if not p:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_prompt_detail",
            tool_kind="prompt",
            outcome="not_found",
            metadata={"name": raw},
        )
        return web.json_response({"error": "not found"}, status=404)
    name = raw.split("/", 1)[-1] if "/" in raw else raw
    resolved = validate_file_path(p["path"])
    if resolved is None:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_prompt_detail",
            tool_kind="prompt",
            outcome="blocked",
            metadata={"name": name, "path": p["path"]},
        )
        return web.json_response({"error": "access denied"}, status=403)
    # Read through the SAME hardlink-rejecting gate the scoped read uses, not by
    # name. ``validate_file_path`` canonicalizes and refuses a sensitive target,
    # but a canonical path is a fact about a NAME at one instant: opening that
    # name again re-resolves every component, so the bytes served here need not
    # be the bytes anything above checked. A hardlink defeats the check outright
    # with no race at all — it shares its target's inode, so the alias's own path
    # is what ``realpath`` yields and ``is_sensitive_path`` judges, while the
    # bytes are the aliased file's. This endpoint resolves across every source,
    # including a project's ``.kiro/prompts``, which holds content the user
    # CLONED — so an aliased ``~/.aws/credentials`` there would be returned in
    # full. The gate opens FIRST with ``O_NOFOLLOW``, then ``fstat``s that one
    # descriptor and refuses ``st_nlink > 1`` or a non-regular inode, making the
    # inode validated the inode returned.
    #
    # ``within_root`` is the canonical path's OWN parent, not an authorization:
    # this branch resolves across every source (package SOP roots — plural, from
    # the platform seam — and both user scopes), so there is no single authorized
    # directory to name the way ``?scope=`` names one for the scoped read. It is
    # passed anyway because it is the ONLY thing that carries this guarantee onto
    # Windows, where ``O_NOFOLLOW`` does not exist and the gate's ``getattr`` for
    # it yields 0: there the open follows a leaf swapped for a link after
    # canonicalization, and the fd-real-path check
    # (``GetFinalPathNameByHandleW``) is what still sees that the inode opened is
    # not the one resolved. A link the entry legitimately points at is already
    # followed by ``validate_file_path``, so its target's own directory IS the
    # root and only a later substitution escapes it.
    #
    # The cap moves from a pre-read ``stat`` onto the gate's own bound, so the
    # size that refuses is the size of the bytes actually read rather than of a
    # separately-stat'd name. ``FileTooLargeError`` is not an ``OSError``, so it
    # is caught explicitly to keep the coded 413 instead of an unaudited 500.
    try:
        body_bytes = safe_read_file_bytes_nolink(
            resolved, within_root=os.path.dirname(resolved), max_bytes=MAX_PROMPT_BYTES
        )
    except FileTooLargeError:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_prompt_detail",
            tool_kind="prompt",
            outcome="too_large",
            metadata={"name": name, "path": p["path"]},
        )
        return web.json_response({"error": "file too large"}, status=413)
    if body_bytes is None:
        # The gate refuses and reads through one descriptor, so it cannot say
        # which of the two happened — and collapsing them is the point rather
        # than a loss: reporting a refusal differently from an I/O error would
        # make this endpoint an oracle for whether a given path is protected.
        # Reported as the "not readable" outcome the by-name read already
        # produced for a file it could not open.
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_prompt_detail",
            tool_kind="prompt",
            outcome="error",
            metadata={"name": name, "path": p["path"]},
        )
        return web.json_response({"error": "file not readable"}, status=500)
    content = body_bytes.decode("utf-8", errors="replace")
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_prompt_detail",
        tool_kind="prompt",
        outcome="ok",
        metadata={"name": name, "path": p["path"]},
    )
    # Report whether this copy was transformed. The editor writes what it was
    # given, so a redacted copy must never be offered as an edit base: saving it
    # would replace the real token with the redaction marker. The write path
    # cannot detect that after the fact, so the read path says so here.
    content, cred_hits = redact_credentials(content)
    content, url_hits = redact_exfiltration_urls(content)
    out = dict(p)
    _redact_prompt(out)
    # Strip full filesystem path — return display-only relative path
    out["path"] = out["path"].replace(str(Path.home()), "~")
    return web.json_response(
        {
            **out,
            "name": name,
            "content": content,
            "redacted": bool(cred_hits or url_hits),
        }
    )


# ── Prompt authoring (user-sourced prompts only) ──
#
# Writes address their target by explicit ``scope`` (``global``/``local``) plus
# file stem — never by list-order resolution — so a name that exists in both
# user directories can never be written through the wrong one. Package SOPs are
# not addressable here at all: every write path is confined to the two user
# prompt directories, so "edit a package prompt" is unrepresentable rather than
# merely rejected.

_PROMPT_SCOPES = ("global", "local")

#: Filename byte budget for ``<name>.md``. Every mainstream filesystem caps a
#: single path component at 255 bytes; staying under it turns a would-be
#: ENAMETOOLONG (an unaudited 500 from deep in the executor) into a coded 400.
MAX_PROMPT_NAME_BYTES = 200


def _linked_prompt_root(d: Path) -> bool:
    """True when the prompt directory itself is a link, so writes would land
    outside the tree the caller named.

    Confinement compares the target against the RESOLVED scope directory, which
    is the right test for an entry inside that directory but says nothing about
    the directory itself: if ``~/.kiro/prompts`` is a symlink, both sides resolve
    into the link's destination and every path looks confined.

    Delegates to ``platform_compat.is_link_or_junction`` rather than testing
    ``is_symlink()`` here: ``islink`` is FALSE for a Windows directory junction,
    and that helper already carries the reparse-tag fallback for interpreters
    without ``os.path.isjunction``. A local probe would have to re-derive that,
    and ``stat.IO_REPARSE_TAG_MOUNT_POINT`` is not exported off Windows, so a
    hand-rolled version fails open on exactly the platform it is meant to cover.

    Deliberately only the leaf: an ANCESTOR symlink is normal (``/home/<user>``
    is itself a link on many hosts) and redirects nothing the user did not
    already choose, so testing ``resolve() != absolute()`` would refuse every
    write on those machines.
    """
    try:
        return is_link_or_junction(d)
    except OSError:
        return True  # unreadable root: refuse rather than guess


#: True when this platform can perform an operation RELATIVE to an open
#: directory descriptor. POSIX has ``openat``/``unlinkat`` behind these; Windows
#: has neither ``O_DIRECTORY`` nor ``dir_fd`` support, so there the by-name
#: operation plus the junction check on the root is all that is available.
#: ``supports_pinned_walk`` covers the openat capability itself; unlink/mkdir
#: are probed separately because delete removes and create mkdirs relative to
#: the pinned descriptor, and the Windows-simulation tests clear capabilities
#: selectively.
_DIR_FD_SUPPORTED = pinned_fs.supports_pinned_walk() and {os.unlink, os.mkdir}.issubset(
    os.supports_dir_fd
)

#: True when this interpreter can build a file with NO NAME at all -- Linux's
#: ``O_TMPFILE`` -- and hand it a name with ``linkat``. That is what lets create
#: publish a finished prompt without the body ever being reachable under a name,
#: and without the published inode passing through a two-link state.
#:
#: Deliberately only ATTRIBUTE lookups: this module is imported on the gateway
#: boot path, where every statement is paid on every launch before the socket
#: accepts requests. The remaining half of the capability -- whether
#: ``/proc/self/fd`` is actually mounted, which ``linkat``'s unprivileged form
#: needs -- is a filesystem question, so it is asked inside the write job in the
#: executor rather than here. A mount that refuses ``O_TMPFILE`` (NFS, some
#: overlayfs) cannot be probed at all: the capability is per-filesystem, so the
#: open is attempted and its refusal handled where it happens.
_UNNAMED_CREATE_SUPPORTED = bool(getattr(os, "O_TMPFILE", 0)) and os.link in os.supports_dir_fd


def _pin_prompt_dir(d: Path, *, create: bool = False) -> int:
    """Return a descriptor pinning the prompt directory *d*, via ``pinned_fs``.

    This is what closes the check-to-use window the by-name paths leave open.
    ``_resolve_prompt_dir`` validates a PATH; an operation that then names
    ``<dir>/<stem>.md`` re-resolves every component afresh, so replacing a
    directory in between makes it land somewhere the check never saw. Operations
    performed relative to this descriptor reach the inode the walk pinned, no
    matter what the names mean by then.

    The mechanism — the openat-per-component walk, the ``ELOOP``/``ENOTDIR``
    translation, the capability probe — lives in :mod:`kiro_crew.pinned_fs`,
    whose charter (two closed PRs' worth of call-site rewrites) is that callers
    stay thin consumers of one set of invariants. This wrapper only supplies
    this handler's policy:

    * Which links are REFUSED deliberately matches ``_linked_prompt_root``
      rather than being stricter. An already-existing ancestor link is a
      location the user chose — ``/home/<user>`` is a link on many hosts and
      dotfile managers routinely symlink ``~/.kiro`` — and ``pinned_fs``
      tolerates it the documented way: the parent chain is realpathed ONCE
      before the walk, so a pre-existing link is followed by that resolution
      and only a component swapped after it is refused. The leaf refuses a
      link outright (``O_NOFOLLOW``), which is ``_linked_prompt_root``'s rule.
    * With *create*, the parents are ensured BY NAME first — the module's
      contract ("callers create their own tree roots"), and the same policy as
      the read path for pre-existing links — and only the leaf is created
      through its pinned parent.

    Raises :class:`pinned_fs.PinnedPathRefusal` for a linked or non-directory
    component (callers map it to ``linked_prompt_root``), and plain ``OSError``
    for operational failures, which reach the generic write-failure path.
    """
    if create:
        d.parent.mkdir(parents=True, exist_ok=True)
        return pinned_fs.create_and_open_dir_pinned(d, what="prompt directory")
    return pinned_fs.open_dir_pinned(d, what="prompt directory")


def _invalidate_prompt_cache() -> None:
    """Late-binding into the parent package, where the cache globals live."""
    # circular import: the parent package imports this module, so a top-level
    # import here would not resolve.
    import kiro_crew.dashboard.handlers as _pkg

    _pkg._invalidate_prompt_cache()


def _resolve_prompt_dir(scope: str) -> tuple[Path | None, str | None]:
    """Resolve and validate the prompt directory for *scope*, OFF the loop.

    Returns ``(dir, None)`` or ``(None, error_code)``.

    Both halves touch the filesystem: resolving "local" reads the active
    project, and the link check ``lstat``s the directory. On a network-mounted
    home or project that is a multi-second stall, so every caller runs this
    inside its own executor job rather than on the event loop — the gateway
    serves other requests and the heartbeat keeps ticking meanwhile.
    """
    d = _user_prompt_dir(scope)
    if d is None:
        return None, "no_active_project"
    if _linked_prompt_root(d):
        return None, "linked_prompt_root"
    if scope == "local" and not _local_prompt_dir_in_project(d):
        return None, "linked_prompt_root"
    return d, None


def _local_prompt_dir_in_project(d: Path) -> bool:
    """True when the local prompt dir RESOLVES inside the resolved project root.

    The leaf-only rule in ``_linked_prompt_root`` tolerates ancestor links
    because a link under the user's own tree is a location the user chose. A
    project ``.kiro`` is authored by the REPOSITORY, not the user: a checkout
    shipping ``.kiro -> ~/.kiro`` would silently redirect local-scope writes
    and deletes into the global prompt tree. Comparing resolved-to-resolved
    keeps legitimately-linked project roots working while refusing any chain
    that leaves the project.
    """
    proj = _agent._project_dir()
    if not proj:
        return False
    try:
        return d.resolve().is_relative_to(Path(proj).resolve())
    except OSError:
        return False


def _user_prompt_dir(scope: str) -> Path | None:
    """Resolve the user prompt directory for *scope*.

    Deliberately the same resolver ``_list_aim_prompts`` uses (the
    gateway-global project dir): create and list must agree on where "local"
    is, or a created prompt would never appear in the listing. Moving both
    sides to per-chat-slot project resolution is a coordinated change to both.
    """
    if scope == "global":
        return Path.home() / ".kiro" / "prompts"
    # Called through the module rather than imported by name so that patching
    # ``kiro_crew.agent._project_dir`` (as the tests do) is still observed here.
    proj = _agent._project_dir()
    return proj / ".kiro" / "prompts" if proj else None


def _plain_stem_ok(stem: str) -> bool:
    """True when *stem* is a plain path component that cannot leave its
    directory — no traversal, no separator, no dotfile.

    Shared by the pending-skill slug check and the prompt-name check: both need
    exactly this predicate, and two spellings of it would drift.
    """
    return (
        bool(stem)
        and stem not in (".", "..")
        and not stem.startswith(".")
        and "/" not in stem
        and "\\" not in stem
        and ".." not in stem
    )


def _utf8_or_none(text: str) -> bytes | None:
    """UTF-8 bytes for *text*, or None when it has no UTF-8 form.

    JSON permits lone surrogates (``"\\ud800"``), and `json.loads` hands them
    back as a `str` that `str.encode("utf-8")` refuses. So a syntactically valid
    request body can carry content that cannot be written at all, and the size
    check — the first thing that encodes it — is where that surfaces. Returning
    None keeps it on the coded, audited 400 path; letting `UnicodeEncodeError`
    escape made it a bare 500 with no audit line, which is the one thing these
    handlers promise not to do.
    """
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return None


def _prompt_audit(op: str, outcome: str, **meta: Any) -> None:
    """Audit a prompt write — every outcome, rejections included (a refused write
    can be filesystem probing)."""
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name=op,
        tool_kind="prompt",
        outcome=outcome,
        metadata=meta,
    )


#: Serializes prompt mutations (PUT/DELETE) within this process. The update
#: path's compare-and-swap is check-then-write across two filesystem calls,
#: and the discovery pool runs jobs concurrently, so without this two writers
#: could both verify the same base before either replaces the file. Create
#: needs no lock: its atomicity is O_EXCL on the open itself.
_PROMPT_WRITE_LOCK = threading.Lock()


def _refuse(op: str, outcome: str, code: str, **meta: Any) -> None:
    """Audit a refused prompt write under the same identifier the response carries.

    The caller answers with a literal status and the same ``code`` (the contract
    the dashboard localizes on; ``error`` prose is advisory, RFC 9457 3.1.3).
    Passing ``code`` as the audited reason is what keeps "what was logged" and
    "what was answered" the same word.
    """
    _prompt_audit(op, outcome, reason=code, **meta)


_CODE_APP_TOKEN_FORBIDDEN = "app_token_forbidden"


def _deny_non_owner_prompt_write(request: web.Request, op: str, **meta: Any) -> web.Response | None:
    """403 unless this is the configured owner's dashboard request, else ``None``.

    Two refusals, distinct codes. An app-token caller — or an absent claim,
    where the middleware did not authenticate this request or deliberately
    withheld the claim, as the internal-secret transport does — is refused
    ``app_token_forbidden``: app-token grants are path-only, so an app whose
    manifest covers ``/api/prompts`` for its read surface would otherwise
    reach these mutations too. A dashboard caller who is not the configured
    owner is refused ``dashboard_owner_required``: prompts are the owner's
    agent instructions — a write is an instruction-injection surface — and
    allowed messaging users other than the owner can hold dashboard sessions,
    so "any dashboard user" is the wrong bar (``is_owner_dashboard_request``
    is reused rather than re-derived, and admits the signed local bootstrap
    subjects when no owner is configured). Both refusals fire before any body
    parsing and are SEL-audited under the code the response carries (``meta``
    carries the target, e.g. ``name``/``scope``, so a name-enumerating caller
    leaves attributable lines).
    """
    request_app = request.get("app")
    if request_app != "":
        try:
            _refuse(
                op,
                "blocked",
                _CODE_APP_TOKEN_FORBIDDEN,
                caller=str(request_app or "absent-claim"),
                **meta,
            )
        except Exception:  # noqa: BLE001 — preserve the denial response if SEL is unwritable
            logger.debug("Could not audit refused app-token prompt write", exc_info=True)
        return web.json_response(
            {"error": "app tokens may not modify prompts", "code": _CODE_APP_TOKEN_FORBIDDEN},
            status=403,
        )
    if not is_owner_dashboard_request(request):
        try:
            _refuse(
                op,
                "blocked",
                _CODE_DASHBOARD_OWNER_REQUIRED,
                caller=str(request.get("user") or "unknown"),
                **meta,
            )
        except Exception:  # noqa: BLE001 — preserve the denial response if SEL is unwritable
            logger.debug("Could not audit refused non-owner prompt write", exc_info=True)
        return web.json_response(
            {
                "error": "dashboard owner required",
                "code": _CODE_DASHBOARD_OWNER_REQUIRED,
            },
            status=403,
        )
    return None


async def api_prompts_create(request: web.Request) -> web.Response:
    """POST /api/prompts — create a user prompt. Body ``{name, content, scope}``.

    The name is sanitized to the skills rule minus ``/``: prompts are FLAT
    ``*.md`` files (the lister globs, it does not rglob), so a nested name
    would create a file the Prompts tab never shows.
    """
    op = "api_prompts_create"
    denied = _deny_non_owner_prompt_write(request, op)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        _refuse(op, "bad_request", "invalid_json")
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    raw_name = str(body.get("name", "")).strip()
    content = body.get("content")
    scope = str(body.get("scope", "global"))
    if not isinstance(content, str) or not content.strip():
        _refuse(op, "bad_request", "content_required", name=raw_name)
        return web.json_response(
            {"error": "content is required", "code": "content_required"}, status=400
        )
    if scope not in _PROMPT_SCOPES:
        _refuse(op, "bad_request", "bad_scope", scope=scope)
        return web.json_response(
            {"error": "scope must be 'global' or 'local'", "code": "bad_scope"}, status=400
        )
    encoded = _utf8_or_none(content)
    if encoded is None:
        _refuse(op, "bad_request", "content_not_encodable", name=raw_name)
        return web.json_response(
            {"error": "content is not valid UTF-8 text", "code": "content_not_encodable"},
            status=400,
        )
    if len(encoded) > MAX_PROMPT_BYTES:
        _refuse(op, "too_large", "content_too_large", name=raw_name)
        return web.json_response(
            {"error": "content exceeds size limit", "code": "content_too_large"}, status=413
        )
    safe_name = re.sub(r"[^a-z0-9\-]", "-", raw_name.lower()).strip("-")
    if not safe_name:
        _refuse(op, "bad_request", "invalid_name", name=raw_name)
        return web.json_response(
            {"error": "invalid prompt name", "code": "invalid_name"}, status=400
        )
    if len(f"{safe_name}.md".encode("utf-8")) > MAX_PROMPT_NAME_BYTES:
        _refuse(op, "bad_request", "name_too_long", name=safe_name, scope=scope)
        return web.json_response(
            {"error": "prompt name is too long", "code": "name_too_long"}, status=400
        )

    def _write() -> str | None:
        target_dir, err = _resolve_prompt_dir(scope)
        if target_dir is None:
            return err
        filename = f"{safe_name}.md"
        if not _DIR_FD_SUPPORTED:
            # No openat: the by-name create is the only option, so this platform
            # keeps the narrower guarantee of the junction check alone.
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / filename
            created_ident: tuple[int, int] | None = None
            try:
                # "x" mode makes create-if-absent atomic — no exists()/write race.
                # newline="" keeps the bytes byte-exact, as the update path does:
                # without it Windows expands every LF to CRLF, so a body just under
                # MAX_PROMPT_BYTES becomes a file over it — created successfully,
                # then rejected by its own read with 413.
                with path.open("x", encoding="utf-8", newline="") as f:
                    # Identity of the inode THIS create made, read from the
                    # descriptor while it is provably ours. The cleanup below
                    # re-resolves the name, and only this pair proves the entry
                    # still is the file this call created.
                    st = os.fstat(f.fileno())
                    created_ident = (st.st_dev, st.st_ino)
                    f.write(content)
            except FileExistsError:
                return "exists"
            except OSError:
                # The file now exists but holds a partial body, and O_EXCL would
                # answer the retry with 409 forever. Remove it so the caller's next
                # attempt is a clean create rather than a permanent conflict — but
                # only when the name still resolves to the inode this call created:
                # a concurrent writer can replace the entry inside the failure
                # window, and a bare by-name unlink would delete THEIR file. Same
                # narrower-guarantee posture as the junction check on this
                # no-openat path; a swap after the lstat below remains possible,
                # and losing the retry-cleanup then is the safe direction.
                if created_ident is not None:
                    try:
                        st_now = path.lstat()
                        if (st_now.st_dev, st_now.st_ino) == created_ident:
                            path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
            return None
        try:
            # The adapter ensures the parents by name and creates only the leaf
            # through its pinned chain — see _pin_prompt_dir for the policy.
            dir_fd = _pin_prompt_dir(target_dir, create=True)
        except pinned_fs.PinnedPathRefusal:
            # Only a linked or non-directory component is this refusal; EACCES,
            # EMFILE and friends escape as OSError to the generic write-failure
            # path, so a permission denial is not misdiagnosed as a linked root.
            return "linked_prompt_root"
        try:
            # This one write has to satisfy four things at once, and every
            # obvious shape satisfies only three:
            #
            #   (1) never publish a partial body;
            #   (2) never destroy a file another writer holds at the name;
            #   (3) leave the published inode at ONE link, because the read path
            #       a few hundred lines below goes through
            #       safe_read_file_bytes_nolink and refuses st_nlink > 1 -- a
            #       prompt with two links answers 403 forever, which is a worse
            #       dead end than the 409 this fixes;
            #   (4) leave nothing behind when the process dies mid-write.
            #
            # O_EXCL straight onto <stem>.md breaks (1): the name holds the body
            # while it is being written, so a failure strands a partial prompt
            # and every retry is refused 409 -- the reported defect. A cleanup
            # cannot fully repair that, because POSIX has no unlink-by-inode:
            # verify-then-unlink is two syscalls on one NAME, so an atomic save
            # landing between them destroys the replacement, breaking (2).
            # Writing a NAMED temp and linking it into place breaks (3) and (4):
            # between the link and the temp's removal the inode carries two
            # links, and a process death there leaves the prompt permanently
            # unreadable. rename would clobber, and renameat2's RENAME_NOREPLACE
            # is Linux-only and unexposed by CPython -- pinned_fs's
            # put_back_no_clobber records that same dead end.
            #
            # An UNNAMED inode has none of these problems. O_TMPFILE builds the
            # body with no name at all, so nothing can be observed half-written
            # and there is nothing to clean up -- a crash drops an inode that was
            # never linked. linkat then gives it its one and only name, and link
            # is create-if-absent, so an occupied name is still a 409 and what is
            # already there is never touched. The inode goes from zero links to
            # one and is never at two.
            fd = -1
            if _UNNAMED_CREATE_SUPPORTED and os.path.isdir("/proc/self/fd"):
                try:
                    # Opened THROUGH the pinned descriptor, so the inode is born
                    # on the filesystem holding the directory the walk
                    # validated. Mode matches what open("x") would have produced
                    # (0o666 & ~umask), because this inode is the one published.
                    fd = os.open(".", os.O_TMPFILE | os.O_WRONLY, 0o666, dir_fd=dir_fd)
                except OSError:
                    # O_TMPFILE is a per-FILESYSTEM capability, not a per-OS one:
                    # a mount without it answers EOPNOTSUPP here. Not a failure
                    # -- the named fallback below keeps the pinned guarantee
                    # there, at the cost of the residual it documents.
                    fd = -1
            if fd >= 0:
                try:
                    # Written on the raw descriptor rather than through
                    # os.fdopen, so ownership is unambiguous. fdopen's failure
                    # behaviour is split: an argument error raises BEFORE
                    # wrapping and leaves the descriptor open, while a late
                    # failure (a MemoryError building the buffer) runs io.open's
                    # error path, which closes it. Both are reachable -- probed
                    # on CPython 3.12 -- so a caller either leaks a descriptor or
                    # double-closes one, and a double close in this thread pool
                    # can shut a descriptor another worker just opened. Owning
                    # the fd here makes the close exactly once. No newline
                    # translation exists on this path at all: the descriptor is
                    # binary and handed the encoded bytes, so the file holds
                    # exactly what was posted.
                    data = content.encode("utf-8")
                    written = 0
                    while written < len(data):
                        written += os.write(fd, data[written:])
                    # Flushed BEFORE the name exists. 201 is a promise the prompt
                    # is on disk, and fsync is where a full or network-backed
                    # filesystem reports the error it deferred -- the
                    # reported-success-before-durable half of this bug. It also
                    # leaves the close below nothing to report, which is why a
                    # failing close here cannot strand anything.
                    os.fsync(fd)
                    try:
                        # The publish, and the only syscall that touches the
                        # user-visible name. linkat's AT_EMPTY_PATH form would
                        # need CAP_DAC_READ_SEARCH; the /proc spelling is the
                        # unprivileged equivalent, and follow_symlinks=True is
                        # what makes that entry resolve to the inode instead of
                        # being linked as a symlink.
                        os.link(
                            f"/proc/self/fd/{fd:d}",
                            filename,
                            dst_dir_fd=dir_fd,
                            follow_symlinks=True,
                        )
                    except FileExistsError:
                        return "exists"
                    # The link made the entry; this makes the ENTRY durable.
                    # fsync on the file settles its CONTENT, and a directory is a
                    # separate object -- so without this a power loss after a 201
                    # can come back with the body intact and no name pointing at
                    # it, which is the acknowledged-then-vanished case.
                    #
                    # It RAISES rather than being logged and swallowed. 201 is a
                    # claim the prompt is on disk, and a create that cannot flush
                    # the entry has not established that, so reporting success
                    # would be the very failure this change exists to close.
                    # Answering 500 costs little here: the body is already
                    # written, flushed and linked, so the caller sees a complete
                    # and readable prompt, and a retry gets a truthful 409 on a
                    # prompt that really does exist and can still be edited
                    # through the update path -- nothing like the truncated
                    # leftover that made the original 409 a dead end.
                    #
                    # The entry is deliberately NOT withdrawn on this path. There
                    # is nothing safe to withdraw it with: removing the leaf would
                    # mean unlinking the prompt's own name, which is exactly the
                    # verify-then-unlink hazard the unnamed publish exists to
                    # avoid. The by-name branch below does withdraw, because it
                    # already holds an identity-checked cleanup for its own leaf.
                    os.fsync(dir_fd)
                finally:
                    # Swallowed: fsync already settled durability, so after a
                    # successful publish a failing close must not turn a created
                    # prompt into a 500. Before the publish the inode has no
                    # name, so a failed close can leak nothing but the descriptor
                    # itself, which this reclaims.
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                return None
            # No O_TMPFILE on this filesystem. The body goes under its own name
            # and the failure path removes it, bound to the inode this call
            # created so the removal addresses an object rather than a name. The
            # residual is the one pinned_fs's _unlink_verified documents as
            # irreducible and ships: the verify and the unlink are two syscalls
            # on one name, so a replacement landing between them is lost. It is
            # accepted here for the reason it is accepted there -- POSIX offers
            # nothing better -- and it is now confined to filesystems that cannot
            # build an unnamed inode.
            # O_EXCL keeps create-if-absent atomic; O_NOFOLLOW refuses an
            # existing symlink at the name rather than writing through it. Both
            # resolve relative to the pinned directory, so a swap after the check
            # cannot redirect this write. O_BINARY matters only on Windows, which
            # has no openat and takes the by-name branch above, but the flag
            # belongs on any os.open this code owns.
            try:
                fd = os.open(
                    filename,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | getattr(os, "O_BINARY", 0),
                    0o666,
                    dir_fd=dir_fd,
                )
            except FileExistsError:
                return "exists"
            # Annotated once, in the no-openat branch above: mypy reads both
            # branches as one scope, so re-annotating here is a [no-redef].
            created_ident = None
            try:
                # Identity of the inode THIS create made, read from the
                # descriptor while it is provably ours, and unset until then so a
                # failure before this point forgoes the cleanup rather than
                # unlinking on a guess.
                leaf_st = os.fstat(fd)
                created_ident = (leaf_st.st_dev, leaf_st.st_ino)
                data = content.encode("utf-8")
                written = 0
                while written < len(data):
                    written += os.write(fd, data[written:])
                os.fsync(fd)
                # Closed INSIDE the guarded region: close is the other place a
                # deferred write error surfaces, so a close in a `finally` beside
                # the cleanup arm would skip the removal below and strand the
                # partial body. Clearing `fd` first keeps the arm's fallback
                # close from double-closing; atomic_write uses the same two lines
                # for the same reason.
                fd, open_fd = -1, fd
                os.close(open_fd)
                # Same reason as the unnamed branch above: the O_EXCL open made
                # the entry, and the entry needs its own flush before 201 can
                # claim the prompt is on disk. It raises here too -- and on THIS
                # path the failure also withdraws the publication, because the
                # arm below already removes this call's own leaf under an identity
                # check. A caller that gets the 500 can retry into a clean create.
                os.fsync(dir_fd)
            except BaseException:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    if created_ident is not None:
                        st_now = os.stat(filename, dir_fd=dir_fd, follow_symlinks=False)
                        if (st_now.st_dev, st_now.st_ino) == created_ident:
                            os.unlink(filename, dir_fd=dir_fd)
                except OSError:
                    pass
                raise
        finally:
            os.close(dir_fd)
        return None

    # A filesystem refusal (EACCES, ENOSPC, a name the FS still rejects) must
    # leave an audit trail and a coded answer, not escape as a bare 500 from
    # inside the executor. Caught on Exception rather than OSError — as the
    # skill handlers in this file already do — because the property claimed
    # here is that EVERY outcome is audited, and a non-OS failure inside the
    # job (a MemoryError on the encoded body, a bug in a helper) would
    # otherwise answer 500 with no audit line at all. CancelledError is a
    # BaseException, so a cancelled request still propagates.
    try:
        err = await asyncio.get_running_loop().run_in_executor(discovery_executor(), _write)
    except Exception:
        _refuse(op, "error", "write_failed", name=safe_name, scope=scope)
        return web.json_response(
            {"error": "could not write the prompt", "code": "write_failed"}, status=500
        )
    if err == "no_active_project":
        _refuse(op, "bad_request", "no_active_project", scope=scope)
        return web.json_response(
            {"error": "no active project for local scope", "code": "no_active_project"}, status=400
        )
    if err == "linked_prompt_root":
        _refuse(op, "blocked", "linked_prompt_root", scope=scope)
        return web.json_response(
            {"error": "prompt directory is a link", "code": "linked_prompt_root"}, status=403
        )
    if err == "exists":
        _refuse(op, "conflict", "prompt_exists", name=safe_name, scope=scope)
        return web.json_response(
            {"error": f"prompt '{safe_name}' already exists", "code": "prompt_exists"}, status=409
        )
    _invalidate_prompt_cache()
    _prompt_audit(op, "ok", name=safe_name, scope=scope)
    return web.json_response({"ok": True, "name": safe_name, "scope": scope}, status=201)


async def _api_user_prompt_detail(request: web.Request, name: str, scope: str) -> web.Response:
    """GET /api/prompts/{name}?scope= — read ONE user prompt, by scope and stem.

    Addressed exactly like a write, so the bytes the editor is seeded from are
    the bytes a following PUT would replace. ``redacted`` reports whether this
    copy was transformed on the way out; see the unscoped branch for why.
    """
    op = "api_prompt_detail"
    if not _plain_stem_ok(name):
        _refuse(op, "rejected", "invalid_name", name=name)
        return web.json_response(
            {"error": "invalid prompt name", "code": "invalid_name"}, status=400
        )

    def _read() -> tuple[str | None, str, str | None, str, bool, str]:
        # Directory resolution, the link check, and description extraction all
        # touch the filesystem, so they share this one executor job rather than
        # running on the loop.
        target_dir, derr = _resolve_prompt_dir(scope)
        if target_dir is None:
            return None, "", derr, "", False, ""
        target = target_dir / f"{name}.md"
        # Link check FIRST, and without following: ``is_file()`` follows a
        # symlink, so putting it first would answer 404 for a dangling link and
        # 403 for a live one — a per-path existence oracle for anything the
        # link's author cares to point at. Refusing every link with the same
        # code, before anything dereferences it, makes the two indistinguishable.
        # ``is_link_or_junction`` is lstat-based (False for a missing entry) and
        # covers Windows junctions, which ``is_symlink()`` does not.
        try:
            if is_link_or_junction(target):
                return None, "", "access denied", "", False, ""
        except OSError:
            return None, "", "access denied", "", False, ""
        if not target.is_file():
            return None, "", "not found", "", False, ""
        if target.resolve().parent != target_dir.resolve():
            return None, "", "access denied", "", False, ""
        if target.stat().st_size > MAX_PROMPT_BYTES:
            # Checked separately from the read's own cap so an oversized prompt
            # still answers with its own 413 code rather than a flat refusal.
            return None, "", "too large", "", False, ""
        # Read through the hardlink-rejecting gate rather than open()ing by
        # name: it opens with O_NOFOLLOW and validates the inode it actually
        # read (st_nlink > 1, non-regular, or a real path outside the root is
        # refused), so a sensitive file hardlinked into the prompt dir cannot
        # be served through this endpoint. It also enforces the size cap, so
        # the separate stat() that used to do that is gone.
        # The stat above is a separate syscall from the gate's own open, so a
        # prompt that grows past the cap in between would make the gate raise.
        # FileTooLargeError is not an OSError, so catching it here is what keeps
        # that race on the coded 413 path instead of an unaudited 500.
        try:
            raw = safe_read_file_bytes_nolink(
                str(target), within_root=str(target_dir), max_bytes=MAX_PROMPT_BYTES
            )
        except FileTooLargeError:
            return None, "", "too large", "", False, ""
        if raw is None:
            return None, "", "access denied", "", False, ""
        # Content tolerates undecodable bytes (the pane shows what is there),
        # but the description does not: strict-decode for metadata so a file
        # that is not UTF-8 yields no description rather than mojibake, which
        # is what the listing path does too.
        lossy = False
        try:
            description = _description_from_text(raw.decode("utf-8"))
        except UnicodeDecodeError:
            # The content copy below substitutes U+FFFD for the bytes it could
            # not decode. That copy is a TRANSFORMATION of the file, so it must
            # not become an edit base: saving it would write the replacement
            # characters over bytes that are still perfectly good on disk.
            # Reported to the caller, which refuses editing exactly as it does
            # for a redacted copy.
            lossy = True
            description = ""
        return (
            raw.decode("utf-8", errors="replace"),
            # From the validated bytes, NOT by reopening the path: the gate
            # above pinned an inode, and a second open could land on another.
            description,
            None,
            str(target),
            lossy,
            # Edit base for compare-and-swap: a later PUT presents this hash and
            # the writer refuses when the file no longer matches it. Hashed from
            # the same validated bytes the content copy came from, so the pair
            # cannot disagree about which file state they describe.
            hashlib.sha256(raw).hexdigest(),
        )

    try:
        content, description, err, target_path, lossy, content_hash = (
            await asyncio.get_running_loop().run_in_executor(discovery_executor(), _read)
        )
    except Exception:
        _refuse(op, "error", "read_failed", name=name, scope=scope)
        return web.json_response(
            {"error": "could not read the prompt", "code": "read_failed"}, status=500
        )
    if err == "no_active_project":
        _refuse(op, "bad_request", "no_active_project", name=name, scope=scope)
        return web.json_response(
            {"error": "no active project for local scope", "code": "no_active_project"}, status=400
        )
    if err == "linked_prompt_root":
        _refuse(op, "blocked", "linked_prompt_root", name=name, scope=scope)
        return web.json_response(
            {"error": "prompt directory is a link", "code": "linked_prompt_root"}, status=403
        )
    if err == "not found":
        _refuse(op, "not_found", "prompt_not_found", name=name, scope=scope)
        return web.json_response({"error": "not found", "code": "prompt_not_found"}, status=404)
    if err == "access denied":
        _refuse(op, "blocked", "access_denied", name=name, scope=scope)
        return web.json_response({"error": "access denied", "code": "access_denied"}, status=403)
    if err == "too large":
        _refuse(op, "too_large", "content_too_large", name=name, scope=scope)
        return web.json_response(
            {"error": "file too large", "code": "content_too_large"}, status=413
        )
    body = content or ""
    body, cred_hits = redact_credentials(body)
    body, url_hits = redact_exfiltration_urls(body)
    # Metadata gets the same treatment as the unscoped branch: a description read
    # out of frontmatter is file content too, and can carry a token.
    out = {
        "name": name,
        "fullName": name,
        "package": "",
        "source": scope,
        "description": description,
        "path": target_path.replace(str(Path.home()), "~"),
    }
    _redact_prompt(out)
    _prompt_audit(op, "ok", name=name, scope=scope)
    return web.json_response(
        {
            **out,
            "content": body,
            "redacted": bool(cred_hits or url_hits),
            # Separate from `redacted` so the UI can say WHICH transformation
            # happened: "filtered for safety" and "not valid UTF-8" are different
            # facts about the file, and only one of them implies a credential.
            "lossy": lossy,
            # The edit base a PUT presents back — of the RAW file bytes, since it
            # names the file state the compare-and-swap checks against. Withheld
            # when the copy is redacted or lossy: editing is refused for those, so
            # the hash serves no caller — and for a redacted copy it would be an
            # offline verification oracle for the very content the redaction hides
            # (hash a guess, compare). No edit base for a copy that must not be one.
            "hash": "" if (cred_hits or url_hits or lossy) else content_hash,
        }
    )


async def _api_prompt_write(request: web.Request) -> web.Response:
    """PUT/DELETE /api/prompts/{name}?scope= — update or delete a user prompt.

    Unlike create, the name is validated but NOT rewritten: it must identify an
    existing file, including one whose stem the sanitizer would not have
    produced (e.g. a hand-created ``My_Prompt.md``).
    """
    name = request.match_info["name"]
    scope = request.query.get("scope", "")
    op = "api_prompt_update" if request.method == "PUT" else "api_prompt_delete"
    denied = _deny_non_owner_prompt_write(request, op, name=name, scope=scope)
    if denied is not None:
        return denied
    if scope not in _PROMPT_SCOPES:
        _refuse(op, "bad_request", "bad_scope", name=name, scope=scope)
        return web.json_response(
            {"error": "scope query param must be 'global' or 'local'", "code": "bad_scope"},
            status=400,
        )
    if not _plain_stem_ok(name):
        _refuse(op, "bad_request", "invalid_name", name=name)
        return web.json_response(
            {"error": "invalid prompt name", "code": "invalid_name"}, status=400
        )

    content: str | None = None
    base_hash: str | None = None
    if request.method == "PUT":
        try:
            body = await request.json()
        except Exception:
            body = None
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("content"), str)
            or not body["content"].strip()
        ):
            _refuse(op, "bad_request", "content_required", name=name)
            return web.json_response(
                {"error": "content is required", "code": "content_required"}, status=400
            )
        new_content: str = body["content"]
        encoded = _utf8_or_none(new_content)
        if encoded is None:
            _refuse(op, "bad_request", "content_not_encodable", name=name)
            return web.json_response(
                {"error": "content is not valid UTF-8 text", "code": "content_not_encodable"},
                status=400,
            )
        if len(encoded) > MAX_PROMPT_BYTES:
            _refuse(op, "too_large", "content_too_large", name=name)
            return web.json_response(
                {"error": "content exceeds size limit", "code": "content_too_large"}, status=413
            )
        # An update REQUIRES the edit base it was made against. The scoped GET
        # hands out the file's hash; a PUT that cannot present one was seeded
        # from something other than the file — exactly the copies (stale cache,
        # redacted, lossy) this feature refuses as edit bases everywhere else.
        # The API is new in this change, so requiring it breaks no caller.
        # Checked LAST among the body checks so each earlier refusal keeps its
        # own code: a caller fixing an oversize body should hear "too large",
        # not "missing hash".
        raw_hash = body.get("base_hash")
        if not isinstance(raw_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", raw_hash):
            _refuse(op, "bad_request", "base_hash_required", name=name)
            return web.json_response(
                {
                    "error": "base_hash (sha256 of the content the edit was based on) is required",
                    "code": "base_hash_required",
                },
                status=400,
            )
        base_hash = raw_hash
        content = new_content

    def _apply() -> str | None:
        # Serialized: the compare-and-swap below is check-then-write, and the
        # executor pool runs several jobs at once — two concurrent PUTs could
        # both verify the same base and then write in turn, the exact lost
        # update the CAS exists to refuse. Under the lock the second writer's
        # verify reads the first one's content and answers 409. This closes
        # the race among THIS process's writers, which is every writer that
        # presents a base_hash at all; an external editor writing in the
        # residual window is not stopped by any in-process lock, which is why
        # the window is documented rather than claimed away.
        with _PROMPT_WRITE_LOCK:
            return _apply_locked()

    def _apply_locked() -> str | None:
        target_dir, derr = _resolve_prompt_dir(scope)
        if target_dir is None:
            return derr
        target = target_dir / f"{name}.md"
        # Confinement: the file must LIVE in the scope directory, not merely be
        # named under it — a symlinked entry resolving elsewhere is refused,
        # not written through. The link check runs FIRST and never follows:
        # ``is_file()`` follows, so checking it first would answer 404 for a
        # dangling link and 403 for a live one — an existence oracle for the
        # link's target. Every link gets the same refusal, dangling or not.
        try:
            if is_link_or_junction(target):
                return "access denied"
        except OSError:
            return "access denied"
        if not target.is_file():
            return "not found"
        if target.resolve().parent != target_dir.resolve():
            return "access denied"
        if content is None:
            if not _DIR_FD_SUPPORTED:
                try:
                    target.unlink()
                except FileNotFoundError:
                    # Raced away between the confinement check and the unlink.
                    # Same contract as the dir_fd branch below: a coded 404,
                    # not an uncaught error surfacing as write_failed (500).
                    return "not found"
            else:
                # Removed relative to a descriptor pinning the validated
                # directory, so swapping the root (or an ancestor) for a link
                # after the confinement check cannot redirect the unlink to a
                # file outside the tree the caller named. ``unlinkat`` never
                # follows a symlink at the final component, so a linked entry
                # still loses only the link — the property the confinement
                # check above already refuses to reach.
                try:
                    dir_fd = _pin_prompt_dir(target_dir)
                except FileNotFoundError:
                    # The directory went away under us; the file the caller named
                    # is gone with it, which is the 404 the by-name check would
                    # have answered a moment earlier.
                    return "not found"
                except pinned_fs.PinnedPathRefusal:
                    return "linked_prompt_root"
                try:
                    os.unlink(f"{name}.md", dir_fd=dir_fd)
                except FileNotFoundError:
                    return "not found"
                finally:
                    os.close(dir_fd)
        else:
            # Compare-and-swap through ONE opened descriptor: the primitive
            # verifies ``base_hash`` against the bytes of the very inode whose
            # replacement it stages, so verification and replacement share a
            # single name resolution — there is no by-name re-read between them
            # for a swapped ancestor or leaf to redirect. A concurrency change
            # detected after verification (an atomic external save swapping the
            # inode, or an in-place rewrite visible through mtime/size) answers
            # conflict rather than overwriting: the newer file wins, never the
            # stale edit. The primitive also carries the original's mode and
            # access-control xattrs onto the replacement (refusing when an
            # access-control attribute cannot be carried) and opens without
            # ``O_CREAT`` — a prompt that vanished must not be recreated here.
            # The process-wide write lock above serializes the dashboard's own
            # writers; the descriptor anchoring is what narrows external ones.
            # ``base_hash`` is non-None by construction on this branch: a PUT
            # with content and no well-formed base_hash was refused with a
            # coded 400 before the executor was entered.
            assert base_hash is not None
            outcome = verified_replace_file_nolink(
                str(target),
                content,
                base_hash,
                within_root=str(target_dir),
                max_bytes=MAX_PROMPT_BYTES,
            )
            if outcome in ("conflict", "too_large"):
                # too_large means the file outgrew the cap since the edit base
                # was read — by definition not the state the edit was based on.
                return "conflict"
            if outcome != "ok":
                return "write refused"
        return None

    try:
        err = await asyncio.get_running_loop().run_in_executor(discovery_executor(), _apply)
    except Exception:
        _refuse(op, "error", "write_failed", name=name, scope=scope)
        return web.json_response(
            {"error": "could not write the prompt", "code": "write_failed"}, status=500
        )
    if err == "no_active_project":
        _refuse(op, "bad_request", "no_active_project", name=name, scope=scope)
        return web.json_response(
            {"error": "no active project for local scope", "code": "no_active_project"}, status=400
        )
    if err == "linked_prompt_root":
        _refuse(op, "blocked", "linked_prompt_root", name=name, scope=scope)
        return web.json_response(
            {"error": "prompt directory is a link", "code": "linked_prompt_root"}, status=403
        )
    if err == "not found":
        _refuse(op, "not_found", "prompt_not_found", name=name, scope=scope)
        return web.json_response({"error": "not found", "code": "prompt_not_found"}, status=404)
    if err == "access denied":
        _refuse(op, "blocked", "access_denied", name=name, scope=scope)
        return web.json_response({"error": "access denied", "code": "access_denied"}, status=403)
    if err == "conflict":
        _refuse(op, "conflict", "content_conflict", name=name, scope=scope)
        return web.json_response(
            {
                "error": "the prompt changed on disk after the edit was started",
                "code": "content_conflict",
            },
            status=409,
        )
    if err == "write refused":
        # The writer failed closed — a hardlink, a non-regular file, an escape
        # from the scope dir, or access-control metadata it could not carry.
        # Answered HERE, in the write dispatch, because this is the only place
        # that produces it: an unhandled value falls through to the success
        # response, which tells the caller their edit was saved while the file
        # on disk still holds the original.
        _refuse(op, "blocked", "write_refused", name=name, scope=scope)
        return web.json_response(
            {"error": "could not write the prompt safely", "code": "write_refused"}, status=403
        )
    _invalidate_prompt_cache()
    _prompt_audit(op, "ok", name=name, scope=scope)
    if content is not None:
        # The edit base for the NEXT save without a fresh GET: hashed from the
        # exact bytes this handler validated and wrote, so a client that saves
        # twice in a row presents the state it actually created.
        encoded_now = content.encode("utf-8")
        return web.json_response({"ok": True, "hash": hashlib.sha256(encoded_now).hexdigest()})
    return web.json_response({"ok": True})


# ── Skills ──


# Open-standard skill-key territories whose READ path (_resolve_skill_root in
# _shared.py) resolves per-session / per-machine — ``kiro-user/`` against
# ``~/.kiro/skills`` and ``kiro-workspace/`` against ``<project>/.kiro/skills`` —
# while the WRITE handlers (skills.create/update/delete_skill) join the key onto
# a core root. That means the same key names a DIFFERENT file on write than the
# reader was shown (issue #8244). These prefixes are documented read-only in
# api_skills, so the write path refuses them rather than silently writing the
# core-root copy. The literals must match the prefixes _resolve_skill_root and
# _skill_key_roots use so read and write agree on territory. The ``package/``
# prefix is intentionally NOT listed here — that territory is handled separately
# by PR #7105; this guard is its untracked kiro-user/ and kiro-workspace/ sibling.
READONLY_SKILL_KEY_PREFIXES = ("kiro-user/", "kiro-workspace/")


async def api_skills(request: web.Request) -> web.Response:
    """GET /api/skills — list skills from all known sources.

    Sources:
    - ``kirocrew``: ``~/.kiro/crew/skills/`` (managed by SkillsLoader; editable)
    - ``package``: skills an edition contributes, if any (read-only here)
    - ``kiro-user``: ``~/.kiro/skills/`` (open-standard; read-only here)
    - ``kiro-workspace``: ``<project>/.kiro/skills/`` (open-standard; read-only here)

    Each entry carries ``loaded_by_agents`` — the names of installed agents
    whose ``resources`` would load the skill via a ``skill://`` URI. Empty
    list means no agent loads it via the kiro-cli native loader (it may
    still be loaded via KiroCrew text-injection or an external MCP server).

    ``?agent=<name>`` scopes the listing to that agent's own ``skill://``
    mapping (matching the same globs the prompt-injection path resolves via
    :func:`agent_skill_globs`) — filtered to skills in that agent's
    ``loaded_by_agents``. An agent with no explicit skill:// resources of its
    own (``agent_skill_globs`` returns ``[]``) keeps the unfiltered, legacy
    all-or-nothing listing: an agent that never customized its skill set
    must not lose access to skills a customized agent's presence would
    otherwise imply are opt-in.

    When the agent filter is actually applied (agent given AND its globs are
    non-empty), the response is the envelope
    ``{"skills": [...], "agent_scoped": true, "agent": <name>}`` instead of
    the bare array. The flag is required wiring, not decoration: a filtered
    list — especially an EMPTY one — is byte-identical to the legacy
    unfiltered array, so without it the client cannot tell "no skills are
    mapped to this agent" apart from "no skills exist at all". Every
    unscoped path keeps the bare-array shape unchanged.
    """
    state: DashboardState = request.app["state"]
    session_key = _read_session_key(request)
    denied = _deny_foreign_app_skill_slot(request, state, session_key, "skills_list")
    if denied is not None:
        return denied
    skills = _get_skills(state)
    # Resolve the active project dir (cheap in-memory scan of slots) on the loop.
    # Scoped to the requesting chat slot: without the key, two chats on
    # different projects made this fall to None and kiro-workspace skills
    # silently vanished from the listing (#2457).
    # Strict: must match what SkillsLoader will resolve for THIS chat, or the
    # catalog advertises a skill whose $token expands to nothing.
    project_dir: Path | None = requesting_slot_project(state, session_key)
    # Run the edition capability lookup async (on the loop, non-blocking), then offload ALL
    # blocking filesystem work — kirocrew list_skills() (os.walk + per-file
    # frontmatter reads), package path globs, kiro per-skill resolve/read, and the
    # agent annotation — onto the dedicated DISCOVERY pool in one job. This work
    # would stall the event loop past the loop-stall watchdog (~25s) on large
    # skills×agents catalogs if run on-loop. Use the discovery pool
    # (NOT maintenance_executor): this scan is browser-triggerable and can be
    # seconds-long, so the maintenance pool would let a few dashboard tabs
    # occupy the workers the orphan-reaper sweeps need to recover from a wedge
    # (see kiro_crew.executors). No result cache: the endpoint always reflects
    # current on-disk state, so freshly created/installed skills appear
    # immediately (correctness over the latency a cache would add).
    mgr = _capability_manager()
    try:
        package_skills = await mgr.list_skills() if mgr.available() else []
    except Exception:
        # The capability manager is one of three skill sources; degrade to "no
        # package skills" rather than 500 the whole /api/skills endpoint.
        package_skills = []
    result = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(),
        collect_skills_blocking,
        skills,
        package_skills,
        project_dir,
    )
    agent = request.query.get("agent") or None
    if agent:
        globs = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), agent_skill_globs, agent
        )
        if globs:
            result = [s for s in result if agent in (s.get("loaded_by_agents") or [])]
            return web.json_response({"skills": result, "agent_scoped": True, "agent": agent})
    return web.json_response(result)


def _grant_reviewed_project(project_dir: Path, expected: object, *, session_key: str) -> str:
    """Snapshot *project_dir*, confirm its reviewed canonical key, then grant.

    Runs on the discovery executor: `canonical_key` realpaths the slot path, and
    `grant_project_trust` takes a lock and writes. Neither belongs on the event
    loop.

    *expected* is the canonical key returned by the trust snapshot, never a
    selector. The current slot path is canonicalized once, then compared to that
    opaque string before the same key is persisted. Client text is never resolved,
    so a supplied UNC path cannot initiate outbound authentication. Missing and
    mismatched keys both refuse: consent without the reviewed identity is blind.
    """
    return grant_project_trust(
        project_dir,
        expected_key=expected,
        session_key=session_key,
    )


def _trust_snapshot(project_dir: Path | None) -> dict[str, Any]:
    """Blocking read of trust state for *project_dir* plus every stored grant."""
    project_key = canonical_key(project_dir) if project_dir else None
    return {
        "project": str(project_dir) if project_dir else "",
        "project_key": project_key or "",
        "trusted": is_key_trusted(project_key),
        "grants": list_trusted_projects(),
    }


async def api_skills_trust(request: web.Request) -> web.Response:
    """Report the requesting chat's project-skills trust state and all grants."""
    denied = _deny_non_owner_skill_trust(request, "skill_trust_read")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    # Strict: must match what SkillsLoader will resolve for THIS chat, or the
    # catalog advertises a skill whose $token expands to nothing.
    project_dir: Path | None = requesting_slot_project(state, _read_session_key(request))
    snapshot = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _trust_snapshot, project_dir
    )
    return web.json_response(snapshot)


async def api_skills_trust_grant(request: web.Request) -> web.Response:
    """Grant project-skills trust to the REQUESTING CHAT's own project.

    The directory is taken from the requesting slot, never from the request
    body: a caller-supplied path would let anything that can reach this
    endpoint consent on the operator's behalf for a directory they never
    opened. The operator can only trust the project they actually have open.

    The body carries ``expected_key`` — the canonical identity returned with the
    consent dialog's snapshot. It is a required confirmation, never a selector:
    the directory still comes from the slot, and a missing or mismatched key is
    refused. This covers slot changes and mutable aliases between review and click.
    """
    denied = _deny_non_owner_skill_trust(request, "skill_trust_grant")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    session_key = _read_session_key(request)
    # Strict: consent is recorded for the directory THIS chat is bound to.
    # The shared fallback would grant trust to another chat's project.
    project_dir: Path | None = requesting_slot_project(state, session_key)
    if project_dir is None:
        return web.json_response(
            {
                "error": "no project is set for this chat, so there is no directory to trust",
                "code": "skill_trust_no_project",
                "reason": "no_project",
            },
            status=400,
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed/missing confirmation is refused below
        body = {}
    expected = (body or {}).get("expected_key") if isinstance(body, dict) else None
    loop = asyncio.get_running_loop()
    try:
        # Confirmation AND grant in one offloaded call. Both halves touch the
        # filesystem (canonical_key does realpath + isdir, i.e. one lstat per path
        # component), and this handler offloads every other filesystem step -- doing
        # it inline stalled the event loop. Combining them also removes the window
        # a second await would open between confirming a directory and recording
        # consent for it, so what was reviewed is what gets written.
        await loop.run_in_executor(
            discovery_executor(),
            functools.partial(
                _grant_reviewed_project,
                project_dir,
                expected,
                session_key=session_key,
            ),
        )
    except _ReviewedProjectChanged:
        return web.json_response(
            {
                "error": (
                    "this chat's project is no longer the directory shown for "
                    "review, so consent was not recorded"
                ),
                "code": "skill_trust_project_changed",
                "reviewed": str(expected),
                "current": str(project_dir),
            },
            status=409,
        )
    except ValueError as exc:
        return web.json_response(
            {"error": str(exc), "code": "skill_trust_unusable_project"}, status=400
        )
    except TrustStoreFull as exc:
        return web.json_response({"error": str(exc), "code": "skill_trust_store_full"}, status=409)
    except TrustStoreUnreadable as exc:
        # Refusing beats overwriting: the store may hold grants this build
        # cannot read, and appending to an empty list would destroy them.
        return web.json_response(
            {"error": str(exc), "code": "skill_trust_store_unreadable"}, status=409
        )
    snapshot = await loop.run_in_executor(discovery_executor(), _trust_snapshot, project_dir)
    return web.json_response(snapshot)


async def api_skills_trust_revoke(request: web.Request) -> web.Response:
    """Withdraw a project-skills trust grant.

    Unlike granting, this accepts an explicit ``path`` so the operator can
    revoke a grant for a directory they no longer have open (or have deleted)
    from the settings list. Removing trust only ever narrows what loads, so a
    caller-supplied path is safe here.
    """
    denied = _deny_non_owner_skill_trust(request, "skill_trust_revoke")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    session_key = _read_session_key(request)
    target = request.query.get("path", "").strip()
    if not target:
        project_dir = active_project_dir(state, session_key)
        if project_dir is None:
            return web.json_response(
                {
                    "error": "no path given and no project is set for this chat",
                    "code": "skill_trust_no_target",
                },
                status=400,
            )
        target = str(project_dir)
    loop = asyncio.get_running_loop()
    try:
        removed = await loop.run_in_executor(
            discovery_executor(),
            functools.partial(revoke_project_trust, target, session_key=session_key),
        )
    except TrustStoreUnreadable as exc:
        # A revoke rewrites the survivors, so an unreadable store would lose the
        # grants it could not read -- refuse rather than narrow destructively.
        return web.json_response(
            {"error": str(exc), "code": "skill_trust_store_unreadable"}, status=409
        )
    project_dir = active_project_dir(state, session_key)
    snapshot = await loop.run_in_executor(discovery_executor(), _trust_snapshot, project_dir)
    snapshot["removed"] = removed
    return web.json_response(snapshot)


async def api_skill_tree(request: web.Request) -> web.Response:
    """GET /api/skills/{name}/tree — list files within a skill folder.

    Capped at SKILL_TREE_MAX_ENTRIES; sensitive paths and symlinks
    escaping the skill root are omitted.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["name"]
    session_key = _read_session_key(request)
    if name.startswith("kiro-workspace/"):
        denied = _deny_foreign_app_skill_slot(request, state, session_key, "skill_tree")
        if denied is not None:
            return denied

    def _resolve_and_list() -> tuple["Path | None", list]:
        # Resolve (stat/realpath) and the tree walk are one filesystem
        # transaction; both run on the discovery pool so a network-backed
        # project cannot stall the event loop.
        r = _resolve_skill_root(name, state, session_key)
        return (r, list_skill_tree(r) if r is not None else [])

    root, entries = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _resolve_and_list
    )
    if root is None:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_tree",
            tool_kind="skill",
            outcome="not_found",
            metadata={"name": name},
        )
        return web.json_response({"error": "not found"}, status=404)
    # Sanitize the absolute path — never expose the server's real home to the
    # client.  ``root`` is already resolved (symlinks followed), so compare
    # against the *resolved* home too; otherwise a symlinked home (e.g. macOS
    # ``/var`` → ``/private/var``) would mismatch and leak the real path.
    display_root = str(root)
    for home in {str(Path.home()), str(Path.home().resolve())}:
        display_root = display_root.replace(home, "~")
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_tree",
        tool_kind="skill",
        outcome="ok",
        metadata={"name": name, "root": display_root, "count": len(entries)},
    )
    return web.json_response({"name": name, "root": display_root, "entries": entries})


async def api_skill_file(request: web.Request) -> web.Response:
    """GET /api/skills/{name}/file?path=<rel> — read a single file inside a skill folder.

    Capped at SKILL_FILE_MAX_BYTES.  Returns 400 on path-escape attempts,
    403 on sensitive paths, 413 when over the size cap, 404 otherwise.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["name"]
    rel_path = request.query.get("path", "")
    session_key = _read_session_key(request)
    if name.startswith("kiro-workspace/"):
        denied = _deny_foreign_app_skill_slot(request, state, session_key, "skill_file")
        if denied is not None:
            return denied

    def _audit(outcome: str) -> None:
        # Audit every access — including failed ones (traversal rejections,
        # sensitive-path blocks), which can indicate filesystem probing.
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_file",
            tool_kind="skill",
            outcome=outcome,
            metadata={"name": name, "path": rel_path},
        )

    if not rel_path:
        _audit("bad_request")
        return web.json_response({"error": "path query param required"}, status=400)

    def _resolve_and_read() -> tuple["Path | None", str | None, str | None]:
        # One filesystem transaction on the discovery pool (see api_skill_tree).
        r = _resolve_skill_root(name, state, session_key)
        if r is None:
            return None, None, None
        c, e = read_skill_file(r, rel_path)
        return r, c, e

    root, content, err = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _resolve_and_read
    )
    if root is None:
        _audit("not_found")
        return web.json_response({"error": "not found"}, status=404)
    if err:
        if err == "access denied":
            _audit("blocked")
            return web.json_response({"error": err}, status=403)
        if err.startswith("file too large"):
            _audit("too_large")
            return web.json_response({"error": err}, status=413)
        if err == "invalid path":
            _audit("blocked")
            return web.json_response({"error": err}, status=400)
        _audit("not_found")
        return web.json_response({"error": err}, status=404)
    _audit("ok")
    return web.json_response({"name": name, "path": rel_path, "content": content})


# ── Auto-skill pending-approval queue (v2) ──


async def api_skills_pending(request: web.Request) -> web.Response:
    """GET /api/skills/-/pending — list staged auto-skill candidates."""
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)

    def _prune_and_list() -> list:
        # Opportunistic TTL cleanup on read — gives prune_pending a real caller
        # so stale candidates don't accumulate unbounded.
        try:
            ttl = KiroCrewConfig.load().skills.pending_ttl_days
            skills.prune_pending(ttl)
        except Exception:
            pass
        return skills.list_pending_skills()

    try:
        items = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _prune_and_list
        )
    except Exception:
        items = []
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skills_pending",
        tool_kind="skill",
        outcome="ok",
        metadata={"count": len(items)},
    )
    return web.json_response({"pending": items})


async def api_skill_pending_detail(request: web.Request) -> web.Response:
    """GET /api/skills/-/pending/{slug} — full candidate incl. body + scripts."""
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    slug = request.match_info["slug"]
    if not _plain_stem_ok(slug):
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_detail",
            tool_kind="skill",
            outcome="bad_request",
            metadata={"slug": slug},
        )
        return web.json_response({"error": "invalid slug"}, status=400)
    try:
        detail = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), skills.get_pending_skill, slug
        )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_detail",
            tool_kind="skill",
            outcome="error",
            metadata={"slug": slug},
        )
        return web.json_response({"error": "internal error"}, status=500)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_pending_detail",
        tool_kind="skill",
        outcome="ok" if detail is not None else "not_found",
        metadata={"slug": slug},
    )
    if detail is None:
        return web.json_response({"error": "not found"}, status=404)
    # Update candidates carry an approval PREVIEW so the UI can show exactly what
    # approving would change: the target's current live body, the proposed
    # post-approval content, and a unified diff between them (computed
    # server-side with difflib so the frontend needs no diff dependency).
    # kind/target may be exposed at the top level or nested under ``meta`` — read
    # defensively. All preview fields are null if the target skill was removed
    # since the candidate was staged.
    _meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
    kind = detail.get("kind") or _meta.get("kind")
    if kind == "update":

        def _preview() -> dict | None:
            try:
                return skills.preview_pending_update(slug)
            except Exception:
                return None

        try:
            pv = await asyncio.get_running_loop().run_in_executor(discovery_executor(), _preview)
        except Exception:
            pv = None
        detail["live_body"] = (pv or {}).get("live_body")
        detail["proposed_body"] = (pv or {}).get("proposed_body")
        detail["diff"] = (pv or {}).get("diff")
        detail["from_version"] = (pv or {}).get("from_version")
        detail["to_version"] = (pv or {}).get("to_version")
        detail["stale_base"] = bool((pv or {}).get("stale_base"))
    return web.json_response(detail)


async def api_skill_pending_approve(request: web.Request) -> web.Response:
    """POST /api/skills/-/pending/{slug}/approve — promote candidate to live."""
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    slug = request.match_info["slug"]
    if not _plain_stem_ok(slug):
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_approve",
            tool_kind="skill",
            outcome="rejected",
            metadata={"slug": slug, "reason": "invalid_slug"},
        )
        return web.json_response({"error": "invalid slug"}, status=400)

    def _approve_and_bound() -> str | None:
        # Route on candidate kind: an UPDATE candidate rewrites an existing live
        # skill (approve_pending_update); a NEW candidate is promoted fresh
        # (approve_pending_skill). kind is read from the candidate detail
        # (top-level or nested ``meta``), defaulting to the new path.
        kind = None
        try:
            _detail = skills.get_pending_skill(slug)
        except Exception:
            _detail = None
        if isinstance(_detail, dict):
            _meta_raw = _detail.get("meta")
            _meta: dict = _meta_raw if isinstance(_meta_raw, dict) else {}
            kind = _detail.get("kind") or _meta.get("kind")
        if kind == "update":
            nm = skills.approve_pending_update(slug)
        else:
            nm = skills.approve_pending_skill(slug)
        if nm:
            # Approving consumes a slot — enforce the bound (archive, never
            # delete). Best-effort; runs in the same off-loop executor job.
            # Exempt the just-approved skill so a full-cap pass can't archive the
            # very skill this request promoted (brand-new + zero-hit, it would
            # otherwise rank lowest in the max-N backstop).
            try:
                cfg = KiroCrewConfig.load().skills
                skills.run_skill_lifecycle(
                    max_auto_skills=cfg.max_auto_skills,
                    stale_after_days=cfg.stale_after_days,
                    archive_after_days=cfg.archive_after_days,
                    exempt={nm},
                )
            except Exception:
                pass
        return nm

    try:
        name = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _approve_and_bound
        )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_approve",
            tool_kind="skill",
            outcome="error",
            metadata={"slug": slug},
        )
        return web.json_response({"error": "internal error"}, status=500)
    outcome = "ok" if name else "not_found"
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_pending_approve",
        tool_kind="skill",
        outcome=outcome,
        metadata={"slug": slug, "name": name or ""},
    )
    if not name:
        return web.json_response(
            {"error": "not found, a live skill already exists, or script validation failed"},
            status=409,
        )
    return web.json_response({"approved": name})


async def api_skill_pending_dismiss(request: web.Request) -> web.Response:
    """POST /api/skills/-/pending/{slug}/dismiss — delete a candidate."""
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    slug = request.match_info["slug"]
    if not _plain_stem_ok(slug):
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_dismiss",
            tool_kind="skill",
            outcome="rejected",
            metadata={"slug": slug, "reason": "invalid_slug"},
        )
        return web.json_response({"error": "invalid slug"}, status=400)
    try:
        ok = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), skills.dismiss_pending_skill, slug
        )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_dismiss",
            tool_kind="skill",
            outcome="error",
            metadata={"slug": slug},
        )
        return web.json_response({"error": "internal error"}, status=500)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_pending_dismiss",
        tool_kind="skill",
        outcome="ok" if ok else "not_found",
        metadata={"slug": slug},
    )
    if not ok:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"dismissed": slug})


async def api_skills_pending_dismiss_all(request: web.Request) -> web.Response:
    """POST /api/skills/-/pending/-/dismiss-all — dismiss pending candidates.

    Accepts an optional JSON body ``{"slugs": ["slug1", ...]}``.  When present,
    only those slugs are dismissed (the client passes the set it displayed to the
    user, so a candidate staged *after* the confirmation dialog is never silently
    deleted).  When the body is absent or ``slugs`` is empty, ALL pending
    candidates are dismissed (back-compat / fallback).
    """
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    raw_slugs = body.get("slugs")
    if raw_slugs is not None and (
        not isinstance(raw_slugs, list) or not all(isinstance(s, str) for s in raw_slugs)
    ):
        return web.json_response(
            {"error": "slugs must be an array of strings", "code": "invalid_slugs"}, status=400
        )
    slugs: list[str] = raw_slugs if isinstance(raw_slugs, list) else []
    try:
        if slugs:
            count = await asyncio.get_running_loop().run_in_executor(
                discovery_executor(),
                lambda: skills.dismiss_pending_slugs(slugs),
            )
        else:
            return web.json_response(
                {
                    "error": "slugs array is required and must not be empty",
                    "code": "slugs_required",
                },
                status=400,
            )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skills_pending_dismiss_all",
            tool_kind="skill",
            outcome="error",
            metadata={},
        )
        return web.json_response({"error": "internal error", "code": "internal_error"}, status=500)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skills_pending_dismiss_all",
        tool_kind="skill",
        outcome="ok",
        metadata={"count": count},
    )
    return web.json_response({"dismissed_count": count})


async def api_skill_pin(request: web.Request) -> web.Response:
    """POST /api/skills/-/pin — body {name, pinned:bool}. Pin/unpin an auto-skill
    so the lifecycle never archives it."""
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str(body.get("name", "")).strip()
    raw_pinned = body.get("pinned", True)
    if not isinstance(raw_pinned, bool):
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pin",
            tool_kind="skill",
            outcome="rejected",
            metadata={"name": name, "reason": "pinned_not_bool"},
        )
        return web.json_response({"error": "pinned must be a boolean"}, status=400)
    pinned = raw_pinned
    if not name:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pin",
            tool_kind="skill",
            outcome="rejected",
            metadata={"name": name, "reason": "name_required"},
        )
        return web.json_response({"error": "name required"}, status=400)
    try:
        ok = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), skills.set_pinned, name, pinned
        )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pin",
            tool_kind="skill",
            outcome="error",
            metadata={"name": name, "pinned": pinned},
        )
        return web.json_response({"error": "internal error"}, status=500)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_pin",
        tool_kind="skill",
        outcome="ok" if ok else "rejected",
        metadata={"name": name, "pinned": pinned},
    )
    if not ok:
        return web.json_response({"error": "not an auto-skill or not found"}, status=400)
    return web.json_response({"name": name, "pinned": pinned})


async def api_skill_inject_on_trigger(request: web.Request) -> web.Response:
    """POST /api/skills/-/inject-on-trigger — body {name, inject:bool}.

    Opt a skill in or out of full-body injection when its triggers match. The
    edit is a targeted frontmatter line change performed server-side, not a
    round-trip through the skill editor: rebuilding the file from the structured
    form would be a wider write than this needs.

    Every outcome is audited, including the rejections. Turning ``inject`` off
    changes what the agent is guaranteed to see when the skill matches, so "who
    made this skill advisory, and when" has to be answerable.
    """
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    try:
        body = await request.json()
    except Exception:
        body = {}
    # `request.json()` yields whatever the body parsed to, and `[]` / `"x"` / `7`
    # are all valid JSON. Normalize any non-object to an empty one so validation
    # answers with a 400 and a code instead of AttributeError -> 500.
    if not isinstance(body, dict):
        body = {}
    name = str(body.get("name", "")).strip()
    raw_inject = body.get("inject")
    if not isinstance(raw_inject, bool):
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_inject_on_trigger",
            tool_kind="skill",
            outcome="rejected",
            metadata={"name": name, "reason": "inject_not_bool"},
        )
        return web.json_response(
            {"error": "inject must be a boolean", "code": "inject_not_bool"}, status=400
        )
    inject = raw_inject
    if not name:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_inject_on_trigger",
            tool_kind="skill",
            outcome="rejected",
            metadata={"name": name, "reason": "name_required"},
        )
        return web.json_response({"error": "name required", "code": "name_required"}, status=400)
    try:
        ok = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), skills.set_inject_on_trigger, name, inject
        )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_inject_on_trigger",
            tool_kind="skill",
            outcome="error",
            metadata={"name": name, "inject": inject},
        )
        return web.json_response({"error": "internal error", "code": "internal_error"}, status=500)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_inject_on_trigger",
        tool_kind="skill",
        outcome="ok" if ok else "rejected",
        metadata={"name": name, "inject": inject},
    )
    if not ok:
        return web.json_response(
            {"error": "not found or has no frontmatter", "code": "skill_not_editable"},
            status=400,
        )
    return web.json_response({"name": name, "inject_on_trigger": inject})


def _match_package_row(
    rows: list[dict[str, Any]], name: str, pkg_name: str
) -> dict[str, Any] | None:
    """Pick the capability row a ``package/<...>`` skill key refers to.

    ``key`` is the exact identifier the row was listed under, so it decides
    first. Matching on ``name`` is a LEAF comparison and is only a fallback for
    an edition that keys its rows some other way — two skills can share a leaf
    under different parents (``package/shared-skill`` and ``package/SomePkg/shared-skill``),
    and picking the first leaf match would serve the wrong SKILL.md while looking
    entirely successful.

    So the leaf fallback is used only when it is UNAMBIGUOUS. An ambiguous leaf
    returns ``None`` (the caller 404s) and logs, because a reader who opened one
    skill and silently got another has no way to notice.
    """
    for row in rows:
        if row.get("key") == name:
            return row
    leaf_matches = [row for row in rows if row.get("name") == pkg_name]
    if len(leaf_matches) == 1:
        return leaf_matches[0]
    if leaf_matches:
        logger.warning(
            "skill key %r matches no row key and %d rows by leaf name (%s); "
            "refusing to guess which SKILL.md was meant",
            name,
            len(leaf_matches),
            ", ".join(sorted(str(r.get("key")) for r in leaf_matches)),
        )
    return None


async def api_skill_detail(request: web.Request) -> web.Response:
    """GET/PUT/DELETE /api/skills/{name} — get, update, or delete a skill."""
    state: DashboardState = request.app["state"]
    name = request.match_info["name"]
    skills = _get_skills(state)

    # Refuse mutating verbs on the open-standard read-only territories. Their
    # READ path resolves per-session / per-machine (project or ~/.kiro/skills),
    # but update_skill/delete_skill would join the key onto a core root — so the
    # write lands in a different file than the reader was shown (issue #8244).
    # Guarding here, before the PUT/DELETE branches and any session-key work,
    # ensures the mutating verb never reaches skills.*; GET is untouched and keeps
    # resolving via _resolve_skill_root. Same shape #7105 applies to package/.
    if request.method in ("PUT", "DELETE") and name.startswith(READONLY_SKILL_KEY_PREFIXES):
        return web.json_response(
            {
                "error": (
                    f"skill '{name}' is in a read-only territory "
                    "(kiro-user/ and kiro-workspace/ skills are managed on disk, "
                    "not through this endpoint)"
                ),
                "code": "readonly_skill_prefix",
            },
            status=405,
            headers={"Allow": "GET"},
        )

    if request.method == "DELETE":
        # Off the loop: delete_skill walks a pinned parent chain and then rmtrees
        # the skill directory, and update_skill below stages a temp file, carries
        # the ACL and renames it into place. On network-backed storage either can
        # stall long enough to matter to every other session sharing this loop, so
        # both go to a thread the way discover.py already routes the same two calls.
        ok = await asyncio.to_thread(skills.delete_skill, name)
        if not ok:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"ok": True})

    if request.method == "PUT":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        content = body.get("content", "")
        if not content:
            return web.json_response({"error": "content is required"}, status=400)
        ok = await asyncio.to_thread(skills.update_skill, name, content)
        if not ok:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"ok": True})

    # GET
    if name.startswith("kiro-workspace/"):
        session_key = _read_session_key(request)
        denied = _deny_foreign_app_skill_slot(request, state, session_key, "skill_detail")
        if denied is not None:
            return denied
    content = skills.load_skill(name)
    if content is None and name.startswith("package/"):
        pkg_name = name[len("package/") :]  # strip "package/" prefix
        # The capability manager owns skill listing + path resolution; it
        # returns structured rows (no core text parsing / event-loop globbing).
        mgr = _capability_manager()
        try:
            package_skills = await mgr.list_skills() if mgr.available() else []
        except Exception:
            package_skills = []
        row = _match_package_row(package_skills, name, pkg_name)
        if row is not None and row.get("path"):
            resolved = validate_file_path(str(row["path"]))
            if resolved is None:
                return web.json_response({"error": "access denied"}, status=403)
            # Same descriptor gate as the prompt reads above, for the same reason:
            # canonicalizing a path and then opening that name is two resolutions,
            # and a hardlink needs neither a race nor a link to defeat the first
            # one — it shares its target's inode, so ``realpath`` yields the
            # alias's own name and ``is_sensitive_path`` judges that instead of the
            # file whose bytes come back. The gate opens once with ``O_NOFOLLOW``
            # and refuses ``st_nlink > 1``, a non-regular inode, or an opened
            # descriptor whose real path left the canonical parent (the last of
            # which is what still holds on Windows, where ``O_NOFOLLOW`` does not
            # exist). ``row["path"]`` comes from the capability seam rather than a
            # cloned checkout, so the actor here needs write access to a package
            # skill root — a weaker requirement than the prompt sites, but the same
            # defect and the same fix.
            #
            # A refusal leaves ``content`` unset, which is the 404 an unreadable
            # skill already produced, so nothing is distinguishable from I/O
            # trouble — but it is SEL-recorded, because a 404 is also what a name
            # nobody installed produces and a refusal would otherwise be silent.
            # ``FileTooLargeError`` is not an ``OSError`` and would otherwise
            # escape as an unaudited 500; it is the one refusal whose cause IS
            # knowable, so it is recorded as itself.
            #
            # Off the loop, like the delete and update verbs above: no
            # caller-supplied cap applies here, so the gate reads up to its own
            # 50 MB default from storage that can be network-backed, and this
            # handler shares one event loop with every other session's turn.
            skill_bytes: bytes | None
            try:
                skill_bytes = await asyncio.to_thread(
                    safe_read_file_bytes_nolink, resolved, within_root=os.path.dirname(resolved)
                )
                refusal = "error"
            except FileTooLargeError:
                skill_bytes = None
                refusal = "too_large"
            if skill_bytes is not None:
                content = skill_bytes.decode("utf-8", errors="replace")
            else:
                _audit_unread(
                    "api_skill_detail", "skill", refusal, {"name": name, "path": str(row["path"])}
                )
    if content is None and (name.startswith("kiro-user/") or name.startswith("kiro-workspace/")):
        # Open-standard kiro-cli skills are read-only here — load via the
        # same path-resolution logic used by the tree/file endpoints so the
        # detail modal can fetch SKILL.md regardless of which root the
        # skill lives in.
        session_key = _read_session_key(request)

        def _resolve_and_read_md() -> str | None:
            # One filesystem transaction on the discovery pool (see api_skill_tree).
            r = _resolve_skill_root(name, state, session_key)
            if r is None:
                return None
            c, e = read_skill_file(r, "SKILL.md")
            return c if e is None else None

        content_value = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _resolve_and_read_md
        )
        if content_value is not None:
            content = content_value
    if content is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"name": name, "content": content})


async def api_skills_create(request: web.Request) -> web.Response:
    """POST /api/skills — create a new skill."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    name = body.get("name", "").strip()
    content = body.get("content", "").strip()
    if not name:
        return web.json_response({"error": "name is required", "code": "name_required"}, status=400)
    if not content:
        return web.json_response(
            {"error": "content is required", "code": "content_required"}, status=400
        )
    # Sanitize name: lowercase, alphanumeric + hyphens + slashes for nesting
    safe_name = re.sub(r"[^a-z0-9\-/]", "-", name.lower()).strip("-").strip("/")
    safe_name = re.sub(r"/+", "/", safe_name)  # collapse multiple slashes
    if not safe_name:
        return web.json_response(
            {"error": "invalid skill name", "code": "invalid_name"}, status=400
        )
    # Refuse creating into the open-standard read-only territories. Checked on
    # the SANITISED name because that is what create_skill would write (e.g.
    # 'Kiro-Workspace/Foo' sanitises to 'kiro-workspace/foo'). create_skill joins
    # the key onto a core root, but the reader is served kiro-user/ and
    # kiro-workspace/ skills from a session/machine-scoped location — so a create
    # here would write to a different file than the reader is shown (issue #8244).
    if safe_name.startswith(READONLY_SKILL_KEY_PREFIXES):
        return web.json_response(
            {
                "error": (
                    f"skill name '{safe_name}' is in a reserved read-only territory "
                    "(kiro-user/ and kiro-workspace/ skills are managed on disk)"
                ),
                "code": "reserved_skill_prefix",
            },
            status=400,
        )
    skills = _get_skills(state)
    # Off the loop for the same reason api_skill_detail offloads its two calls:
    # create_skill walks a pinned parent chain and writes the SKILL.md.
    ok = await asyncio.to_thread(skills.create_skill, safe_name, content)
    if not ok:
        return web.json_response(
            {"error": f"skill '{safe_name}' already exists", "code": "skill_exists"}, status=409
        )
    return web.json_response({"ok": True, "name": safe_name})
