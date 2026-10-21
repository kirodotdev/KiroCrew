"""Kiro Crew's own agent specs: presence, acceptance by kiro-cli and KAS, and repair.

Bound into :class:`KiroPrerequisiteService`; reads ``self._assume_ready``,
``self._spec_lister``, ``self._status`` (through :meth:`_dashboard_snapshot`) and
``self._repair_lock``, and runs ``self._audited_probe`` / ``self._probe``. Writes no
field of its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.agent_files import AGENT_FILENAME, LITE_AGENT_FILENAME
from kiro_crew.kiro_prerequisite_identity_store import _read_bounded_regular_file
from kiro_crew.kiro_prerequisite_status import _sanitize_detail
from kiro_crew.mcp_cleanup import mcp_entry_is_registry_governed

if TYPE_CHECKING:
    from kiro_crew.kiro_prerequisite_service import KiroPrerequisiteService

logger = logging.getLogger("kiro_crew.kiro_prerequisite")


# Dashboard-facing text for a spec-repair arm that reported success while the
# overlay still lists missing specs (the no-op-is-failure rule documented on
# ``repair_agent_specs``). Shared by the main-rebuild and auxiliary arms so the
# remediation command cannot drift between them.
_SPECS_STILL_MISSING_ERROR = (
    "The repair reported success but the specs are still missing. "
    "Run `kirocrew setup --agent-only --clean` on the gateway host."
)


#: Marker identifying a kiro-cli agent-spec REJECTION in the probe's captured
#: output, as opposed to any other reason the probe produced text.
#:
#: ``kiro-cli agent validate`` exits 0 whether or not it accepted the file — the
#: verdict is carried only in the message it writes — so the exit status cannot
#: be used and this match is the whole signal. On acceptance it writes nothing at
#: all; on rejection it writes ``Error: Json supplied at <path> is invalid:
#: <reason>``.
#:
#: Matching the schema-rejection wording rather than "the probe printed
#: something" is deliberate and load-bearing in BOTH directions. A probe that
#: could not read the file (a sandbox denial, a deleted spec) also writes to the
#: captured stream, and reporting that as "kiro-cli rejects your spec" would send
#: the user to rewrite a spec that is fine. Anything unrecognized is therefore
#: treated as acceptance: this module's rule is to report nothing rather than
#: block a working install behind a repair card it cannot clear.
_SPEC_REJECTION_MARKER = "is invalid"


def _default_spec_lister() -> list[tuple[str, Path]]:
    """Production enumerator for :class:`KiroPrerequisiteService`'s spec probe.

    Delegates so path resolution and the ownership guard stay behind ``kiro_crew.agent``
    (see ``present_required_agent_specs`` in ``agent_parts/spec_files.py``).
    """
    from kiro_crew.agent import present_required_agent_specs  # circular import

    return present_required_agent_specs()


def _unlaunchable_mcp_servers(spec_path: Path) -> str:
    """Return why *spec_path* declares an MCP server that cannot start, or ``""``.

    ``kiro-cli agent validate`` is deliberately not the only oracle here, because
    it is looser than the loader on one point that matters: an ``mcpServers`` entry
    that names no transport at all validates clean, and the server is then simply
    absent at runtime. The spec looks accepted while the session lacks the tools it
    declares — the same silent shape this probe exists to remove, reached through a
    different door.

    A server is launchable with EITHER a stdio ``command`` or a remote ``url``,
    matching the contract the custom-MCP handler enforces ("spec needs 'command'
    (stdio) or 'url' (remote)"). Treating a URL-only entry as unlaunchable would be
    far worse than the gap being closed: a valid remote server would force a
    healthy install into an unclearable readiness gate, and the only escape would
    discard the user's MCP tool selections.

    A registry pointer (``"type": "registry"`` and nothing else) is launchable too:
    kiro-cli resolves it by map key against the admin's catalog and supplies the
    transport itself, and the spec rebuild carries such pointers on purpose.
    Reporting one would put every governed host behind this gate.

    Structural only, and narrow on purpose: it reports an entry that names no
    transport whatsoever, or is not an object. It does not judge whether a command
    resolves or a URL answers — those are runtime questions with a different answer
    per host, and they belong to the binary.

    Fails open on an unreadable, oversized or non-JSON file: each is a different
    fault with its own reporting, and guessing here would put a second card on one
    problem. The read goes through the module's bounded helper rather than
    ``read_text`` so a pathologically large spec cannot pull the gateway into an
    OOM through the readiness probe.
    """
    raw = _read_bounded_regular_file(spec_path)
    if raw is None:
        return ""
    try:
        spec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(spec, dict):
        return ""
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return ""
    for name, entry in sorted(servers.items()):
        if not isinstance(entry, dict):
            return (
                f"The MCP server {name!r} is not an object, so Kiro CLI cannot "
                f"start it and the session runs without its tools."
            )
        stdio = entry.get("command")
        remote = entry.get("url")
        launchable = (isinstance(stdio, str) and stdio.strip()) or (
            isinstance(remote, str) and remote.strip()
        )
        if not launchable and not mcp_entry_is_registry_governed(entry):
            return (
                f"The MCP server {name!r} names neither a command to run nor a "
                f"url to reach, so Kiro CLI cannot start it and the session runs "
                f"without its tools."
            )
    return ""


def _kas_spec_load_failures(specs: list[tuple[str, Path]]) -> tuple[list[str], str]:
    """Return the required specs KAS session creation could not load, plus a reason.

    This is KAS's LOADING contract, not full Kiro CLI schema acceptance.
    ``KasHarness.session_extras`` loads the active spec with
    ``kas_agents.load_agent_spec`` and then resolves its prompt through
    ``build_kas_custom_agents`` -> ``resolve_prompt``; either raising
    ``KasAgentTranslationError`` refuses the session. This runs those same two
    steps and nothing else. It stops before ``to_client_custom_agent``, which
    consults the governance ceiling and writes SEL records, and it does not apply
    ``_unlaunchable_mcp_servers``, which KAS does not enforce while loading.

    It exists because ``kiro-cli agent validate`` refuses to run while signed out,
    and a signed-out Kiro CLI is the normal state for KAS. Any other exception is
    treated as acceptance, matching this module's rule not to invent a repair card.
    """
    # circular import: the SDK's spec seam loads the ACP layer, which pulls in
    # agent discovery, governance and SEL.
    from kiro_crew.agent_sdk.spec_hooks import kas_spec_load_error

    rejected: list[str] = []
    detail = ""
    for filename, spec_path in specs:
        try:
            error = kas_spec_load_error(spec_path.parent, Path(filename).stem)
        except Exception:
            logger.debug("Could not check KAS loading for %s", filename, exc_info=True)
            continue
        if error is not None:
            rejected.append(filename)
            if not detail:
                detail = _sanitize_detail(error)
    return rejected, detail


def _missing_agent_specs() -> list[str]:
    """The required agent specs absent from the agents dir (blocking call)."""
    from kiro_crew.agent import missing_required_agent_specs  # circular import

    return missing_required_agent_specs()


async def _agent_spec_overlay(
    self: KiroPrerequisiteService, result: dict[str, Any]
) -> dict[str, Any]:
    """Fold agent-spec presence into a snapshot, narrowing ``ready``.

    Applied on EVERY read, not only on a forced probe. That is affordable
    because it is two ``stat`` calls rather than the probe's two sandboxed
    ``kiro-cli`` spawns, so it does not violate the boot-and-explicit-action
    rule the subprocess probe follows (see :meth:`snapshot`). Off-loop anyway,
    matching this package's discipline of never doing filesystem work inline
    on the event loop.

    Only ever NARROWS: a missing spec turns ``ready`` off and
    ``repair_required`` on; a present spec grants nothing the probe did not
    already grant.

    Deliberately scoped to the dashboard-facing snapshot and NOT to
    :meth:`session_ready`. That predicate gates poll-driven spawn sites, and
    turn-starting paths intentionally do not block on it — they let the turn
    carry the failure, which now arrives as an actionable missing-spec message
    from ``acp/runtime_reader_loop.py`` instead of a raw JSON-RPC dict. Narrowing it here
    would re-introduce the stale-latch lockout that design removed.
    """

    if self._assume_ready:
        # ``assume_ready`` is the deliberate host-reality bypass (test mode,
        # fixtures, offline E2E). Those homes have no managed specs on disk, so
        # applying the overlay there would report a repair-required gate for
        # every such gateway.
        return result
    try:
        missing = await asyncio.to_thread(self._missing_agent_specs)
    except Exception:
        # Unreadable agents dir: report nothing rather than inventing a
        # missing spec and blocking a working install behind a repair card.
        logger.debug("Could not check Kiro Crew agent specs", exc_info=True)
        return result
    result["missing_agent_specs"] = list(missing)
    if missing:
        result["ready"] = False
        result["repair_required"] = True
    return result


async def _repair_agent_specs(self: KiroPrerequisiteService) -> str:
    """Rewrite the managed agent specs. Returns the failure text, or ``""``.

    Reached ONLY from the ``POST /api/kiro-prerequisite/repair-specs`` handler,
    never from ``snapshot()``. That placement is load-bearing, not stylistic:
    the status route is an ``add_get``, and both dashboard barriers are
    method-scoped — ``csrf_middleware`` skips ``check_origin`` for
    ``{GET, HEAD, OPTIONS}`` and ``sel_audit_middleware`` logs only
    ``{POST, PUT, DELETE, PATCH}``. A write hung off the GET would therefore
    be cross-site triggerable (a ``SameSite=Lax`` cookie rides a top-level
    cross-site GET) and would leave no SEL record, while every sibling
    operation in this service — including the read-only probes — audits.

    Only rebuilds when the MAIN spec is absent, which keeps it out of a
    lost-update class: ``rebuild_agent_config`` regenerates the whole file and
    re-merges ``mcpServers`` under ``bridges._mcp_lock``, but NOT the
    ``tools``/``allowedTools`` half ``api_mcp_toggle`` writes separately — so
    rebuilding over an existing spec can drop a concurrent toggle's edit. With
    no file on disk there is nothing to lose (the toggle path itself bails
    with "Cannot read agent config, skipping sync").

    Failure is returned rather than raised: the caller reports it in the
    payload, which is the entire point — the boot path's swallowed exception is
    what made the original install undiagnosable. Sanitized like every other
    dashboard-facing string in this module, because the rebuild's call graph
    merges ``mcpServers[*].env``.
    """

    def _rebuild() -> None:
        from kiro_crew.agent import rebuild_agent_config  # circular import

        rebuild_agent_config()

    try:
        await asyncio.to_thread(_rebuild)
    except Exception as exc:
        logger.error("Agent spec repair from the readiness gate failed", exc_info=True)
        return _sanitize_detail(f"{type(exc).__name__}: {exc}")
    logger.info("Agent specs repaired from the readiness gate")
    return ""


async def _repair_auxiliary_specs(self: KiroPrerequisiteService, missing: list[str]) -> str:
    """Write the missing AUXILIARY required specs. Returns failure text, or ``""``.

    The counterpart to :meth:`_repair_agent_specs` for the required specs
    other than the main one — today only the lite agent spec. Unlike the
    main-spec rebuild, this write is NOT in the lost-update class that
    method's docstring gates on: ``_install_lite_agent_fallback`` is an
    atomic whole-file JSON write, and no ``tools``/``allowedTools`` half is
    toggle-merged into the lite spec, so there is no concurrent edit to
    lose — and the spec is only written here when it is absent anyway.

    An auxiliary name this method does not know how to write is deliberately
    left alone: it stays missing, and the caller's post-repair overlay
    reports it through the still-missing error text instead of a false
    success.
    """

    def _write() -> None:
        from kiro_crew.agent import _install_lite_agent_fallback  # circular import

        if LITE_AGENT_FILENAME in missing:
            _install_lite_agent_fallback()

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error(
            "Auxiliary agent spec repair from the readiness gate failed",
            exc_info=True,
        )
        return _sanitize_detail(f"{type(exc).__name__}: {exc}")
    logger.info("Auxiliary agent specs repaired from the readiness gate")
    return ""


async def repair_agent_specs(self: KiroPrerequisiteService, caller: str = "") -> dict[str, Any]:
    """Repair the managed agent specs, then return the post-repair snapshot.

    The Check again button's repair arm, behind a POST so the write is
    origin-checked and audited (see :meth:`_repair_agent_specs`). Re-reporting
    alone could never help the install this exists to fix: the probe's two
    inputs (binary, ``whoami``) are both already true in that state.

    Returns a snapshot with ``agent_spec_repair_error`` set — empty on success.
    A no-op rebuild is reported as a failure rather than a success: the write
    can decline silently (``_decline_shared_agent_home`` returns early without
    raising), and reporting that as ``""`` would leave the gate showing a
    button that changes nothing on every press.
    """

    del caller  # the SEL record is written by the route's audit middleware
    # The missing-spec check and the rebuild must be ONE critical section. Read
    # outside the lock they are a TOCTOU: two concurrent repairs both observe
    # the spec missing, both rebuild, and the second one regenerates the file
    # the first just wrote. Re-reading inside the lock makes the loser a no-op.
    async with self._repair_lock:
        before = await self._dashboard_snapshot()
        missing_before = before.get("missing_agent_specs") or []
        # Read off the latched probe result because acceptance costs a
        # subprocess and the spec overlay only checks file presence.
        rejected_before = before.get("rejected_agent_specs") or []
        # A REJECTED spec is deliberately NOT rebuilt. The file exists, so a
        # regenerate would drop a concurrent api_mcp_toggle edit to the
        # tools/allowedTools half that rebuild_agent_config does not re-merge
        # (see _repair_agent_specs). Losing a user's tool grants to clear a
        # rejection is a worse outcome than the rejection, and the rebuild
        # cannot be made safe here: it already takes bridges._mcp_lock
        # internally, so wrapping this call in the same lock would deadlock.
        # Re-asking the binary is the honest action for the state, and it is
        # what the card's button offers.
        repairable = AGENT_FILENAME in missing_before
        if not repairable:
            auxiliary_missing = [name for name in missing_before if name != AGENT_FILENAME]
            error = ""
            if auxiliary_missing:
                # Only auxiliary required specs are missing. The main-spec
                # gate above keeps rebuild_agent_config away from a present
                # main spec, but the auxiliary specs have their own writers
                # with no such lost-update class (see
                # _repair_auxiliary_specs), so refusing to write them would
                # leave the gate blocking on a file the button never
                # writes. Runs even when a spec is REJECTED: acceptance is
                # only evaluated for PRESENT specs, so a rejected main spec
                # and a missing lite spec can coexist, and writing a MISSING
                # file rewrites nothing — the lost-update reasoning behind
                # the rejection guard does not apply to it.
                error = await self._repair_auxiliary_specs(auxiliary_missing)
            elif not rejected_before:
                # Nothing to repair: a concurrent repair already wrote the
                # specs. Report, do not write.
                before["agent_spec_repair_error"] = ""
                return before
            if rejected_before:
                # Not a no-op: acceptance is only re-answerable by the binary,
                # and the stat-only overlay cannot ask it. force=True because
                # the cached snapshot inside _PROBE_CACHE_SECS would just echo
                # the rejection this is trying to re-test.
                try:
                    await self._probe(force=True)
                except Exception:  # noqa: BLE001 — stale state beats a 500
                    logger.warning("Re-probe of rejected agent specs failed", exc_info=True)
            result = await self._dashboard_snapshot()
            if not error and auxiliary_missing and (result.get("missing_agent_specs") or []):
                error = _SPECS_STILL_MISSING_ERROR
            result["agent_spec_repair_error"] = error
            return result
        error = await self._repair_agent_specs()
        if not error and AGENT_FILENAME in rejected_before:
            # Acceptance is only re-answerable by the binary, and the overlay
            # cannot ask it. Re-probe so a rewrite that actually fixed the
            # rejection clears the card instead of leaving stale state up.
            # Affordable and in-policy here: this is the explicit-action half
            # of the probe's boot-and-explicit-action budget.
            try:
                await self._probe(force=True)
            except Exception:  # noqa: BLE001 — a failed re-probe keeps stale state, not a 500
                logger.warning("Re-probe after agent-spec repair failed", exc_info=True)
        result = await self._dashboard_snapshot()
        if not error and (result.get("missing_agent_specs") or []):
            error = _SPECS_STILL_MISSING_ERROR
        result["agent_spec_repair_error"] = error
        return result


async def _probe_spec_acceptance(
    self: KiroPrerequisiteService, executable: str
) -> tuple[list[str], str]:
    """Ask kiro-cli whether it accepts each required Kiro Crew spec on disk.

    Returns ``(rejected filenames, first reason)`` — both empty when every
    present spec is accepted.

    Complements :meth:`_agent_spec_overlay`, which answers whether the specs
    EXIST. A spec can be present and still be refused, and a refused spec is
    dropped from kiro-cli's agent table, so the product's own agent resolves
    to the default one with none of its MCP servers. Statting the file cannot
    see that; only the binary can answer it.

    Scoped to :data:`REQUIRED_KIRO_AGENT_FILES` because those are the specs
    whose rejection fails EVERY turn rather than disabling one feature, and to
    files that already exist because absence is ``missing_agent_specs``' job —
    reporting one fault under two names would put two cards on one problem.

    A spawn per spec, so it belongs to the probe's boot-and-explicit-action
    budget and must not be folded into the every-read overlay.
    """

    def _present() -> list[tuple[str, Path]]:
        return self._spec_lister()

    # Enumeration is delegated so path resolution and the ownership guard live
    # in one place (see present_required_agent_specs): an instance that may not
    # own these specs gets an empty list and spawns nothing.
    try:
        specs = await asyncio.to_thread(_present)
    except Exception:
        logger.debug("Could not enumerate Kiro Crew agent specs", exc_info=True)
        return [], ""
    rejected: list[str] = []
    detail = ""
    for filename, spec_path in specs:
        # Checked BEFORE spawning, because `agent validate` does not enforce
        # it: a spec whose mcpServers entry carries no launchable command
        # passes validate with no output at all, yet the server cannot start,
        # so the session runs without the tools the spec promises. Reading the
        # file is also cheaper than a subprocess, so a spec that fails here
        # costs no spawn.
        structural = await asyncio.to_thread(_unlaunchable_mcp_servers, spec_path)
        if structural:
            rejected.append(filename)
            if not detail:
                # Sanitized like every other dashboard-facing string in this
                # module: the text names the MCP server, and a server name is
                # user-supplied, so it can carry a credential.
                detail = _sanitize_detail(structural)
            continue
        result = await self._audited_probe(
            "probe_agent_spec",
            executable,
            ["agent", "validate", "--path", str(spec_path)],
        )
        if _SPEC_REJECTION_MARKER not in (result.output or ""):
            continue
        rejected.append(filename)
        if not detail:
            detail = _sanitize_detail((result.output or "").strip())
    return rejected, detail


async def _probe_kas_spec_loading(self: KiroPrerequisiteService) -> tuple[list[str], str]:
    """Check the present required specs against KAS's local loading contract.

    Spawns nothing. Used when Kiro CLI validation did not run; see
    :func:`_kas_spec_load_failures` for what this does and does not accept.
    """
    try:
        specs = await asyncio.to_thread(self._spec_lister)
    except Exception:
        logger.debug("Could not enumerate Kiro Crew agent specs", exc_info=True)
        return [], ""
    return await asyncio.to_thread(_kas_spec_load_failures, specs)
