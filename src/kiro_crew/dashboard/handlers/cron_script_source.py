"""Reading and rendering a script job's source.

``GET /api/crons/{id}/script`` resolves the job's script, reads it through the
link-refusing chokepoint and renders it redacted, with the digest an approval of
the script's secret grant echoes back.

Served through :mod:`kiro_crew.dashboard.handlers.cron`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import config_dir
from kiro_crew.cron_script import resolve_script_path
from kiro_crew.dashboard.handlers.cron_shared import (
    _invalid_path_id_response,
    _refuse_foreign_app_job,
    _sel,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

from ._shared import guard_owner_surface_routes

# Ceiling on the script source returned by GET /api/crons/{id}/script. Cron
# scripts are hand- or LLM-authored helpers of a few KB; anything near this
# ceiling is not a cron script, so the view truncates rather than streaming an
# unbounded file into the dashboard.
_SCRIPT_SOURCE_MAX_BYTES = 256 * 1024


# The read below traverses the link-refusal + fd-real-path chokepoint in hooks
# (safe_read_file_bytes_nolink). Every half answers on Windows as well as on POSIX:
# the open goes through ``platform_compat.open_file_no_reparse``, which refuses a
# reparse point at the FINAL component there in the same call that opens it;
# ``pinned_fs.fd_real_path`` reads the opened handle's real path through
# ``GetFinalPathNameByHandleW``, so the containment check against
# ``<config_dir>/crons/`` and the sensitivity check are pinned to the inode actually
# opened; and ``os.fstat`` reports ``st_nlink`` there, so the hardlink-alias refusal
# holds too. This read needs no ``dir_fd``/``openat``, which is the primitive Windows
# lacks.


def _read_script_source_sync(
    script_spec: object,
) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    """Resolve a job's stored ``script`` spec and read its source (blocking).

    Returns ``(payload, None)`` on success or ``(None, (message, code))`` on
    refusal. Runs in a worker thread — resolution stats the filesystem and the
    read is synchronous file IO, neither of which may run on the event loop.

    The path is derived exclusively from the job's own stored ``script`` field
    (never from the client), re-validated by ``resolve_script_path`` (existence,
    sensitivity, containment under ``<config_dir>/crons/``), and then read
    through ``safe_read_file_bytes_nolink`` pinned to that same root so a
    symlink or hardlink swapped in after the by-name check is rejected, never
    dereferenced.

    INVARIANT: no persisted job state may produce a 500 from this endpoint.
    ``script_spec`` comes from ``crons.json``, which is agent- and hand-editable
    JSON — its value can be any JSON type and any string shape. Every failure
    to resolve it, of any kind, is therefore a 4xx refusal, never a crash:
    the spec is validated as a string up front, and the resolution step is
    wrapped fail-closed (``FileNotFoundError`` stays distinct only to give the
    honest 404).
    """
    if not isinstance(script_spec, str):
        # Truthy non-string ``script`` in crons.json (number, list, object):
        # the handler's ``if not job.script`` gate passes it through, and the
        # resolver would crash on it. Refuse, same code as any bad path.
        return None, ("script path refused", "script_path_refused")
    try:
        # A PERSISTED spec off crons.json, so an app cron's bundle path must
        # resolve here; the nolink read below stays pinned to crons/, so a
        # bundle script yields a typed refusal rather than bundle bytes.
        file_path, func_name = resolve_script_path(script_spec, allow_bundle_roots=True)
    except FileNotFoundError:
        return None, ("script file not found", "script_not_found")
    except Exception:
        # Fail-closed catch-all, deliberate: malformed spec (ValueError), a
        # path escaping the crons root (PermissionError), symlink-loop
        # resolution failures (RuntimeError on some Python versions,
        # OSError/ELOOP on others), and any failure mode not yet enumerated —
        # the spec is untrusted persisted data, so an unanticipated exception
        # type must degrade to the same refusal as an anticipated one, never
        # to a 500. The refusal does not echo resolution detail (the spec
        # string is already visible on the job record; the resolved path is
        # not the client's business).
        return None, ("script path refused", "script_path_refused")
    crons_root = str((config_dir() / "crons").resolve())
    truncated = False
    try:
        data = safe_read_file_bytes_nolink(
            file_path, within_root=crons_root, max_bytes=_SCRIPT_SOURCE_MAX_BYTES
        )
    except FileTooLargeError:
        data = safe_read_file_bytes_nolink(
            file_path,
            within_root=crons_root,
            max_bytes=_SCRIPT_SOURCE_MAX_BYTES,
            allow_truncate=True,
        )
        truncated = True
    if data is None:
        # Fail-closed refusal from the chokepoint (swapped symlink, hardlink,
        # non-regular file, unverifiable containment). 4xx, never a 500.
        return None, ("script unreadable", "script_read_refused")
    # Scripts under crons/ are LLM-writeable by design, so treat their content
    # like any other agent-influenced text shown in the dashboard: strip raw
    # credential patterns and exfiltration URLs before it leaves the backend.
    # The file and function names come from the same stored spec, so they get
    # the identical treatment — a credential-shaped name must not ride out on
    # the metadata fields either.
    source, reviewable = _display_source(data)
    file_name = redact_credentials(redact_exfiltration_urls(os.path.basename(file_path))[0])[0]
    func = redact_credentials(redact_exfiltration_urls(func_name)[0])[0]
    # Digest of the RAW bytes read (pre-redaction): the approval path hashes
    # the same raw body when it pins a grant, and the operator's approve call
    # echoes this value back so the server can prove the code it blesses is
    # the code this response rendered. Over a truncated read the digest covers
    # only the prefix and can never match a full-body pin — correct, since a
    # script the operator could not see in full is not approvable either.
    return {
        "source": source,
        "file": file_name,
        "function": func,
        "truncated": truncated,
        "reviewable": reviewable and not truncated,
        "sha256": hashlib.sha256(data).hexdigest(),
    }, None


def _display_source(data: bytes) -> tuple[str, bool]:
    """Render a script body for the dashboard and say whether the rendering IS
    the code.

    Returns ``(display_text, reviewable)``. The display text is the UTF-8
    decode with credential patterns and exfiltration URLs redacted. It is
    ``reviewable`` only when that text equals the raw decode byte-for-byte:
    the approval flow binds the operator's decision to the digest of the RAW
    bytes, so any byte the display does not show faithfully — a span the
    redactor masked, or an undecodable sequence the decoder replaced — is code
    the operator would be blessing without having seen it. An agent could
    hide executable content behind exactly such a span (a string the redactor
    matches, an invalid UTF-8 run inside a bytes literal), so a body whose
    display differs from its raw form is not approvable at all, and the
    promotion path re-derives this verdict from its own body snapshot rather
    than trusting the client's copy of the flag.
    """
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        lossy = data.decode("utf-8", errors="replace")
        return redact_credentials(redact_exfiltration_urls(lossy)[0])[0], False
    shown = redact_credentials(redact_exfiltration_urls(decoded)[0])[0]
    return shown, shown == decoded


async def api_cron_script_source(request: web.Request) -> web.Response:
    """GET /api/crons/{id}/script — read-only source of a script cron's callable.

    The job id is the only caller-supplied input; the file path is derived
    server-side from the stored job record (see ``_read_script_source_sync``).
    """
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    app_denied = await _refuse_foreign_app_job(request, state, [job_id], "crons.script_source")
    if app_denied is not None:
        return app_denied
    # Freshness-guaranteed lookup, same rationale as api_cron_run: the job may
    # have been minted by another process and not yet be in the cache snapshot.
    job = await state.crons.get_job_async(job_id)
    if not job:
        return web.json_response({"error": "job not found", "code": "job_not_found"}, status=404)
    if not job.script:
        return web.json_response({"error": "job has no script", "code": "no_script"}, status=404)
    payload, err = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _read_script_source_sync, job.script
    )
    if err is not None:
        message, code = err
        # SEL audit: a refused read of an on-disk script is a guarded-path
        # permission decision (containment escape, symlink swap, unresolvable
        # spec) and must leave an audit record, same as an allowed read below.
        _sel().log_api_access(
            caller="dashboard",
            operation="cron.script_source",
            outcome="denied",
            source="api_cron_script_source",
            resources=f"job_id={job_id} code={code}",
        )
        # Literal statuses per branch (not a computed ``status=`` expression) so
        # the error-code contract ratchet can see each site is coded.
        if code == "script_not_found":
            return web.json_response({"error": message, "code": code}, status=404)
        return web.json_response({"error": message, "code": code}, status=422)
    # _read_script_source_sync returns exactly one of (payload, err) non-None.
    assert payload is not None
    _sel().log_api_access(
        caller="dashboard",
        operation="cron.script_source",
        outcome="ok",
        source="api_cron_script_source",
        resources=f"job_id={job_id} truncated={payload['truncated']}",
    )
    return web.json_response(payload)


# Every api_* handler in this module is an owner surface, so a private member's
# internal call is refused before it runs.
guard_owner_surface_routes(globals(), member_scoped=frozenset())
