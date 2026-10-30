"""Probing one MCP server.

``probe_server`` is the entry every probe passes through: it refuses a
consent-disabled server, hands a remote one to ``_probe_remote``, and spawns a
local one in the sandbox off the gateway's event loop, runs the ``initialize``
and ``tools/list`` handshake over its stdio, records the temp declarations it
refused, and caches the verdict.

:mod:`kiro_crew.mcp_discovery` stays the import path and the patch surface for
every name here; that module's comment lists the owners.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.env import (
    expand_spec_args,
    mcp_search_path,
    resolved_command_casing,
    sanitize_spec_env,
    saved_command_casing,
    spec_path_key,
)
from kiro_crew.executors import mcp_probe_executor
from kiro_crew.mcp_discovery_managed import _is_first_party_managed_argv, _managed_tools_in_process
from kiro_crew.mcp_discovery_probe_state import (
    _PROBE_TEARDOWN_WAIT_SECS,
    MCP_CLIENT_PROTOCOL_VERSION,
    _cache_probe,
    _clear_unresolvable,
    _get_probe_timeout,
    _probe_cache,
    _sanitize_probe_error,
    _unresolved_error,
    _warn_managed_in_process_once,
    _warn_probe_sandbox_unavailable_once,
    _warn_unresolvable_once,
    downgrade_protocol_version,
)
from kiro_crew.mcp_discovery_remote import _probe_remote
from kiro_crew.mcp_discovery_server_info import McpServerInfo
from kiro_crew.sandbox import (
    CANONICAL_TEMP_KEYS,
    SandboxUnavailableError,
    classify_declared_temp_env,
    classify_declared_temp_path,
    create_subprocess_limited,
    declared_temp_refusal_reasons,
    format_declared_temp_refusals,
    sandboxed_spawn_argv,
    sandboxed_spawn_argv_async,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger("kiro_crew.mcp_discovery")


# Cap on how many *non-JSON banner* lines to skip while waiting for the
# JSON-RPC handshake. Only lines that are not JSON count toward this cap;
# blank lines and well-formed JSON-RPC notifications are bounded by the shared
# timeout budget alone (so a chatty-but-spec-compliant server that emits many
# notifications before its response is not mis-capped). A well-behaved server
# emits its response immediately; a chatty launcher (e.g. ``aim`` mid-self-
# update) may prepend a banner line or two.
_MAX_BANNER_LINES = 50


async def _read_stdio_jsonrpc_response(
    stream: asyncio.StreamReader, timeout: float, name: str = ""
) -> dict | None:
    """Read stdout until a JSON-RPC *response* object appears.

    stdio MCP servers must speak newline-delimited JSON, but some processes —
    or launchers that front them, like ``aim`` while self-updating — print a
    human-readable banner or a blank line to stdout *before* the handshake.
    Reading the first line and ``json.loads``-ing it directly would let a
    single stray line raise ``Expecting value: line 1 column 1 (char 0)`` and
    report a healthy server as errored (cached for up to 30 min).

    This consumes lines within one overall ``timeout`` budget, skipping blank
    lines, non-JSON lines, and JSON-RPC *notifications* (objects without an
    ``id``), and returns the first JSON object that carries an ``id`` (a
    response). Only lines that are not JSON count toward ``_MAX_BANNER_LINES``;
    blanks and notifications are bounded by the timeout alone. Returns ``None``
    on EOF or once more than ``_MAX_BANNER_LINES`` banner lines have arrived
    (the flood case is logged). Raises ``asyncio.TimeoutError`` if the deadline
    elapses, so the caller's existing timeout handling is preserved.

    Note the divergent sibling: the remote HTTP/SSE path uses
    :func:`_read_jsonrpc_response`, which returns ``{}`` (not ``None``) on an
    empty response and does NOT filter notifications. Keep the two straight —
    do not copy one call site's null-handling to the other.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    banner_lines = 0
    first_banner = ""
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        line = await asyncio.wait_for(stream.readline(), timeout=remaining)
        if not line:
            # EOF — process closed stdout without responding. Preserve the
            # "non-JSON was on stdout" signal a json.loads error would
            # surface, so a banner-then-EOF probe is still diagnosable.
            if banner_lines:
                logger.debug(
                    "MCP probe [%s]: EOF after %d banner line(s); first banner: %r",
                    name or "?",
                    banner_lines,
                    first_banner,
                )
            return None
        text = line.decode(errors="replace").strip()
        if not text:
            continue  # blank line — bounded by the timeout budget, not the cap
        try:
            parsed = json.loads(text)
        except (ValueError, RecursionError):
            # Not JSON: a banner/log line (e.g. `aim` self-update), or a value
            # nested past the decoder's ceiling. Only these count toward the
            # flood cap.
            banner_lines += 1
            if not first_banner:
                first_banner = text[:120]
            if banner_lines > _MAX_BANNER_LINES:
                logger.warning(
                    "MCP probe [%s]: no JSON-RPC response after %d banner "
                    "line(s); first banner: %r",
                    name or "?",
                    banner_lines,
                    first_banner,
                )
                return None
            continue
        # A JSON-RPC response always carries "id"; skip notifications (objects
        # with "method" and no "id") and non-object payloads (a progress
        # counter, a list). These do NOT count toward the banner cap — the
        # timeout budget bounds them.
        if isinstance(parsed, dict) and "id" in parsed:
            return parsed


