"""On-demand minting of a Connections provider's OAuth approval URL.

A mint is one dedicated kiro-cli process running a single-server agent spec. Its
session is created with no prompt and makes no model call: MCP initialization is
eager, so the server's OAuth challenge is buffered during init and readable as
soon as the session is ready.

The process is HELD once the URL is in hand, not disposed. The PKCE verifier is a
value in that process's memory and the loopback listener that answers the
provider's redirect is one of that session's MCP children, so a mint torn down
after the drain leaves a redirect port that accepts a connect and then resets
every real exchange -- consent the provider renders and no paste can redeem. The
hold ends on a terminal condition only: a grant appears on disk, the TTL
expires, a newer mint supersedes this one, or the server initializes without
challenging.

Because the process is held while nothing claims it as a session, its PID is
registered with the orphan-sweep protection set for exactly that span. Without
it the periodic sweep reaps the mint once it ages past the spawn grace, which
takes the verifier and the listener with it and leaves a published URL that can
no longer be redeemed.

INVARIANT: no filesystem operation in this module executes on the event loop.

Every flow here touches disk -- the user's home for a grant, the shared agents
directory for a spec, our own state dir for the manifest -- and any of those can
sit on a network mount, where a stat is unbounded rather than sub-millisecond. On
the loop one such stall takes every other request and the gateway's heartbeat with
it. So the filesystem-touching helpers are all plain synchronous functions, and
every async boundary that needs one runs it through ``asyncio.to_thread``: the
aged-spec sweep, the spec write, the spec removal, the grant read, the agents-dir
read, the data-home resolution, and the SEL audits -- the flow outcome and the
grant-presence observation -- whose first call in a process constructs the
security event log before it can enqueue anything. The
removal is additionally shielded, because it also releases a file that agent
discovery would otherwise offer as a selectable agent -- see :func:`_dispose_mint`.

The invariant is enforced rather than described: a drift guard derives both sides
of it from this module's own AST -- which helpers reach a filesystem primitive,
and what each coroutine calls directly -- so a new helper that touches disk cannot
be called straight from a coroutine without failing the suite.
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

# ``kiro_crew.agent`` is imported as a MODULE, not as symbols: the agents dir and
# the atomic writer are resolved at call time so a test (and the spec-emission
# path) can substitute them. ``from ... import f`` would freeze this module's own
# binding.
from kiro_crew import agent as _agent
from kiro_crew.acp.client import AcpClient
from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.agent_files import AGENT_FILENAME, OWNED_KIRO_AGENT_FILES
from kiro_crew.config.loader import data_home
from kiro_crew.connections.tool_test import _classify as _classify_tool_inventory
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.mcp_grant import grant_fingerprint, grant_observed
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.security import oauth_url_contains_credential
from kiro_crew.sel import sel
from kiro_crew.session_pid import register_protected_pid, unregister_protected_pid

logger = logging.getLogger(__name__)

_MINT_READY_TIMEOUT_SECONDS = 90.0
_MINT_TTL_SECONDS = 600.0
_MINT_GRANT_POLL_SECONDS = 5.0
# How often the watcher's no-baseline fallback may spawn a revalidation. Bounds a
# rare degraded path (the disproof's capture stat failed) to at most one process
# per interval across the mint's TTL, instead of one per grant poll.
_GRANT_REVALIDATION_INTERVAL_SECONDS = 60.0
# Tight, because the window it closes is the one where the orphan sweep can kill a
# still-initializing mint. Cheap: an attribute read, no I/O.
_MINT_PID_CLAIM_POLL_SECONDS = 0.05
_CLIENT_SHUTDOWN_TIMEOUT_SECONDS = 10.0

# Ephemeral single-server specs a mint writes under the kiro agents directory.
# Deliberately absent from the owned-spec allowlist: that is an exact-name set
# for long-lived managed specs a convergence sweep rewrites, and a mint spec must
# be deleted by its writer rather than rewritten by a sweep.
_MINT_AGENT_PREFIX = "kirocrew-mint-"
_MAIN_AGENT_NAME = "kirocrew"
# A spec this old cannot belong to a live mint: every mint is released by its TTL
# at the latest, so anything past TTL plus a margin was stranded by a hard kill.
_MINT_SPEC_ORPHAN_SECONDS = _MINT_TTL_SECONDS * 2
# Serializes this process's manifest read-modify-writes.
_MINT_MANIFEST_LOCK = threading.Lock()


class MintState(TypedDict, total=False):
    """A mint's row. The holdings are released on teardown, never served."""

    state: str  # minting | waiting | granted | failed | expired
    oauth_url: str
    reason: str
    started: float
    token: str  # row identity; see _new_mint_token
    client: Any
    watcher: Any
    agent: str  # ephemeral spec name
    spec_path: str  # the exact file this flow wrote, and the only one it deletes
    pid: int  # sweep-protected for as long as the process is held
    # Set only by the warm table (:mod:`kiro_crew.connections.warm`). A shared row
    # owns no ``client``: its URL was minted on a process it shares with every
    # other card, so redeemability is judged by generation AND activation liveness
    # instead. Declared here because the table itself is shared, and a row type
    # that cannot describe half its rows pushes every read through a cast.
    shared: bool
    generation: int  # the shared process that holds this row's PKCE verifier
    activation: int  # the session that owns this row's loopback listener


_mints: dict[str, MintState] = {}
_mints_lock = LoopBoundLock()


def _new_mint_token() -> str:
    """A fresh row identity, unique across restarts as well as within one process.

    Deliberately NOT a clock reading: ``time.monotonic()`` has ~15.6ms granularity
    on Windows, so two Connects for one provider inside the same tick would read as
    the same row and every token guard would fail open. Deliberately not a counter
    either: a counter restarts at the same value with the gateway, so a tab holding
    a pre-restart token can match a post-restart row and act on a flow it never
    started.
    """
    return uuid4().hex


def _acp_client_factory() -> Any:
    """Indirection so tests can substitute a fake client class."""
    return AcpClient


