"""Reading, creating, updating and deleting one prompt.

``GET``, ``PUT`` and ``DELETE /api/prompts/{name}`` and ``POST /api/prompts``: the
scoped read, the create, update and delete that each run pinned to the prompt
directory they resolved, and the name limits a create enforces.

Served through :mod:`kiro_crew.dashboard.handlers.prompts`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import pinned_fs
from kiro_crew.dashboard.handlers.prompts_caller_gates import (
    _deny_non_owner_prompt_write,
    _prompt_audit,
    _prompt_local_project,
    _refuse,
    _sel,
)
from kiro_crew.dashboard.handlers.prompts_paths import (
    _DIR_FD_SUPPORTED,
    _IS_WINDOWS,
    _UNNAMED_CREATE_SUPPORTED,
    _is_path_too_long_error,
    _pin_prompt_dir,
    _plain_stem_ok,
    _prompt_read_within_root,
    _resolve_prompt_dir,
)
from kiro_crew.dashboard.handlers.prompts_reading import (
    _description_from_text,
    _find_prompt,
    _redact_prompt,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.hooks import (
    FileTooLargeError,
    safe_read_file_bytes_nolink,
    verified_replace_file_nolink,
)
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

from ._shared import _read_session_key

MAX_PROMPT_BYTES = 100_000  # 100 KB — public constant, imported across dashboard + gateway + tests


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
    # Resolve the local project on the loop first and capture it into the executor
    # job below, through the same _prompt_local_project seam the lister uses, so an
    # unscoped lookup can only match a local prompt the same request would have
    # been shown. That resolver reads state._slots and nothing else, so it is the
    # one step here that may stay on the loop.
    state: DashboardState = request.app["state"]
    session_key = _read_session_key(request)
    project_dir = _prompt_local_project(request, state, session_key)

    def _resolve_and_read() -> tuple[dict[str, Any] | None, str, str]:
        """Resolve the name AND read the matched file, in one executor job.

        Returns ``(entry, content, error_token)``, where an empty token means the
        read succeeded. Every step is filesystem work — the resolution's
        ``rglob('*.sop.md')`` walk over the (possibly large, edition-provided)
        package roots on a cold/expired cache, the sensitive-path gate's
        ``resolve``, the size ``stat``, and the body ``read_text`` — so they share
        ONE job rather than handing the metadata back and finishing on the loop.
        Which matters because a match does not always name a package root or
        the gateway's own ``~/.kiro/prompts``: it can name
        ``<project>/.kiro/prompts``, a directory the gateway does not own and that
        may be network-backed, so a ``stat`` and a read left on the loop would
        stall every other request and the heartbeat on exactly the storage this
        endpoint newly reaches. ``_api_user_prompt_detail``'s ``_read`` is one job
        for the same reason.
        """
        p = _find_prompt(raw, project_dir)
        if not p:
            return None, "", "not_found"
        from kiro_crew.hooks import validate_file_path  # noqa: F811

        # Canonicalize and refuse a sensitive target FIRST, so that refusal keeps
        # its own coded 403 rather than being folded into the gate's single
        # deliberately-uninformative None below.
        resolved = validate_file_path(p["path"])
        if resolved is None:
            return p, "", "blocked"
        # Read through the hardlink-rejecting gate rather than by name, the same
        # way the scoped branch and the chat `@mention` expansion do. Canonicalizing
        # a path and then opening THAT NAME leaves a window in which the leaf is
        # swapped for a link, so the bytes served are not the bytes any check ran
        # against; the gate opens first with ``O_NOFOLLOW`` and validates the
        # descriptor it actually read, so the inode checked is the inode returned,
        # and ``st_nlink > 1`` or a non-regular inode is refused. Every entry
        # reaching here was minted by the listing gate, which refuses a link
        # outright, so this closes that swap window rather than a standing hole.
        #
        # ``within_root`` comes from the one shared derivation, which also decides
        # when a missing root is a REFUSAL rather than a fallback — see
        # ``_prompt_read_within_root``.
        read_root = _prompt_read_within_root(p, project_dir, resolved)
        if read_root is None:
            return p, "", "error"
        try:
            data = safe_read_file_bytes_nolink(
                resolved,
                within_root=read_root,
                max_bytes=MAX_PROMPT_BYTES,
            )
        except FileTooLargeError:
            # Its own 413, exactly as the explicit size stat this replaces gave,
            # and as the scoped branch gives.
            return p, "", "too_large"
        if data is None:
            # The gate refuses and reads through ONE descriptor, so it cannot say
            # which of a link, a hardlink, an escaping inode or an unreadable file
            # happened — and that is deliberate, since distinguishing them would
            # make the endpoint an oracle for a link's target. Reported as the
            # `file not readable` this route already answered for an unreadable
            # file, so a refusal reveals nothing the plain case did not.
            return p, "", "error"
        return p, data.decode("utf-8", errors="replace"), ""

    p, content, err = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _resolve_and_read
    )
    if p is None:
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
    if err == "blocked":
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
    if err == "too_large":
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
    if err == "error":
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


def _invalidate_prompt_cache() -> None:
    """Late-binding into the parent package, where the cache globals live."""
    # circular import: the parent package imports this module, so a top-level
    # import here would not resolve.
    import kiro_crew.dashboard.handlers as _pkg

    _pkg._invalidate_prompt_cache()


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


#: Serializes prompt mutations (PUT/DELETE) within this process. The update
#: path's compare-and-swap is check-then-write across two filesystem calls,
#: and the discovery pool runs jobs concurrently, so without this two writers
#: could both verify the same base before either replaces the file. Create
#: needs no lock: its atomicity is O_EXCL on the open itself.
_PROMPT_WRITE_LOCK = threading.Lock()


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

    # Resolve the local project on the event loop (_prompt_local_project only
    # reads state._slots, non-blocking) and capture it into the executor closure
    # below, so a "local" create lands in the requester's own checkout. It is the
    # SAME seam the lister resolves through, which is what keeps create and list
    # from disagreeing — a prompt created here is one the same request lists.
    state: DashboardState = request.app["state"]
    session_key = _read_session_key(request)
    project_dir = _prompt_local_project(request, state, session_key)

    def _write_inner() -> str | None:
        target_dir, err = _resolve_prompt_dir(scope, project_dir)
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
            except FileNotFoundError as exc:
                # On Windows ``path.open`` is the CRT ``_wopen``, whose too-long
                # refusal carries only ``errno`` ENOENT and no ``winerror``, so
                # ``_is_path_too_long_error`` cannot see it. The parent was just
                # made above, so if it still exists a missing parent is not the
                # cause and the joined path's length is. A ``winerror`` code is
                # left to the shared predicate, which reads it as it says.
                if (
                    _IS_WINDOWS
                    and created_ident is None
                    and getattr(exc, "winerror", None) is None
                    and target_dir.is_dir()
                ):
                    return "path_too_long"
                raise
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
            # residual is the one pinned_fs.unlink_verified documents as
            # irreducible and ships: the verify and the unlink are two syscalls
            # on one name, so a replacement landing between them is lost. It is
            # accepted here for the reason it is accepted there -- POSIX offers
            # nothing better -- and it is now confined to filesystems that cannot
            # build an unnamed inode.
            #
            # The transaction itself is pinned_fs.write_new_file_pinned's, shared
            # with the steering and skill creates: the O_EXCL|O_NOFOLLOW create
            # relative to the pinned directory, the identity probe before the
            # first write, the short-write loop, the close inside the guard, and
            # the identity-checked rollback. fsync=True adds what 201 promises
            # here and the other two creates do not: the body is flushed before
            # the close, and the ENTRY is flushed after it -- a directory flush
            # that fails withdraws the publication through the same rollback, so
            # a caller that gets the 500 retries into a clean create. Only the
            # taxonomy is this surface's: an occupied name is "exists".
            try:
                pinned_fs.write_new_file_pinned(
                    dir_fd, filename, content.encode("utf-8"), mode=0o666, fsync=True
                )
            except FileExistsError:
                return "exists"
        finally:
            os.close(dir_fd)
        return None

    def _write() -> str | None:
        # The name fit its budget, but the JOINED path handed to the filesystem
        # may still be too long for THIS host (a Windows install without
        # long-path support). Map that one refusal onto a coded 400 sentinel; let
        # every other OSError keep the generic write-failure handling below.
        try:
            return _write_inner()
        except OSError as exc:
            if _is_path_too_long_error(exc):
                return "path_too_long"
            raise

    # A filesystem refusal (EACCES, ENOSPC, a name the FS still rejects) must
    # leave an audit trail and a coded answer, not escape as a bare 500 from
    # inside the executor. Caught on Exception rather than OSError — as the
    # skill handlers in the prompts_skill_* owners do — because the property claimed
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
    if err == "path_too_long":
        # A request-shape refusal, answered and audited like the name bound's.
        _refuse(op, "bad_request", "name_too_long", name=safe_name, scope=scope)
        return web.json_response(
            {"error": "prompt name is too long for its destination path", "code": "name_too_long"},
            status=400,
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

    # Resolve the local project on the loop and close over it so a scoped
    # "local" read addresses the requester's own checkout — through the same
    # _prompt_local_project seam create and list use, so the bytes the editor is
    # seeded from are the bytes a following PUT to this scope would replace.
    state: DashboardState = request.app["state"]
    session_key = _read_session_key(request)
    project_dir = _prompt_local_project(request, state, session_key)

    def _read() -> tuple[str | None, str, str | None, str, bool, str]:
        # Directory resolution, the link check, and description extraction all
        # touch the filesystem, so they share this one executor job rather than
        # running on the loop.
        target_dir, derr = _resolve_prompt_dir(scope, project_dir)
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
        # be served through this endpoint. It also enforces the size cap, so no
        # separate stat() is needed for it.
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
            # the writer refuses when the file does not match it. Hashed from
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

    # Resolve the local project on the loop and close over it in _apply_locked so
    # a "local" update/delete addresses the requester's own checkout, through the
    # same _prompt_local_project seam the scoped read uses to seed the editor —
    # the write lands in the file the read served, not in another project's copy
    # of the same stem.
    state: DashboardState = request.app["state"]
    session_key = _read_session_key(request)
    project_dir = _prompt_local_project(request, state, session_key)

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
        target_dir, derr = _resolve_prompt_dir(scope, project_dir)
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