async def _probe_on_private_loop(
    server: McpServerInfo, client_info: dict[str, str] | None
) -> McpServerInfo:
    """Run one local probe on a PRIVATE event loop owned by a pooled worker thread.

    ``create_subprocess_exec`` forks and execs synchronously before its first
    await, on every platform, so on the gateway loop a slow spawn freezes every tab.
    A cancel is forwarded to the private task, whose ``finally`` reaps the child.
    """
    handle: list[tuple[asyncio.AbstractEventLoop, asyncio.Task[Any] | None]] = []

    async def _main() -> McpServerInfo:
        handle.append((asyncio.get_running_loop(), asyncio.current_task()))
        return await probe_server(server, client_info=client_info, _on_private_loop=True)

    def _own_loop() -> McpServerInfo:
        with asyncio.Runner() as runner:
            return runner.run(_main())

    fut = asyncio.get_running_loop().run_in_executor(mcp_probe_executor(), _own_loop)
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        fut.cancel()
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())
        for loop, task in handle:
            if task is not None:
                with contextlib.suppress(RuntimeError):  # private loop already closed
                    loop.call_soon_threadsafe(task.cancel)
        raise


def _drop_temp_refusals(server: McpServerInfo) -> None:
    """Clear a skipped probe's refusals on the row AND in the probe cache.

    A skipped probe stands behind no refusal, and ``list_servers`` rehydrates
    the row from the cache on the next read, so clearing the row alone would
    bring the stale refusal back.
    """
    server.temp_refusals = []
    cached = _probe_cache.get(server.name)
    if cached is not None:
        cached.temp_refusals = []


def secret_reference_refusal(secret_keys: list[str] | tuple[str, ...]) -> str:
    """The not-probed reason for a server whose env holds ``secret://`` values.

    Names only the env KEYS, never a value. The MCP tab shows an ``outdated``
    row's error as this reason, with the routing guide beside it.
    """
    return (
        f"not probed: env {', '.join(secret_keys)} holds a secret:// reference, "
        "which is resolved only when the MCP gateway starts the server"
    )


def _served_by_gateway(name: str) -> bool:
    """True when the running MCP gateway starts server *name* for a session.

    The same answer the agent-spec rebuild acts on
    (``kiro_crew.agent.gateway_routed_names``, matched with
    ``routed_for_secret_reference``): saved routing AND a stub in the overlay the
    running gateway built. Saved routing alone is only intent until the next
    gateway start, so a row reads routed exactly when a session would launch the
    server through the gateway. Blocking (config and overlay reads); any failure
    answers False, which keeps the refusal row and its routing guidance.
    """
    try:
        # Deferred: ``kiro_crew.agent`` is heavy, and this probe module must not
        # pull it in at import time.
        from kiro_crew import agent
        from kiro_crew.mcp_gateway.secret_uri import routed_for_secret_reference

        return routed_for_secret_reference(name, agent.gateway_routed_names())
    except Exception:  # noqa: BLE001 -- "cannot tell" keeps the refusal row
        logger.debug("Could not read MCP gateway routing for %s", name, exc_info=True)
        return False


def _redact_temp_refusal_text(text: str) -> str:
    """The redactor the declared-temp WARNING applies to a path or failure."""
    return _sanitize_probe_error(ValueError(text))


def _temp_refusal_records(refused: dict[str, tuple[str, str]]) -> list[dict[str, str]]:
    """``McpServerInfo.temp_refusals`` entries for a refused temp declaration."""
    return [
        {"key": key, "path": _redact_temp_refusal_text(path), "cause": cause}
        for key, (path, cause) in sorted(refused.items())
    ]