def _mint_spec_name(alias: str) -> str:
    """A spec name no concurrent flow can collide with.

    The pid and random token make the name unique per flow, so two cards acting at
    once cannot land on the same file. The name is NOT what authorizes a delete --
    see the cleanup decision table below.
    """
    return f"{_MINT_AGENT_PREFIX}{alias}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


# ── Spec cleanup: an INTERSECTION, not a single test ──
#
# A PATH MAY BE UNLINKED ONLY IF EVERY CONJUNCT BELOW HOLDS.
#
# The agents directory is shared with the user and with sibling gateways, and it
# is enumerated by ``list_agents``, so anything left there shows up as a
# selectable agent. Both failure directions are therefore real: deleting a file we
# did not create is destructive, and leaking one is user-visible clutter. No
# single test separates them, so cleanup takes the intersection of five -- one per
# attack family, each independently sufficient to refuse:
#
#   | # | conjunct                              | the attack it stops              |
#   |---|---------------------------------------|----------------------------------|
#   | 1 | the path is a recorded manifest row   | an AGED USER FILE that happens   |
#   |   |                                       | to look like one of ours         |
#   | 2 | the path's LEXICAL parent is exactly  | a PLANTED ROW naming a victim    |
#   |   | the kiro agents dir -- no resolve()   | elsewhere, ``../`` TRAVERSAL,    |
#   |   |                                       | and a symlinked PARENT component |
#   | 3 | the leaf is not a symlink             | a link whose name and target     |
#   |   |                                       | disagree, re-pointable after the |
#   |   |                                       | check and before the unlink      |
#   | 4 | the filename matches this module's    | a planted row naming a REAL      |
#   |   | own mint name shape                   | agent spec in that same dir      |
#   | 5 | the name is not in the owned-spec     | a planted row naming one of      |
#   |   | allowlist                             | OUR OWN long-lived managed specs |
#
# Conjunct 2 is judged LEXICALLY on purpose. ``resolve()`` would answer for a
# different path than ``unlink`` acts on, and a link anywhere in the recorded path
# can be re-pointed between the two -- so the check would not govern the delete.
#
# Conjunct 1 alone was the original invariant; conjuncts 2-5 are defense-in-depth
# against a writer that already holds the gateway state dir. Worth having anyway:
# they are cheap, and each closes a distinct family rather than narrowing a
# pattern.
#
# Release (a mint ending) deletes the one path its own entry recorded and needs no
# conjuncts -- that path came from our own write, in this process, moments ago.
#
# The failure mode is a lost record (two processes racing the manifest write):
# that spec is never reaped and survives as inert clutter. Losing a record is the
# safe direction, which is why this is a plain atomic write rather than a
# cross-process lock.
_MINT_MANIFEST_NAME = "mint-specs.json"
_MINT_NAME_RE = re.compile(
    rf"^{re.escape(_MINT_AGENT_PREFIX)}[a-z0-9_.-]+-\d+-[0-9a-f]{{8}}\.json$"
)


def _is_reapable_spec(recorded: str) -> bool:
    """Whether ``recorded`` satisfies every conjunct of the cleanup intersection.

    Conjunct 1 (manifest membership) is the caller's -- only recorded rows reach
    here. This function is conjuncts 2-5, and it fails closed: any resolution or
    stat error is a refusal, never a delete.

    Every conjunct is judged on the LEXICAL path, because that is the path
    ``unlink`` acts on. Resolving first would judge a different path than the one
    deleted: a symlink anywhere in the recorded path -- leaf OR parent component --
    makes the resolved form agree while the delete follows the link, and the link
    can be re-pointed between the check and the act.
    """
    lexical = Path(recorded)
    # 2: lexically inside the configured agents dir. No resolve(): the writer
    # records exactly ``agents_dir / name``, so the lexical form is what we wrote.
    # The agents dir is absolute, so this also rejects a relative row (whose parent
    # is ".") and a ``..`` traversal (PurePath keeps ".." components).
    try:
        agents_dir = _agent.kiro_agents_dir_path()
    except OSError:
        return False
    if lexical.parent != agents_dir:
        return False
    # 3: never follow a link. A symlink's name and its target disagree by
    # construction, so there is no way to check one and act on the other safely.
    try:
        if lexical.is_symlink():
            return False
    except OSError:
        return False
    # 4: our own name shape, on the path being unlinked -- a planted row naming a
    # real spec in that same directory does not match.
    if not _MINT_NAME_RE.match(lexical.name):
        return False
    # 5: never one of our long-lived managed specs.
    return lexical.name not in OWNED_KIRO_AGENT_FILES


def _mint_manifest_path() -> Path:
    """Where the created-spec manifest lives: our own state dir, not the user's."""
    return data_home() / "connections" / _MINT_MANIFEST_NAME


def _read_mint_manifest() -> dict[str, float]:
    """Recorded spec paths -> creation time. Unreadable or malformed reads as empty."""
    try:
        raw = json.loads(_mint_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))}


def _write_mint_manifest(rows: dict[str, float]) -> bool:
    """Persist the manifest. Returns False when the row set did NOT reach disk.

    The caller has to know: a spec file whose row never landed is invisible to the
    sweep forever, and agent discovery globs every JSON in the agents dir -- so it
    would stay both unreapable and selectable.
    """
    path = _mint_manifest_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _agent._atomic_json_write(path, dict(rows))
    except OSError:
        logger.debug("mint spec manifest write failed", exc_info=True)
        return False
    return True


def _sweep_mint_specs() -> None:
    """Unlink the specs whose rows are too old to belong to a live mint.

    Runs at the top of every mint attempt, not only when a dedicated spec is
    written: a reconnect that finds an existing grant short-circuits before any
    write, so a row that was still too young at its last sweep would otherwise
    never be looked at again.

    A row is dropped only once its file is confirmed gone. Keeping a row whose
    unlink failed costs one manifest entry and buys a retry; dropping it would
    abandon a real file that nothing else can authorize deleting.
    """
    cutoff = time.time() - _MINT_SPEC_ORPHAN_SECONDS
    with _MINT_MANIFEST_LOCK:
        rows = _read_mint_manifest()
        changed = False
        for recorded, created in list(rows.items()):
            if created >= cutoff:
                continue
            if not _is_reapable_spec(recorded):
                # Drop the row so a poisoned or stale entry cannot accumulate, but
                # never touch the file it names.
                rows.pop(recorded, None)
                changed = True
                logger.debug("mint manifest row is not reapable; dropping the row only")
                continue
            try:
                # ``missing_ok`` is what makes a row naming an absent file harmless:
                # the conjuncts are lexical, so such a row still reaches here, the
                # unlink is a no-op, and the row is dropped as ordinary bookkeeping.
                Path(recorded).unlink(missing_ok=True)
            except OSError:
                # Keep the row: the file is still there and the next sweep retries.
                logger.debug("stranded mint spec removal failed", exc_info=True)
                continue
            rows.pop(recorded, None)
            changed = True
        if changed:
            _write_mint_manifest(rows)


def _record_mint_spec(spec_path: str) -> bool:
    """Record the intent to create a spec. Returns False if the row missed disk."""
    with _MINT_MANIFEST_LOCK:
        rows = _read_mint_manifest()
        rows[spec_path] = time.time()
        return _write_mint_manifest(rows)


def _forget_mint_spec(spec_path: str) -> None:
    """Drop a released spec's row so the manifest tracks only live files."""
    with _MINT_MANIFEST_LOCK:
        rows = _read_mint_manifest()
        if rows.pop(spec_path, None) is not None:
            _write_mint_manifest(rows)


def _mint_spec_body(name: str, servers: dict[str, Any], description: str) -> dict[str, Any]:
    """The agent spec a mint activates: the given servers and nothing else.

    ``servers`` arrives in kiro-cli's wire shape, because it is read back out of
    the emitted managed spec and that is where the translation already happened.
    The entries are therefore copied verbatim rather than re-derived.
    """
    wire_servers = {alias: dict(entry) for alias, entry in servers.items()}
    return {
        "name": name,
        "description": description,
        # The mint is promptless and makes zero model calls, so inheriting the
        # user's configured model would pin a spec nothing infers with.
        "model": "auto",
        # The global mcp.json would re-add every other server behind our back.
        "includeMcpJson": False,
        "prompt": "",
        "mcpServers": wire_servers,
        # ``tools`` is a CLOSED allowlist (no wildcard) and is what MOUNTS the
        # servers, so every entry has to be listed. ``allowedTools`` stays empty:
        # the mint issues no prompt and calls no tool, so it must never carry an
        # auto-approve grant.
        "tools": [f"@{alias}" for alias in wire_servers],
        "allowedTools": [],
    }


def _write_mint_agent_spec(slug: str) -> tuple[str, str]:
    """Write a one-server spec for ``slug``'s mint; return its name and path.

    The mint needs exactly ONE server to answer: the one being connected.
    Pointing it at the shared agent makes kiro-cli initialize every configured
    server first -- a stdio child per entry, seconds of work with no bearing on
    this provider's challenge, all of it on the path the card waits on.

    Falls back to the main agent name when ``slug`` has no entry in the main spec
    yet, so a mint never ends up with FEWER servers than it needs. A main spec
    that exists but is REFUSED by the hardened reader raises ``OSError`` instead:
    the fallback would hand the refused file to the spawned child to reload.
    """
    agents_dir = _agent.kiro_agents_dir_path()
    alias = mcp_server_alias(slug)
    # Hardened reader (#6736). A REFUSED main spec (oversize, sensitive symlink,
    # non-object) must NOT reach the main-agent fallback: that fallback spawns
    # ``kiro-cli --agent kirocrew``, and the child would reload the very file the
    # gateway just refused to read -- uncapped and unguarded. Raising instead
    # lands in the mint flow's failure path (retryable ``failed`` row, holdings
    # disposed, no child spawned). The fallback below stays reserved for what it
    # always meant: the file or the alias entry being genuinely absent.
    spec = _read_agent_spec(
        agents_dir / AGENT_FILENAME,
        operation="connections_mint",
        source="dashboard",
    )
    if spec is None:
        if (agents_dir / AGENT_FILENAME).exists():
            logger.warning("Main agent spec unusable; refusing to mint for %r", alias)
            raise OSError("main agent spec unusable")
        spec = {}
    entry = (spec.get("mcpServers") or {}).get(alias)
    if not isinstance(entry, dict):
        logger.debug("No %r entry in the main agent spec; minting with %r", alias, _MAIN_AGENT_NAME)
        return _MAIN_AGENT_NAME, ""

    name = _mint_spec_name(alias)
    path = agents_dir / f"{name}.json"
    if path.exists():
        # Unreachable for a unique name, and a refusal rather than a clobber is
        # the only safe answer if it ever is reachable.
        raise FileExistsError(f"mint spec {name} already exists")
    # The row goes down FIRST, as a durable statement of intent. The two failure
    # shapes are not symmetric: a row naming a file that does not exist is inert
    # bookkeeping the next sweep drops, while a file with no row is invisible to
    # the sweep forever AND selectable by agent discovery, which globs every JSON
    # in the agents dir. So the manifest deliberately MAY name a nonexistent file;
    # what it must never do is miss one that exists.
    if not _record_mint_spec(str(path)):
        raise OSError(f"could not record mint spec {name}")
    _agent._atomic_json_write(
        path,
        _mint_spec_body(name, {alias: entry}, f"Ephemeral OAuth approval-URL mint for {alias}."),
    )
    return name, str(path)