async def probe_server(
    server: McpServerInfo,
    *,
    client_info: dict[str, str] | None = None,
    _on_private_loop: bool = False,
) -> McpServerInfo:
    """Probe a single MCP server by spawning it and sending initialize.

    Updates server.status and server.tools in place and returns it.

    *client_info* overrides the ``clientInfo`` sent in the handshake. The
    shareability pre-flight uses it to ask the same server twice under two
    identities: a server that negotiates its capabilities from ``clientInfo``
    answers differently, and that is precisely the case a pooled backend cannot
    serve — it caches the first stub's ``initialize`` result and replays it to
    every later stub. Callers that just want status and tools omit it and keep
    the probe's own identity.

    A consent-disabled server is refused HERE, ahead of the local/remote
    dispatch, because probing is the act that runs it: the local branch spawns
    the command and the remote branch opens the connection. Enforcement in each
    caller (``probe_all`` filtering disabled rows before building
    coroutines) would make the guarantee only as good as the newest call
    site's memory — a second entry point would have to restate the check or
    become a way around the consent gate. Keeping the rule in the one function every
    probe must pass through removes that whole class; callers keep their own
    filters and error surfaces as behaviour and UX, not as the safety property.
    """
    # This probe is the sole authority for its own temp refusals; a row
    # rehydrated from the cache must not keep an earlier probe's list on any
    # exit, including the ones below that never spawn.
    server.temp_refusals = []
    if server.disabled:
        _drop_temp_refusals(server)
        server.status = "disabled"
        # Truthy rather than ``is True``: a hand-built McpServerInfo may carry
        # anything here, and any non-empty value should withhold the spawn.
        #
        # No probe ran, so there is nothing to record — deliberately NOT
        # calling _cache_probe(). That cache is keyed by name and shared with
        # ``GET /api/mcp`` via _get_cached(), so writing an empty "disabled"
        # entry would erase the tool list a real probe stored before the user
        # disabled the server. ``tools`` is left untouched for the same reason
        # (last known list, still worth showing); ``error`` is cleared because
        # a stale probe failure is not why this returned.
        server.error = ""
        return server

    if server.is_remote:
        return await _probe_remote(server)

    if server.registry_pointer and not server.command:
        # Nothing local to spawn: kiro-cli resolves this pointer from the admin's
        # catalog, which Kiro Crew never reads. Reported as ``registry`` -- no
        # verdict, so not cached (a stored tool list survives) and not counted
        # toward quarantine -- instead of an "Error / no command" the user cannot
        # act on. ``kiro-cli mcp list`` is where it is verified.
        _drop_temp_refusals(server)
        server.status = "registry"
        server.error = ""
        return server

    if not server.command:
        server.status = "error"
        server.error = "no command"
        logger.warning("MCP probe failed [%s]: no command configured", server.name)
        return server

    # A direct probe runs outside the MCP gateway, the one place a ``secret://``
    # env value is resolved, so it would start the server with the literal
    # reference as its credential. Refused before any spawn, and not cached, so
    # a real probe's stored tool list survives; only the env KEYS are named. The
    # status is ``outdated``, which carries no probe verdict: no handshake was
    # attempted, so the refusal must not count toward the failure quarantine.
    from kiro_crew.mcp_gateway.secret_uri import secret_reference_keys

    secret_keys = secret_reference_keys(server.env)
    if secret_keys:
        _drop_temp_refusals(server)
        # A server the running gateway serves is started there, where the
        # reference IS resolved, so "route it through the gateway" is not its
        # next step. It reports ``routed``: no probe ran, so it states where
        # the server runs and claims nothing about its health, and like
        # ``outdated`` it carries no verdict (not cached, not counted toward the
        # quarantine). Off the loop: the routing answer reads config and the
        # overlay from disk.
        if await asyncio.to_thread(_served_by_gateway, server.name):
            server.status = "routed"
            server.error = ""
            return server
        server.status = "outdated"
        server.error = secret_reference_refusal(secret_keys)
        return server

    if not _on_private_loop:
        return await _probe_on_private_loop(server, client_info)

    server.status = "probing"
    # The PATH the spawn will actually search, bound before the try so the
    # FileNotFoundError handler can name the searched directories regardless of
    # how far the attempt got.
    effective_path = ""
    # Stamped at probe START so the early error returns below (which skip the
    # cache) still carry an honest "when": the probe DID run at this time.
    # _cache_probe overwrites it with completion time on the paths it covers.
    server.probed_at = time.time()
    # Reset: the object may arrive carrying "declared" from a previous cached
    # result (list_servers merges the cache in), and this pass IS a handshake
    # unless the fallback below says otherwise.
    server.probe_mode = "handshake"
    proc = None
    sandbox_cleanup: str | None = None
    probe_tmp: "Path | None" = None
    try:
        env = dict(os.environ)
        # The same expression backs command resolution and the PATH emitted into
        # the agent config (``agent.install_agent``) — sharing it is what keeps
        # "probes healthy" and "works in a session" from disagreeing. The pooled
        # backend path resolves a bare command separately
        # (``mcp_gateway.rewriter``), against a spec value this has already
        # expanded.
        # Case-insensitive PATH key: a Windows-authored spec says "Path", and
        # the emitted config normalizes under that same spelling — reading only
        # "PATH" here would probe with a different path than the session gets.
        _path_key = spec_path_key(server.env)
        _declared_path = server.env.get(_path_key, "") if _path_key else ""
        env["PATH"] = mcp_search_path(_declared_path if isinstance(_declared_path, str) else "")
        # The declared env is untrusted config text applied to the environment
        # the SANDBOX LAUNCHER starts under, so loader/interpreter injection
        # keys must not pass through — they would execute before confinement
        # exists. See env.sanitize_spec_env; _note_denied_env explains a
        # resulting failure to the dashboard reader.
        env.update(sanitize_spec_env((k, v) for k, v in server.env.items() if k != _path_key))

        # Resolve command to absolute path using the merged env PATH
        effective_path = env.get("PATH") or ""
        # A command carrying a directory component is not PATH-searched:
        # ``shutil.which`` looks it up directly and ignores ``path=``. Reporting
        # ``effective_path`` for it would name directories that were never
        # consulted, inverting the not-installed/installed-elsewhere distinction
        # the search-path report exists to draw -- so the report gets "" while
        # the lookup below still uses the real PATH.
        reported_path = "" if os.path.dirname(server.command) else effective_path
        # Same rule the agent-config resolver and gatewayd's rewriter apply, so
        # the probe spawns the spelling the session will use: an absolute
        # command that exists and is executable is the operator's own spelling
        # and runs verbatim, except a saved PATHEXT ``.EXE`` extension, which
        # ``saved_command_casing`` heals; a PATH-resolved one gets the casing
        # repair, since a PATHEXT-synthesized ``.EXE`` reaching an
        # ``argv[0]``-dispatching shim would fail (or pass) the probe for a
        # reason the session does not share.
        if (
            os.path.isabs(server.command)
            and os.path.isfile(server.command)
            and os.access(server.command, os.X_OK)
        ):
            resolved = saved_command_casing(server.command)
        else:
            resolved = resolved_command_casing(shutil.which(server.command, path=effective_path))
        if not resolved:
            server.status = "error"
            server.error = _unresolved_error(server.command, reported_path)
            _warn_unresolvable_once(server.name, server.command, reported_path)
            return server

        # The command resolved, so forget any prior "not found" report — keyed on
        # resolvability, NOT on handshake health. Clearing this at the end of the
        # success path instead would skip the four exits that resolve fine but
        # fail later (no response, a JSON-RPC error reply, a timeout, any other
        # exception), leaving a stale key that silences the WARNING if the binary
        # is removed again. `command` is necessarily a str here, since
        # it resolved to a truthy path above.
        _clear_unresolvable(server.name, server.command)

        # A hostile MCP-config entry names the binary spawned here, so route it
        # through the sandbox chokepoint: OS-level isolation plus a
        # credential-scrubbed environment (on top of the augmented PATH built
        # above). ``strip_python_env`` keeps Kiro Crew's PYTHONPATH/PYTHONHOME out
        # of a foreign Python MCP server.
        #
        # ``first_party_fixed_argv`` is True ONLY when command+args+env EQUAL
        # the invocation this package derives for its own managed servers
        # (``agent._kirocrew_mcp_invocation`` + ``agent._managed_mcp_env`` via
        # ``_is_first_party_managed_argv``) — self-derived, not user-config text
        # — so on a host with genuinely no sandbox backend the "can the server
        # start?" probe runs for real instead of fail-closing. Third-party
        # probes (and any customized managed command/args/env) pass False and
        # keep the full fail-close + opt-in behavior.
        # Probe temp containment: each probe gets its OWN private dir
        # under the managed root, cleaned in this function's finally -- unlike
        # a backend, a probe knows exactly when its lifecycle ends, so no
        # shared directory and no sweep race exist. Lazily imported
        # (mcp_gateway.preflight imports this module, so a module-level import
        # would cycle), created off-loop, and fail-open: a probe must run even
        # when containment cannot be set up.
        #
        # Allocated BEFORE the sandbox wrap: the managed root lives at
        # ``<data home>/run/mcp-tmp``, inside the runtime parent the sandbox
        # seals read-only, so the wrap must know the directory to carve its
        # write access out of that seal. Allocating after the wrap hands the
        # child a ``TMPDIR`` it cannot write -- a Bun-packaged server then
        # fails the probe with "Cannot find the native Koffi module" because
        # it cannot extract its native module. The allocation failure path
        # stays fail-open (probe runs with inherited temp, no carve-out), and
        # the outer ``finally`` sweeps the dir even when the wrap itself
        # raises.
        #
        # Mirrors the backend chokepoint: a spec-DECLARED temp wins -- the
        # operator pointed this server at chosen storage, and overriding it
        # would trade litter for ENOSPC on the data-home volume. Checked
        # case-insensitively (Windows env keys are case-insensitive and the
        # sanitized spec preserves the author's spelling).
        _declared_temp_upper = {
            key.upper() for key in (server.env or {}) if key.upper() in CANONICAL_TEMP_KEYS
        }
        # The spec's OWN spellings, kept beside the upper-cased set: the rewrite
        # below has to tell the declaration apart from the ambient key of the
        # same name, and only the exact spelling does that -- comparing
        # upper-cased names alone lets BOTH survive.
        _declared_temp_spelled = {
            key for key in (server.env or {}) if key.upper() in CANONICAL_TEMP_KEYS
        }
        # ...but a declaration that lands inside the sealed runtime parent is
        # REFUSED rather than honored. Both backends seal
        # ``<data home>/run`` read-only, so honoring it hands the child a temp
        # dir it cannot write -- the same Bun/Koffi extraction failure the
        # managed temp root avoids, and silent because the probe still reports
        # a green handshake for servers that never touch temp. Carving the
        # declared path out of the seal is NOT the alternative: spec ``env`` is
        # untrusted config text and ``extra_writable_dirs`` is validated for
        # self-derived scratch only. The managed temp takes over instead, which
        # keeps the operator's storage intent -- their path named the data-home
        # volume, and so does the managed root. Refused as a WHOLE: ``tempfile``
        # consults TMPDIR before TMP, so honoring a surviving sibling key would
        # leave writability depending on which key the spec happened to spell.
        _sealed_temp: dict[str, tuple[str, str]] = {}
        failure = ""
        if _declared_temp_upper:
            accepted, _sealed_temp, failure = await asyncio.to_thread(
                classify_declared_temp_env,
                server.env or {},
                classifier=classify_declared_temp_path,
            )
            _declared_temp_upper = set(accepted)
            if _sealed_temp:
                server.temp_refusals = _temp_refusal_records(_sealed_temp)
                logger.warning(
                    "MCP probe [%s]: ignoring spec-declared %s — %s; probing with the "
                    "managed temp instead",
                    server.name,
                    format_declared_temp_refusals(
                        _sealed_temp,
                        redactor=_redact_temp_refusal_text,
                    ),
                    "; ".join(
                        declared_temp_refusal_reasons(
                            _sealed_temp,
                            failure,
                            redactor=_redact_temp_refusal_text,
                        )
                    ),
                )
        probe_scratch: "Path | None" = None
        if not _declared_temp_upper:
            try:
                from kiro_crew.mcp_gateway.backend_tmp import (
                    allocate_probe_tmp,
                    probe_child_scratch,
                )

                probe_tmp = await asyncio.to_thread(allocate_probe_tmp)
                # The child gets a SCRATCH SUBDIR, never the allocation root:
                # the root holds the ``.owner`` reclamation record, and the
                # write carve-out below must not put that record inside the
                # child's writable window (see probe_child_scratch).
                probe_scratch = await asyncio.to_thread(probe_child_scratch, probe_tmp)
            except Exception:
                logger.debug("probe temp containment unavailable", exc_info=True)
                if probe_tmp is not None and probe_scratch is None:
                    # The allocation exists but its child-facing half does
                    # not: reclaim now. Ownerless-or-provisional dirs are
                    # deliberately never deleted by the sweeps until
                    # owner-dead+idle, and no probe pid will ever be recorded
                    # for this one.
                    from kiro_crew.mcp_gateway.backend_tmp import sweep_backend_tmp

                    await asyncio.to_thread(sweep_backend_tmp, probe_tmp)
                    probe_tmp = None
        # Expanded the way the emitted agent spec is (see expand_spec_args), so
        # a ``${HOME}/...`` launch path the session can start is not probed
        # literally, failed and quarantined.
        _spawn_args = expand_spec_args(server.args or [], server=server.name)
        wrapped_argv, env, sandbox_cleanup = await sandboxed_spawn_argv_async(
            [resolved, *_spawn_args],
            mode="standard",
            env=env,
            strip_python_env=True,
            extra_writable_dirs=((str(probe_scratch),) if probe_scratch is not None else ()),
            first_party_fixed_argv=_is_first_party_managed_argv(
                server.name, server.command, server.args or [], server.env or {}
            ),
            _prepare=sandboxed_spawn_argv,
        )
        try:
            if probe_scratch is not None:
                from kiro_crew.mcp_gateway.backend_tmp import tmp_env

                # Merged AFTER the wrap so the managed triple lands on the
                # SCRUBBED env the child actually receives.
                #
                # Every OTHER spelling of a temp key is dropped first, not just
                # the three canonical names: spec env keys are matched
                # case-insensitively here, so a stray ``tmpdir`` -- from the
                # ambient env, or a declaration refused above -- would otherwise
                # sit in the env beside the managed ``TMPDIR``. Where env names
                # are case-insensitive the two are ONE variable and the survivor
                # decides what the child reads; where they are distinct the child
                # sees both. Either way the managed triple must be the only temp
                # keys left.
                env = {
                    key: value
                    for key, value in env.items()
                    if key.upper() not in CANONICAL_TEMP_KEYS
                }
                env = {**env, **tmp_env(probe_scratch)}
            elif _declared_temp_upper:
                # Yielding alone is not enough: ambient temp keys are still in
                # ``env`` and ``tempfile`` consults TMPDIR before TMP, so a
                # spec declaring only TMP would silently write through the
                # inherited ambient TMPDIR. Strip every canonical key the spec
                # did NOT itself spell (mirrors the backend chokepoint) --
                # matched case-insensitively, so an ambient ``TMPDIR`` cannot
                # outrank a declared ``tmpdir`` in the child's lookup while both
                # sit in the env.
                # ...and re-emitted under the CANONICAL uppercase name: on
                # POSIX ``tempfile`` reads only TMPDIR/TMP/TEMP as spelled, so
                # a spec declaring ``tmpdir`` would otherwise keep its key, lose
                # the ambient ones, and get the platform default -- the
                # declaration silently not governing while no managed temp was
                # allocated either. The spec spelling is dropped rather than
                # kept beside the canonical one, since on Windows the two are
                # ONE variable.
                declared_values = {
                    key.upper(): value
                    for key, value in env.items()
                    if key in _declared_temp_spelled
                }
                env = {
                    key: value
                    for key, value in env.items()
                    if key.upper() not in CANONICAL_TEMP_KEYS
                }
                env.update(declared_values)
            elif _sealed_temp:
                # The refusal above stands even though allocation failed, so
                # there is no managed dir to point at: strip every temp key,
                # the ambient ones included, so the child falls back to its
                # platform default (``/tmp`` on POSIX, the ``tempfile``
                # candidate list on Windows) instead of the refused path.
                # Fail-open on containment, never onto a directory already
                # known to be read-only.
                env = {
                    key: value
                    for key, value in env.items()
                    if key.upper() not in CANONICAL_TEMP_KEYS
                }
        except Exception:
            logger.debug("probe temp containment unavailable", exc_info=True)
        try:
            proc = await create_subprocess_limited(
                *wrapped_argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=1024 * 1024,  # 1 MB — some MCP servers return large responses
                # POSIX: setsid so the probe owns a dedicated process group and
                # teardown can killpg launcher grandchildren (a leader-only kill
                # leaked ``npx @playwright/mcp`` -> node trees). Windows: silently
                # ignored (mirrors AcpRuntime / AcpClient._spawn).
                start_new_session=platform_compat.IS_POSIX,
            )
        except BaseException:
            # Spawn failed: the probe never existed, so reclaim its fresh dir
            # here and now -- ownerless-or-provisional dirs are deliberately
            # never deleted by the sweeps, making this the ONLY reclamation
            # point for it (mirrors spawn_backend's failure path).
            if probe_tmp is not None:
                from kiro_crew.mcp_gateway.backend_tmp import sweep_backend_tmp

                await asyncio.to_thread(sweep_backend_tmp, probe_tmp)
            raise
        if probe_tmp is not None:
            # Re-record the owner as the PROBE's pid. The provisional owner
            # written at allocation is THIS gateway process, which stays alive
            # indefinitely -- on the Windows path (finally-sweep deferred) the
            # daemon sweep would then retain the dir forever, accumulating one
            # per probe. The probe pid dies with the probe, so owner-dead+idle
            # reclamation works there. Off-loop, fail-open (record_owner
            # swallows OSError; a stale provisional owner then keeps the dir
            # until this gateway exits, bounded by gateway lifetime).
            from kiro_crew.mcp_gateway.backend_tmp import record_owner

            await asyncio.to_thread(record_owner, probe_tmp, proc.pid)

        assert proc.stdin is not None
        assert proc.stdout is not None
        # Offer our newest revision; a -32602 refusal listing ``data.supported``
        # gets one retry on the same pipe with the newest older one it lists,
        # the step-down the HTTP probe takes (``mcp_discovery_remote``).
        protocol_version = MCP_CLIENT_PROTOCOL_VERSION
        while True:
            init_req = (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": protocol_version,
                            "capabilities": {},
                            "clientInfo": (
                                dict(client_info)
                                if client_info
                                else {"name": "kirocrew-probe", "version": "1.0.0"}
                            ),
                        },
                    }
                )
                + "\n"
            )
            proc.stdin.write(init_req.encode())
            await proc.stdin.drain()

            # Read initialize response. Skip any leading non-JSON banner/log
            # lines the server (or a launcher like ``aim`` mid-self-update) may
            # emit on stdout before the JSON-RPC handshake — otherwise a single
            # stray line makes json.loads() raise and a healthy server is wrongly
            # marked errored.
            resp = await _read_stdio_jsonrpc_response(
                proc.stdout, _get_probe_timeout(), name=server.name
            )
            fallback = downgrade_protocol_version(resp, protocol_version)
            if not fallback:
                break
            protocol_version = fallback
        if resp is None:
            server.status = "error"
            server.error = "no response"
            return server

        if isinstance(resp, dict) and resp.get("error"):
            server.status = "error"
            err = resp["error"]
            server.error = (
                err.get("message", "unknown error") if isinstance(err, dict) else str(err)
            )
            return server

        # Keep the handshake metadata the probe already paid for. Read from the
        # server's ANSWER, never from what we asked for: a server may negotiate
        # down to an older protocol version, and that answer is exactly what
        # tells us whether tool annotations could have been sent at all.
        init_result = resp.get("result") if isinstance(resp, dict) else None
        if isinstance(init_result, dict):
            caps = init_result.get("capabilities")
            # An absent capabilities object and an empty one are different
            # claims; only a dict counts as "the server declared something".
            server.capabilities = caps if isinstance(caps, dict) else {}
            version = init_result.get("protocolVersion")
            server.protocol_version = version if isinstance(version, str) else ""
            info = init_result.get("serverInfo")
            server.server_info = info if isinstance(info, dict) else {}

        # Send initialized notification
        notif = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }
            )
            + "\n"
        )
        proc.stdin.write(notif.encode())
        await proc.stdin.drain()

        # Request tool list
        list_req = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                }
            )
            + "\n"
        )
        proc.stdin.write(list_req.encode())
        await proc.stdin.drain()

        resp2 = await _read_stdio_jsonrpc_response(
            proc.stdout, _get_probe_timeout(), name=server.name
        )
        if resp2 is None:
            # initialize succeeded but tools/list yielded no response (banner
            # flood or EOF on this read). "ok" here would certify a server no
            # session can get a tool out of — the whole point of the badge is
            # "tools usable", so a failed tools/list is a failed probe. Cached
            # (matching _probe_remote's error paths) so the list overlay shows
            # the same failure the direct probe reports, instead of serving a
            # stale prior "ok" for up to the TTL.
            server.status = "error"
            server.error = "tools/list: no response after a successful initialize"
            logger.warning(
                "MCP probe failed [%s]: tools/list returned no response after "
                "a successful initialize",
                server.name,
            )
            # Same synthetic-identity rule as the trailing cache write: a
            # pre-flight's second-identity run is a diagnostic, not the
            # canonical observation the dashboard renders.
            if client_info is None:
                _cache_probe(server)
            return server
        if isinstance(resp2, dict) and resp2.get("error"):
            err2 = resp2["error"]
            server.status = "error"
            server.error = "tools/list: " + (
                err2.get("message", "unknown error") if isinstance(err2, dict) else str(err2)
            )
            if client_info is None:
                _cache_probe(server)
            return server
        result = resp2.get("result", {}) if isinstance(resp2, dict) else {}
        tools_data = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools_data, list):
            # A response without a tools LIST is malformed, not "no tools":
            # rendering it as a green zero-tool server would certify a server
            # whose one required answer didn't parse. A genuinely tool-less
            # server sends an empty list, which passes.
            server.status = "error"
            server.error = "tools/list: malformed response (no tools list)"
            if client_info is None:
                _cache_probe(server)
            return server
        server.tools = [
            name for t in tools_data if isinstance(t, dict) and (name := t.get("name", ""))
        ]
        # ``annotations`` (MCP's March 2025 revision on) is the only spec-native hint
        # about whether a tool mutates anything. Collected as positive
        # evidence only — a server on an older protocol version sends none,
        # and that must never read as "this tool writes".
        server.tool_annotations = [
            ann
            for t in tools_data
            if isinstance(t, dict) and isinstance(ann := t.get("annotations"), dict)
        ]

        server.status = "ok"

    except asyncio.TimeoutError:
        server.status = "error"
        server.error = "timeout"
        logger.warning(
            "MCP probe failed [%s]: timeout after %ds", server.name, _get_probe_timeout()
        )
    except FileNotFoundError:
        server.status = "error"
        # Report the search path only for a bare command: a directory-qualified
        # one is looked up directly by shutil.which, not PATH-searched, so naming
        # ``effective_path`` would cite directories never consulted. Recomputed
        # here (not read from ``reported_path``) because this handler can fire
        # before the try-body binds it, just as ``effective_path`` is bound
        # before the try for exactly that reason.
        _reported = "" if os.path.dirname(server.command) else effective_path
        server.error = _unresolved_error(server.command, _reported)
        _warn_unresolvable_once(server.name, server.command, _reported)
    except SandboxUnavailableError as exc:
        # The PROBE could not run — this says nothing about the server, and the
        # two must not be reported alike. Ahead of the generic clause, which would
        # render this as a server fault.
        #
        # For one of OUR OWN managed servers there is a better answer than an
        # error: its tool list is a static declaration in this package
        # (``mcp_core._list_tools()`` and friends — the very functions the stdio
        # shim answers ``tools/list`` from), so read it directly. That is what
        # keeps the built-in tools listed on a host with no sandbox backend (any
        # Windows host, macOS >= 26) without asking the operator for an
        # ``agent.sandbox_allow_unsandboxed_exec`` opt-in for a read-only
        # listing. A managed server whose command+args are self-derived normally
        # never reaches here on such a host — the first-party carve-out lets its
        # probe spawn for real — so this fallback covers the residual cases: a
        # transient sandbox failure, a foreign outer sandbox, a governance
        # sandbox floor, or a customized command.
        #
        # Deliberately a FALLBACK, not the primary path. Two reasons:
        #   * the spawn is the only thing that proves the server can actually
        #     START. `_fix_stale_managed_command` exists because that invocation
        #     does go stale ("command not found: kirocrew; the built-in cron/core
        #     tools then never load"), and short-circuiting on the name alone would
        #     report `ok` for a managed server that cannot run — changing what `ok`
        #     means in the shared `_cache_probe` store, silently, for the one
        #     surface that catches it.
        #   * importing these modules runs package code IN THE GATEWAY PROCESS,
        #     which the gateway does not otherwise do (they are absent from
        #     sys.modules at boot). The package dir is writable by the same uid the
        #     agent runs as and is not on the sensitive-path floor, so on a host
        #     where the sandbox DOES work, importing beats the isolation the spawn
        #     provides. Reaching here means the sandbox could not confine anything
        #     anyway, so the import adds no exposure the refused spawn had not
        #     already conceded — and it is the only way to serve the listing there.
        managed_tools = _managed_tools_in_process(server.name)
        if managed_tools is not None:
            server.status = "ok"
            server.tools = managed_tools
            server.error = ""
            # Not a handshake: nothing verified the server can START. The mode
            # rides the cache into the API payload so the UI can distinguish a
            # declared listing from a proven one instead of rendering both as
            # the same green badge.
            server.probe_mode = "declared"
            _warn_managed_in_process_once(server.name)
            if client_info is None:
                _cache_probe(server)
            return server
        #
        # The wrap is deliberately KEPT rather than skipped for Kiro Crew's own
        # managed servers. "It is our own code" is not the same claim as "the code
        # is unmodified": the package directory is writable by the same uid the
        # agent runs as and is not on the sensitive-path floor, so a prompt-injected
        # agent can edit an editable checkout (or the console script) and an
        # unwrapped probe would then execute it outside the sandbox on the next
        # automatic probe_all(). Skipping the wrap for a managed server would make
        # this the one unsandboxed spawn path in the codebase; the sibling paths
        # (script crons, script hooks, Papyrus compile/git) all keep the wrap and
        # require the opt-in on a backendless host, and this now matches them.
        #
        # So what changes is the REPORTING. The `mcp_probe_` prefix is
        # machine-readable, mirroring the `code` field on the dashboard's JSON error
        # bodies, so a presentation layer can tell an unfixable-by-retry probe
        # limitation apart from a genuine handshake failure without parsing prose.
        server.status = "error"
        server.error = (
            f"mcp_probe_sandbox_unavailable: Kiro Crew could not probe this server "
            f"because no OS-level sandbox backend is available on this host. The "
            f"server itself may be fine — kiro-cli launches it from the agent "
            f"config without this probe, so its tools can still work in chat. "
            f"Set agent.sandbox_allow_unsandboxed_exec=true to enable probing. "
            f"({_sanitize_probe_error(exc)})"
        )
        _warn_probe_sandbox_unavailable_once(server.name)
    except Exception as exc:
        server.status = "error"
        server.error = _sanitize_probe_error(exc)
        logger.warning("MCP probe failed [%s]: %s", server.name, server.error)
    finally:
        # When the probe failed, drain any stderr the child wrote and append
        # a redacted tail to the error message. Most MCP servers print a
        # useful diagnostic (Python traceback, ModuleNotFoundError,
        # a build-tool exception, etc.) on startup failure;
        # without this, callers only see opaque strings like "timeout" or
        # "no response" with no hint of the underlying cause.
        #
        # stderr is untrusted process output that could contain leaked
        # credentials or exfiltration URLs, so scrub it with the security
        # redactors before it reaches doctor output / dashboard / Slack.
        if proc is not None and proc.stderr is not None and server.status == "error":
            try:
                stderr_bytes = await asyncio.wait_for(proc.stderr.read(4096), timeout=1.0)
                stderr_tail = stderr_bytes.decode(errors="replace").strip()
                if stderr_tail:
                    clean, _ = redact_exfiltration_urls(stderr_tail)
                    clean, _ = redact_credentials(clean)
                    server.error = f"{server.error}\nstderr: {clean[:500]}"
            except (asyncio.TimeoutError, Exception):
                pass
        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=_PROBE_TEARDOWN_WAIT_SECS)
            except (asyncio.TimeoutError, Exception):
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=_PROBE_TEARDOWN_WAIT_SECS)
                except Exception:
                    pass
        if proc is not None and platform_compat.IS_POSIX:
            # Reap the probe's ENTIRE process group. The child was spawned with
            # start_new_session=True, so pgid == proc.pid and the group holds
            # any grandchildren the launcher forked (``npx`` / ``node`` shim ->
            # real MCP server). A leader-only kill — and even a graceful leader
            # exit — leaves those grandchildren alive, accumulating one leaked
            # tree per failed probe per discovery cycle. Race-free even after
            # the leader was reaped: a PID in use as a pgid cannot be recycled
            # while any group member lives, so killpg hits only our group or
            # raises ESRCH on an empty one. The int/>1 guard mirrors
            # _sync_kill_provider: a mock stand-in pid must never coerce this
            # into killpg(1) == init.
            probe_pid = proc.pid
            if isinstance(probe_pid, int) and probe_pid > 1:
                try:
                    os.killpg(probe_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # group already empty — nothing leaked
                except OSError:
                    logger.debug(
                        "Probe group reap failed for %s (pgid %s)",
                        server.name,
                        probe_pid,
                        exc_info=True,
                    )
        elif proc is not None and platform_compat.IS_WINDOWS:
            # Windows has no process groups; reap the probe's whole tree via
            # taskkill /T so the launcher's grandchildren (npx/node -> real MCP
            # server) don't leak one tree per failed probe per discovery cycle.
            probe_pid = proc.pid
            if isinstance(probe_pid, int) and probe_pid > 1:
                try:
                    # OFF the event loop: on Windows kill_process_tree shells
                    # out to ``taskkill /T /F`` via a blocking
                    # ``subprocess.run``. Awaited inline it stalls the loop for
                    # the whole spawn+kill of taskkill — once per failed probe,
                    # and ``probe_all`` fans out across every configured server,
                    # so a discovery pass with several unreachable servers
                    # serializes that many process spawns onto the loop and the
                    # dashboard's health check starts dropping. The POSIX branch
                    # above is a bare ``killpg`` syscall and needs no offload.
                    await asyncio.to_thread(
                        platform_compat.kill_process_tree, probe_pid, platform_compat.SIGKILL
                    )
                except ProcessLookupError:
                    # The probe already exited before this post-probe reap, so
                    # ``taskkill /T /F`` returned rc=128 and kill_process_tree
                    # mapped it to ProcessLookupError (the Windows analog of the
                    # POSIX ESRCH silently absorbed above). Nothing leaked — a
                    # traceback here makes a routine cleanup race look identical
                    # to a genuine reap failure, so log it without one.
                    logger.debug(
                        "Probe tree already gone for %s (pid %s)",
                        server.name,
                        probe_pid,
                    )
                except OSError:
                    # A genuine cleanup failure (taskkill access-denied,
                    # spawn error, etc.) keeps its traceback-bearing diagnostic.
                    logger.debug(
                        "Probe tree reap failed for %s (pid %s)",
                        server.name,
                        probe_pid,
                        exc_info=True,
                    )
        if sandbox_cleanup:
            Path(sandbox_cleanup).unlink(missing_ok=True)
        if probe_tmp is not None:
            if platform_compat.IS_POSIX:
                # POSIX: the probe's dedicated process GROUP was reaped above
                # (killpg is tree-faithful), so its private temp dir dies with
                # it. Off-loop, fail-open (a failed cleanup is picked up by
                # the daemon sweep once the dir is owner-dead and idle).
                try:
                    from kiro_crew.mcp_gateway.backend_tmp import sweep_backend_tmp

                    await asyncio.to_thread(sweep_backend_tmp, probe_tmp)
                except Exception:
                    logger.debug("probe temp cleanup failed", exc_info=True)
            else:
                # Windows: taskkill /T walks PPID links and can miss a child
                # whose wrapper already exited, so tree death is UNPROVABLE
                # here. Deleting THIS dir now could remove temp storage under
                # a live survivor -- instead run the root-wide dual-condition
                # sweep (owner dead AND 1h+ whole-tree idle) from THIS
                # process: it reclaims prior probes' dead dirs while never
                # touching the fresh one (its tree is seconds old). Running
                # it here, not only in the mcp-tmp daemon, matters because a
                # topology with no stub servers never starts that daemon --
                # probes must not depend on it for reclamation. Concurrent
                # with a daemon sweep it is benign: both sides lstat-recheck
                # and rmtree(ignore_errors=True).
                try:
                    from kiro_crew.mcp_gateway.backend_tmp import sweep_all_backend_tmp

                    await asyncio.to_thread(sweep_all_backend_tmp)
                except Exception:
                    logger.debug("probe-side backend-tmp sweep failed", exc_info=True)

    # A probe run under a SYNTHETIC identity must not become the cached truth:
    # the per-name cache is what ``GET /api/mcp`` renders, and a pre-flight's
    # second-identity handshake is a diagnostic, not the canonical observation.
    if client_info is None:
        _cache_probe(server)
    return server