def _remove_mint_agent_spec(spec_path: str) -> None:
    """Delete the one spec file this flow created, and drop its manifest row.

    An exact path, never a pattern: the caller is the only party that can know
    which file is its own, and the manifest is the only thing that authorizes a
    delete this flow did not perform itself.

    Blocking: an unlink plus a manifest read-modify-write. Async callers go through
    ``asyncio.to_thread``.
    """
    if not spec_path:
        return
    try:
        Path(spec_path).unlink(missing_ok=True)
    except OSError:
        # Keep the row. It is the only thing that authorizes deleting this file, so
        # dropping it here would strand a real spec that no later sweep can see --
        # the same rule the aged-row sweep follows.
        logger.debug("mint agent spec removal failed; keeping the row for a retry", exc_info=True)
        return
    _forget_mint_spec(spec_path)


async def _shutdown_quietly(client: Any) -> None:
    """Best-effort teardown of a mint's own child process."""
    try:
        await asyncio.wait_for(client.shutdown(), timeout=_CLIENT_SHUTDOWN_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — teardown of our own child; never raises into a caller
        logger.debug("OAuth mint client shutdown failed", exc_info=True)


async def _dispose_mint(entry: MintState) -> None:
    """Release one mint's holdings: its watcher, its process, its PID, its spec.

    Safe to call FROM the watcher. Cancelling the calling task would deliver the
    cancellation at the first await inside the client teardown, abandoning it
    half-done and leaking the process tree and the loopback listener -- so the
    watcher is cancelled only when it is somebody else.
    """
    client = entry.pop("client", None)
    watcher = entry.pop("watcher", None)
    spec_path = entry.pop("spec_path", "")
    pid = entry.pop("pid", 0)
    entry.pop("agent", "")
    if watcher is not None and watcher is not asyncio.current_task():
        watcher.cancel()
    try:
        if client is not None:
            await _shutdown_quietly(client)
    finally:
        # The spec and the PID registration are released even if the teardown
        # above is cancelled from outside, so neither can outlive the row.
        if pid:
            unregister_protected_pid(int(pid))
        # Off the loop: the removal unlinks a file and rewrites the manifest.
        # Shielded because this is the one off-loop hop that sits in a finally
        # reached under cancellation: the promise above is that the spec is released
        # even when the teardown is cancelled from outside, and a bare await here
        # would let a cancellation land before the worker is handed the job --
        # stranding a released spec that agent discovery still offers as selectable.
        # The shield lets the cancellation reach the caller while the removal runs.
        await asyncio.shield(asyncio.to_thread(_remove_mint_agent_spec, spec_path))


async def _grant_change_proven(
    slug: str,
    mcp_url: str,
    baseline: tuple[int, int] | None,
    last_revalidation: list[float],
) -> bool:
    """Whether the credential can be POSITIVELY proven to work now. Fail-closed.

    Reached only for an attempt whose pre-existing grant a validation already
    disproved, so presence carries no information: the disproven pair is still on
    disk and answers "present" exactly as a fresh one would.

    Two admissible proofs, and nothing else:

    * the token artifact CHANGED against the baseline captured at disproof time --
      completing the exchange makes kiro-cli rewrite it, so a different
      ``(mtime_ns, size)`` is observable evidence of a new credential;
    * a fresh authenticated validation returns a proven verdict, used only when no
      baseline exists (the capture stat failed), because change cannot be observed
      without one.

    The revalidation fallback is RATE-LIMITED rather than tried once, and the
    difference matters: the disproven pair is on disk from the start, so presence
    is true on the first tick and a once-only attempt would always land before the
    user could finish consenting -- spending the one spawn and then degrading to
    never-grant for the rest of the TTL. ``last_revalidation`` is a one-slot ledger
    the caller owns, so at most one spawn per
    ``_GRANT_REVALIDATION_INTERVAL_SECONDS`` occurs on this rare degraded path
    while a consent completed later can still resolve.

    An unreadable current stat is False, and so is an unproven revalidation:
    "could not look" is not proof, and this predicate is the last thing standing
    between a stale pair and a Connected badge.
    """
    current = await asyncio.to_thread(grant_fingerprint, mcp_url)
    if baseline is not None:
        return current is not None and current != baseline
    now = time.monotonic()
    if last_revalidation and now - last_revalidation[-1] < _GRANT_REVALIDATION_INTERVAL_SECONDS:
        return False
    last_revalidation.append(now)
    logger.info("OAuth mint for %r has no grant baseline; revalidating before granting", slug)
    return await _validate_existing_grant(slug, mcp_url)


async def _mint_watcher(
    slug: str,
    mcp_url: str,
    token: str,
    require_proof: bool = False,
    baseline: tuple[int, int] | None = None,
) -> None:
    """Hold the mint until consent completes or the TTL expires.

    ``token`` identifies the row this watcher belongs to. A superseding mint
    replaces the row, so every write re-checks the token rather than trusting
    that the slug still names the same flow.

    ``require_proof`` says a validation DISPROVED the grant that was on disk when
    this attempt started, and ``baseline`` is that artifact's fingerprint if it
    could be read. Presence alone cannot answer this watcher's question once that
    is known: nothing deletes a credential on the strength of one failed check, so
    the disproven pair is still on disk and a bare ``grant_observed`` would see it
    on the FIRST tick, five seconds in, flip the row to ``granted`` and dispose the
    process holding the PKCE verifier and the loopback listener. That is the very
    lie this flow exists to prevent, plus a consent URL that can no longer be
    redeemed. ``require_proof`` is carried SEPARATELY from ``baseline`` on purpose:
    an unreadable capture stat yields no baseline, and inferring "no disproof" from
    that absence is what let the resurrection path reopen.
    """
    revalidation: list[float] = []
    try:
        deadline = time.monotonic() + _MINT_TTL_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(_MINT_GRANT_POLL_SECONDS)
            if not await grant_observed(mcp_url):
                continue
            if require_proof and not await _grant_change_proven(
                slug, mcp_url, baseline, revalidation
            ):
                continue
            doomed: MintState | None = None
            async with _mints_lock:
                entry = _mints.get(slug)
                if entry is not None and entry.get("token") == token:
                    entry["state"] = "granted"
                    # Captured under the lock, released after it: a wedged
                    # teardown waits on a process shutdown, and holding the
                    # table that long stalls Connect for every provider.
                    doomed = entry
            if doomed is not None:
                await _dispose_mint(doomed)
            logger.info("OAuth mint for %r completed (grant present)", slug)
            return
        doomed = None
        async with _mints_lock:
            entry = _mints.get(slug)
            if (
                entry is not None
                and entry.get("token") == token
                and entry.get("state") == "waiting"
            ):
                entry["state"] = "expired"
                doomed = entry
        if doomed is not None:
            await _dispose_mint(doomed)
        logger.info("OAuth mint for %r expired after %.0fs", slug, _MINT_TTL_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — watcher must never take the gateway down
        logger.debug("OAuth mint watcher failed [%s]", slug, exc_info=True)


def _log_mint_outcome(slug: str, outcome: str, detail: str) -> None:
    """Record a mint's outcome. Never carries a URL or an exception message.

    Blocking on FIRST use: the append itself is queued to SEL's writer thread, but
    the first ``sel()`` of a process constructs the log -- trust-dir creation, key
    validation, a backward scan of the existing log, and on Windows the owner-only
    DACL on the key file. Async callers go through ``asyncio.to_thread``.
    """
    sel().log_api_access(
        caller="dashboard",
        operation="connections_oauth_mint",
        outcome=outcome,
        source="dashboard",
        resources=f"provider:{slug} {detail}",
    )


async def reserve_mint_row(slug: str) -> tuple[str, MintState | None]:
    """Install this attempt's row and return its token plus the row it replaced.

    The caller must do this BEFORE it answers the tab, because the answer names a
    row the tab will immediately poll. Allocating only a token and letting the
    background flow install the row later leaves a window in which the poll sees
    the PREVIOUS row: if that one is terminal the card reads it as the verdict on
    the new attempt and clears the wait for good, so the URL never lands.

    Returns the displaced entry rather than disposing it, because disposal waits on
    a process shutdown and the caller must not hold the table -- or the tab -- for
    that.
    """
    async with _mints_lock:
        prior = _mints.pop(slug, None)
        token = _new_mint_token()
        _mints[slug] = {"state": "minting", "started": time.monotonic(), "token": token}
    return token, prior


async def cancel_mint(slug: str, token: str | None = None) -> bool:
    """Withdraw ``slug``'s in-flight mint, releasing the process it holds.

    Returns True when a row was dropped. The card calls this on Cancel: without
    it a cancelled mint's dedicated kiro-cli process, its loopback listener and
    its ephemeral spec stay held until the TTL expires or a later Connect
    supersedes the row -- a real leak for a flow the user just abandoned.

    ``token`` fences a stale tab. The table is keyed by slug, so a sibling tab
    connecting the same provider REPLACES the row; a cancel carrying the caller's
    own row token refuses to dispose a row that is no longer theirs. A cancel
    with no token disposes whatever row is current -- a caller that never held a
    token cannot distinguish rows, so its intent is only "cancel this provider".

    Disposal runs OUTSIDE the table lock, for the reason ``reserve_mint_row``
    hands its displaced row back rather than disposing under the lock: a wedged
    teardown waits up to the shutdown timeout, and holding the table that long
    stalls Connect for every other provider.
    """
    async with _mints_lock:
        entry = _mints.get(slug)
        if entry is None:
            return False
        if token is not None and entry.get("token") != token:
            return False
        _mints.pop(slug, None)
        doomed = entry
    await _dispose_mint(doomed)
    await asyncio.to_thread(_log_mint_outcome, slug, "ok", "reason=cancelled")
    return True


def _claim_mint_pid(client: Any, holdings: MintState) -> bool:
    """Shield the mint's child PID from the orphan sweep. Idempotent.

    Returns True once a PID has been claimed. False means the spawn has not
    assigned one yet, so there is nothing to protect.
    """
    if holdings.get("pid"):
        return True
    pid = getattr(client, "_pid", 0)
    if not isinstance(pid, int) or pid <= 0:
        return False
    register_protected_pid(pid)
    holdings["pid"] = pid
    return True


async def _claim_mint_pid_when_spawned(client: Any, holdings: MintState) -> None:
    """Poll until the spawn assigns a PID, then protect it and stop."""
    while not _claim_mint_pid(client, holdings):
        await asyncio.sleep(_MINT_PID_CLAIM_POLL_SECONDS)


_MINT_URL_REJECTION_ATTEMPTS = 2

# The two native commands the validation asks. Bounded together with readiness by
# ONE ``_MINT_READY_TIMEOUT_SECONDS`` deadline: each command carries its own
# independent transport timeout, so bounding only readiness would let a provider
# that answers slowly three times over hold Connect in ``minting`` for the sum of
# all three waits rather than the one budget the card is told to expect.
_GRANT_VALIDATION_COMMANDS = ("/mcp", "/tools")

#: Verdicts that PROVE kiro-cli's stored grant still works. ``no_tools`` belongs
#: here with ``usable``: it is reported only when the MCP server reached status
#: ``running``, which for an OAuth provider means kiro-cli authenticated and
#: completed ``tools/list`` with the stored bearer. A server that exposes no tools
#: is a provider-prerequisite or agent-exposure question, NOT a dead credential,
#: and treating it as unproven would send a user with a perfectly good grant to a
#: consent page that cannot fix what they actually have.
_GRANT_PROVEN_VERDICTS = frozenset({"usable", "no_tools"})


async def _validate_existing_grant(slug: str, mcp_url: str) -> bool:
    """Prove an on-disk grant is actually usable before a mint reports ``granted``.

    ``grant_observed`` answers only "does the artifact PAIR exist" -- true for a
    stale or provider-revoked pair as much as a live one, because a remote revoke
    never touches the local file kiro-cli wrote. Reporting the card connected on
    that alone is how a Connect click on Stripe or Vercel flipped instantly to
    Connected, then fell to "not authorized" once the expensive Test action forced
    kiro-cli's own refresh and the pair turned out to be dead.

    This spawns the SAME single-server ephemeral session a fresh mint would spawn
    (see :func:`_write_mint_agent_spec`) and reads kiro-cli's native ``/mcp`` and
    ``/tools`` results the way :func:`kiro_crew.connections.tool_test.test_connection_tools`
    does for the explicit Test button -- promptless, no model call, one process.
    Unlike Test, this call owns its OWN process rather than reaching for the
    shared ``kirocrew`` agent: mounting every configured server to check one
    would turn a bounded reconnect into the cost of a cold start elsewhere, and a
    single-server spec is exactly what a fresh mint attempt would spawn anyway if
    this validation finds the grant unusable.

    Returns True only for a verdict in :data:`_GRANT_PROVEN_VERDICTS`. A False here
    does not assert the grant is GONE -- it asserts it could not be proven to work
    right now -- which is why the caller's answer is a fresh consent mint rather
    than deleting the credential: a transient provider outage or an unreachable
    kiro-cli would otherwise destroy a live refresh token that is not recoverable
    locally.

    Bounded to one attempt inside one deadline: this is a reconnect confirming a
    grant that is supposed to already work, not a retry loop.

    Never raises. EVERY step lives inside the guarded block, including the client
    factory and the spec write -- ``_write_mint_agent_spec`` raises ``OSError`` on
    an unusable main spec or an unrecordable manifest row, and this runs on a
    fire-and-forget task BEFORE the caller's own try, so an escape would kill the
    flow and strand the row at ``minting`` with no terminal state for the card to
    read. ``holdings`` is therefore initialized before the try, so the ``finally``
    can release whatever had been taken when the raise landed.
    """
    holdings: MintState = {}
    try:
        acp_client_cls = _acp_client_factory()
        mint_work_dir = await asyncio.to_thread(data_home)
        agent_name, spec_path = await asyncio.to_thread(_write_mint_agent_spec, slug)
        holdings["agent"] = agent_name
        holdings["spec_path"] = spec_path
        client = acp_client_cls(
            work_dir=mint_work_dir / "connections" / "mint",
            model="auto",
            agent=agent_name,
            sandbox_mode="auto",
            session_key=f"connections-mint-validate-{slug}",
        )
        holdings["client"] = client
        claim = asyncio.get_running_loop().create_task(
            _claim_mint_pid_when_spawned(client, holdings)
        )

        async def _ready_and_read() -> tuple[dict[str, Any], ...]:
            await client.ensure_ready()
            return tuple([await client.command_result(cmd) for cmd in _GRANT_VALIDATION_COMMANDS])

        try:
            # ONE deadline over readiness AND both commands, so the whole
            # validation costs at most what a cold mint spawn is already allowed.
            results = await asyncio.wait_for(_ready_and_read(), timeout=_MINT_READY_TIMEOUT_SECONDS)
        finally:
            claim.cancel()
            _claim_mint_pid(client, holdings)
    except Exception:  # noqa: BLE001 — validation must never raise into the mint flow
        logger.debug("Grant validation for %r could not complete", slug, exc_info=True)
        return False
    finally:
        await _dispose_mint(holdings)

    verdict = _classify_tool_inventory(results, slug, mcp_server_alias(slug))
    return verdict["verdict"] in _GRANT_PROVEN_VERDICTS


async def start_oauth_mint(
    slug: str,
    mcp_url: str,
    token: str | None = None,
    prior: MintState | None = None,
) -> None:
    """Mint ``slug``'s approval URL on a dedicated promptless session.

    Fire-and-forget. Never raises: failures are recorded in the mint table and
    surface on the card as a coarse reason code. A URL rejected by the credential
    guard gets one fresh process and OAuth state before rejection becomes terminal.

    ``token``/``prior`` come from :func:`reserve_mint_row` when a caller already
    made the row visible; without them this installs its own row.
    """
    if token is None:
        my_token, prior = await reserve_mint_row(slug)
    else:
        my_token = token
    if prior is not None:
        # Outside the lock: a wedged teardown takes up to the shutdown timeout,
        # and holding the table would stall Connect for every other provider.
        await _dispose_mint(prior)

    # Before the short-circuit below, so a reconnect that needs no URL still reaps
    # aged orphans. Off-loop: it reads and rewrites the manifest, and may unlink.
    await asyncio.to_thread(_sweep_mint_specs)

    # Whether a validation DISPROVED the pair that was on disk when this attempt
    # started, tracked SEPARATELY from the fingerprint below. The fingerprint can
    # legitimately be ``None`` (the capture stat failed), and inferring "no
    # disproof" from that absence is what would let the pair reassert itself: the
    # watcher's proof requirement keys on this flag, never on the fingerprint's
    # presence.
    validation_failed = False
    # The disproven artifact's identity when it could be read. ``None`` means the
    # stat failed, NOT that nothing was disproved -- the watcher then demands a
    # positive revalidation instead of a change it cannot observe.
    disproven_grant: tuple[int, int] | None = None

    if await grant_observed(mcp_url):
        # An artifact PAIR on disk is not proof the grant still works: it survives
        # a provider-side revoke and a stale refresh token exactly as it survives
        # a live one, because nothing here has asked kiro-cli to actually use it
        # yet. Reporting `granted` on presence alone is what let Connect flip a
        # dead pair to Connected and then fall to "not authorized" once the
        # explicit Test action forced the real check. Validate BEFORE claiming
        # anything, and fall through to a fresh mint on anything short of a
        # proven-working verdict -- the spawn loop below is exactly what a
        # user expects Connect to do when consent still needs proving.
        if await _validate_existing_grant(slug, mcp_url):
            async with _mints_lock:
                if _mints.get(slug, {}).get("token") == my_token:
                    _mints[slug] = {
                        "state": "granted",
                        "started": time.monotonic(),
                        "token": my_token,
                    }
            # Every POST logs outcome=started, so every path has to log a
            # completion or the audit trail shows starts that never finished.
            await asyncio.to_thread(
                _log_mint_outcome,
                slug,
                "ok",
                "reason=validated_grant",
            )
            return
        # Set BEFORE the stat, so a failing stat cannot erase the disproof.
        validation_failed = True
        # Read AFTER the verdict, so it fingerprints the artifact the validation
        # actually disproved. Nothing is deleted: one failed check can be a
        # provider outage or an unreachable runtime, and a refresh token destroyed
        # on that evidence is not recoverable locally. The fingerprint is what lets
        # the flow distrust the pair without removing it.
        disproven_grant = await asyncio.to_thread(grant_fingerprint, mcp_url)
        logger.info("OAuth mint for %r found an unproven grant on disk; minting fresh", slug)

    # Accumulates what this flow owns, so every exit path releases all of it.
    holdings: MintState = {}
    try:
        # Off the loop: resolving the data home creates it when a KIROCREW_HOME
        # override is in play, so this is a write and not merely a path join.
        mint_work_dir = await asyncio.to_thread(data_home)
        oauth_url = ""
        for attempt in range(_MINT_URL_REJECTION_ATTEMPTS):
            acp_client_cls = _acp_client_factory()
            # One server, not all of them: see _write_mint_agent_spec.
            agent_name, spec_path = await asyncio.to_thread(_write_mint_agent_spec, slug)
            holdings["agent"] = agent_name
            holdings["spec_path"] = spec_path
            client = acp_client_cls(
                work_dir=mint_work_dir / "connections" / "mint",
                model="auto",
                agent=agent_name,
                sandbox_mode="auto",
                session_key=f"connections-mint-{slug}",
            )
            holdings["client"] = client
            # session/new with NO prompt: MCP init is eager, so the challenge
            # buffered during init is available right after ready.
            #
            # The PID claim races readiness on purpose. The child is spawned partway
            # THROUGH ensure_ready, and nothing claims it as a session, so the orphan
            # sweep reaps it once it ages past the spawn grace. Waiting for readiness
            # would leave that whole initialization window -- up to the readiness
            # timeout -- open to the sweep killing a mint that is still starting.
            claim = asyncio.get_running_loop().create_task(
                _claim_mint_pid_when_spawned(client, holdings)
            )
            try:
                await asyncio.wait_for(client.ensure_ready(), timeout=_MINT_READY_TIMEOUT_SECONDS)
            finally:
                claim.cancel()
                # Covers the fast path, where readiness beat the poller's first tick,
                # AND the failure path, so a spawned child is always released.
                _claim_mint_pid(client, holdings)

            oauth_url = ""
            for req in client.pop_pending_oauth_requests():
                if req.get("serverName") == slug and req.get("oauthUrl"):
                    oauth_url = str(req["oauthUrl"])
                    break
            credential_bearing = (
                await asyncio.to_thread(oauth_url_contains_credential, oauth_url)
                if oauth_url
                else False
            )
            if not credential_bearing:
                break

            # The same predicate the chat consent path applies before surfacing a
            # banner. The value is never logged or recorded.
            logger.warning("OAuth mint for %r produced a URL with a credential pattern", slug)
            await _dispose_mint(holdings)
            holdings = {}
            if attempt + 1 < _MINT_URL_REJECTION_ATTEMPTS:
                continue

            async with _mints_lock:
                if _mints.get(slug, {}).get("token") == my_token:
                    _mints[slug] = {
                        "state": "failed",
                        "reason": "mint_url_rejected",
                        "started": time.monotonic(),
                        "token": my_token,
                    }
            await asyncio.to_thread(_log_mint_outcome, slug, "error", "reason=mint_url_rejected")
            return

        # Read before the lock: it stats the rendered spec on disk and does not
        # depend on table state, so it has no business inside the critical section.
        # Only the no-challenge branch consults it, so the common path pays nothing
        # -- neither the read nor the thread hop.
        entry_missing = (
            await asyncio.to_thread(_agent_spec_entry_missing, slug) if not oauth_url else False
        )
        superseded = False

        async with _mints_lock:
            entry = _mints.get(slug)
            if entry is None or entry.get("token") != my_token:
                # Superseded by a newer mint while this one was connecting.
                superseded = True
            elif oauth_url:
                entry.update(holdings)
                entry.update(
                    {
                        "state": "waiting",
                        "oauth_url": oauth_url,
                        "watcher": asyncio.get_running_loop().create_task(
                            _mint_watcher(
                                slug,
                                mcp_url,
                                my_token,
                                validation_failed,
                                disproven_grant,
                            )
                        ),
                    }
                )
                holdings = {}
            else:
                # No challenge means one of THREE things, and they are not the same
                # outcome. An open endpoint (or a grant that landed concurrently)
                # is genuinely granted. A slug with no entry left to initialize
                # produced no challenge because there was nothing to challenge --
                # reporting THAT as granted shows a connected card with no server
                # behind it and no way back, so it has to be a retryable failure.
                # And when a validation already DISPROVED the stored grant, an
                # absence of challenge is not proof either: publishing `granted`
                # from it would republish the exact authorization this attempt just
                # refuted, so it is the same retryable failure rather than a claim.
                if entry_missing:
                    entry["state"] = "failed"
                    entry["reason"] = "mint_server_absent"
                elif validation_failed:
                    entry["state"] = "failed"
                    entry["reason"] = "mint_grant_unproven"
                else:
                    entry["state"] = "granted"

        # Outside the lock: a wedged teardown waits up to the shutdown timeout, and
        # holding the table that long stalls Connect for every other provider.
        if holdings:
            await _dispose_mint(holdings)
            holdings = {}
        if superseded:
            await asyncio.to_thread(_log_mint_outcome, slug, "ok", "reason=superseded")
            return
        if entry_missing:
            await asyncio.to_thread(_log_mint_outcome, slug, "error", "reason=mint_server_absent")
        elif validation_failed and not oauth_url:
            # The refuted-grant no-challenge branch above: a failure, so it audits
            # as one rather than riding the ok/url_minted line.
            await asyncio.to_thread(_log_mint_outcome, slug, "error", "reason=mint_grant_unproven")
        else:
            # ``url_minted`` records whether the spawn produced an approval URL
            # or found no challenge (an open endpoint, or a grant that landed
            # concurrently) -- the route is derivable from it and ``reason``.
            await asyncio.to_thread(
                _log_mint_outcome,
                slug,
                "ok",
                f"url_minted={bool(oauth_url)}",
            )
    except Exception as exc:  # noqa: BLE001 — background task; record, never raise
        logger.warning("OAuth mint for %r failed: %s", slug, type(exc).__name__)
        await _dispose_mint(holdings)
        # Coarse code only — never provider- or exception-supplied text.
        reason = "mint_" + type(exc).__name__.lower()[:32]
        async with _mints_lock:
            entry = _mints.get(slug)
            if entry is not None and entry.get("token") == my_token:
                # Token-guarded: a late failure from a superseded flow must not
                # overwrite the newer row and strand its client, watcher and spec.
                _mints[slug] = {
                    "state": "failed",
                    "reason": reason,
                    "started": time.monotonic(),
                    "token": my_token,
                }
        await asyncio.to_thread(_log_mint_outcome, slug, "error", f"reason={reason}")
    finally:
        # Cancellation is a BaseException, so it skips the handler above while the
        # child process, its loopback listener, the protected PID and the spec are
        # all still held. Every other path has already emptied or handed off the
        # holdings, so this only ever fires for the abandoned-mid-flight case.
        if holdings:
            await _dispose_mint(holdings)


def _mint_holder_alive(entry: MintState) -> bool:
    """Whether this row's URL can still actually be redeemed.

    The PKCE verifier and the loopback listener both live in the minting process,
    so a dead process means a URL no paste can complete. Asked of a row that
    already holds a URL; a row still ``minting`` has nothing stamped yet.

    ABSTAINS on a row carrying a ``generation``. That row was minted on the SHARED
    process (:mod:`kiro_crew.connections.warm`) and owns no ``client`` by design, so
    the ``client is None`` branch below would answer for it -- and answering is the
    bug, because False here is a VERDICT, not a shrug: :func:`expire_dead_holder`
    acts on it, so the first mint-state poll on a warm slug withdrew a URL whose
    process and session were both alive. Warm rows are judged by generation AND
    activation liveness at the warm table's own chokepoint
    (``warm.expire_dead_mints``, called on the status path and by the reaper), which
    is the only reader that can see the registry those stamps name.
    """
    if entry.get("generation"):
        return True
    client = entry.get("client")
    if client is None:
        return False
    probe = getattr(client, "is_process_alive", None)
    if probe is None:
        return True
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 — liveness must never raise into a serve
        logger.debug("mint liveness check failed", exc_info=True)
        return True


def _agent_spec_entry_missing(slug: str) -> bool:
    """Whether ``slug`` has no server left in the rendered agent spec.

    A concurrent uninstall can remove the entry between a Connect writing it and
    this flow initializing, which leaves nothing to produce a challenge.

    Blocking: it stats and reads the shared agents directory. Async callers go
    through ``asyncio.to_thread``.
    """
    agents_dir = _agent.kiro_agents_dir_path()
    # Hardened reader (#6736): a refused main spec reads as absent, so the entry
    # counts as missing -- the same degrade-as-absent direction as before.
    spec = (
        _read_agent_spec(
            agents_dir / AGENT_FILENAME,
            operation="connections_mint",
            source="dashboard",
        )
        or {}
    )
    servers = spec.get("mcpServers") or {}
    return mcp_server_alias(slug) not in servers


async def expire_dead_holder(slug: str) -> None:
    """Write the dead-holder verdict into the row instead of only reporting it.

    The card-facing view already refuses to show a URL whose minting process is
    gone, but a view-only verdict leaves the STORED row ``waiting`` -- and the
    abandon fence only acts on ``expired``, so the entry could never be cleaned up
    and the card sat on "needs attention" with nothing left to retry.
    """
    async with _mints_lock:
        entry = _mints.get(slug)
        if entry is None or entry.get("state") != "waiting" or _mint_holder_alive(entry):
            return
        entry["state"] = "expired"
        entry["reason"] = "mint_process_gone"
        doomed = entry
    await _dispose_mint(doomed)


def pending_mint_for(slug: str) -> MintState | None:
    """The card-facing view of a mint: the STORED state and URL, never the holdings.

    Liveness is deliberately NOT probed here. A dead holder's row is committed to
    ``expired`` by :func:`expire_dead_holder` on the same request, and probing a
    second time would let the process die in between -- reporting a state the
    stored row does not have, which the abandon fence then refuses, leaving the
    entry with nothing able to clean it up.
    """
    entry = _mints.get(slug)
    if entry is None:
        return None
    # The token travels with every view: a tab can only tell ITS OWN terminal row
    # from a sibling tab's by naming the row.
    token = str(entry.get("token") or "")
    state = entry.get("state", "minting")
    view: MintState = {"state": state, "token": token}
    if entry.get("shared"):
        # UNCLAIMED: a premint the warm table holds for whoever clicks Connect next,
        # not a flow any caller started. Reported rather than hidden because this one
        # view feeds two readers with different needs: the status classifier must
        # refuse to read it as user consent (see ``status._classify``), while the
        # mint-state poll must still tell it apart from ``idle``, which its own
        # contract defines as "no mint exists for the provider". Filtering the row out
        # would answer the second reader with that lie -- on exactly the slug a card
        # has just adopted.
        view["shared"] = True
    if entry.get("oauth_url") and state == "waiting" and not entry.get("shared"):
        # The consent URL completes a MACHINE-WIDE grant, so it is served only to
        # the caller whose click owns the row. An unclaimed premint's view says a
        # row exists and nothing more; the URL surfaces only after adoption rotates
        # the token and clears ``shared`` (see ``warm.adopt_shared_mint``).
        view["oauth_url"] = entry["oauth_url"]
    if entry.get("reason"):
        view["reason"] = entry["reason"]
    return view
