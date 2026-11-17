"""A Settings or Connections sign-in run on the KAS engine, for chats that run on KAS.

A chat on KAS (the default backend) reads a remote MCP server's OAuth tokens from
Kiro Crew's gateway-wide grant vault, through the engine's ``_kiro/secret/*``
requests (``acp/kas_secret_storage.py``). The cold mint
(:mod:`kiro_crew.connections.mint`) runs kiro-cli, whose grant lands in kiro-cli's
own cache, and no KAS chat reads that cache. So while chats run on KAS and
``agent.kas_persist_mcp_sign_in`` is on, the sign-in for a server the engine is
sent as its bare url runs here instead, on a KAS engine process of its own:

1. The one-server mint spec is written exactly as the kiro-cli mint writes it,
   and a KAS engine process starts with it. Its handshake declares Crew's secret
   storage, so whatever the engine stores lands in the vault every KAS chat reads.
2. The session reports the server. Connected means the vault already holds a
   working sign-in, or the server needs none: the row is ``granted``.
3. A server that needs authorization is sent ``_kiro/mcp/resetServer`` with
   ``startOAuth``. Its consent URL arrives as ``_kiro/openExternalUrl`` and passes
   the credential gate a kiro-cli URL passes before the row shows it.
4. The process is held while the user consents: the engine's callback listener
   and its PKCE verifier live in it. The row turns ``granted`` once the vault holds
   the server's ``tokens`` entry from this sign-in, and ``expired`` when the engine
   ends the sign-in without one or the TTL passes.

An entry with an ``oauth`` block (an operator's pre-registered client, which only
kiro-cli reads) or with headers keeps the kiro-cli mint, and so does every sign-in
while chats run on another backend or with sign-in persistence off.

The row, its holdings and their release are the cold mint's (``MintState``,
``_dispose_mint``), read through that module at call time, so a test that swaps
its table reaches this flow too. No filesystem call runs on the event loop: each
one goes through ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping

from kiro_crew import mcp_grant_kas, platform_compat
from kiro_crew.config.paths import data_home
from kiro_crew.connections import mint as _mint
from kiro_crew.connections import status as _status
from kiro_crew.connections.mint import MintState
from kiro_crew.connections.registry import Provider, get_provider, is_preregistered
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.security import (
    oauth_url_contains_credential,
    sanitized_oauth_endpoint_display,
)
from kiro_crew.start_priority import person_priority

logger = logging.getLogger(__name__)

#: How long one drain of the mint session's queue waits for a frame.
_SETTLE_SECONDS = 0.5
#: How long the session may take to be torn down before the process is killed.
_SESSION_DESTROY_TIMEOUT_SECONDS = 3.0
#: The report buckets that end the wait for the server's first state.
_REPORTED_STATES = ("ready", "awaiting_auth", "failed")
#: ``kasChatSignIn`` on an MCP Servers row (:func:`kas_sign_in_routes`): a sign-in
#: made from the row runs on kiro-cli, so a KAS chat signs in from the chat.
KAS_CHAT_SIGN_IN_CHAT = "chat"
#: ``kasChatSignIn``: the row's sign-in would be saved for KAS chats, but the grant
#: it holds now only kiro-cli's cache has, so signing in again is what reaches them.
KAS_CHAT_SIGN_IN_AGAIN = "again"


class _KasMintFailed(Exception):
    """A KAS sign-in that cannot continue; ``reason`` is the row's coarse code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _acp_runtime_factory() -> Any:
    """The agent-runtime class, through the session manager's loader (see ``warm``).

    Application code does not import ``kiro_crew.acp``
    (``scripts/check_agent_sdk_boundary.py``), and this is the sanctioned accessor
    the warm mint uses too. It is also what a test substitutes a fake runtime for.
    """
    from kiro_crew.session import _load_bg_runtime_types

    return _load_bg_runtime_types()[0]


def kas_signs_in(entry: Mapping[str, Any] | None = None) -> bool:
    """Whether a sign-in for *entry* runs on the KAS engine. Blocking (reads config).

    True while ordinary chats run on KAS and sign-in persistence is on, for an
    entry the engine is sent as its bare url: no ``oauth`` block and no headers,
    and an ASCII url the vault's entry name can be derived from. ``None`` asks
    only the backend half, for a caller that has no entry in hand.
    """
    if entry is not None and not _sent_bare(entry):
        return False
    return _status.chat_backend_is_kas() and mcp_grant_kas.persistence_enabled()


def _sent_bare(entry: Mapping[str, Any]) -> bool:
    """Whether the engine is sent *entry* as its bare url, the entry half of :func:`kas_signs_in`."""
    url = entry.get("url")
    if entry.get("oauth") or entry.get("headers"):
        return False
    return isinstance(url, str) and url.isascii()


def kiro_cli_sign_in_providers(providers: list[Provider]) -> list[Provider]:
    """The *providers* whose Connect still signs in on kiro-cli. Blocking (reads config).

    The premint asks before the click, so each provider is judged by the entry its
    Connect will mount (:func:`kas_sign_in_entry`): a pre-registered provider, and
    one whose main-spec entry the engine is not sent bare, keep kiro-cli. A provider
    the spec does not hold yet gets the registry's url, sent bare, written by its
    Connect before the mint starts, so it signs in on the KAS engine.
    """
    if not kas_signs_in():
        return list(providers)
    servers = _configured_servers()
    kept: list[Provider] = []
    for provider in providers:
        entry = servers.get(mcp_server_alias(str(provider["slug"])))
        if is_preregistered(provider) or (isinstance(entry, dict) and not _sent_bare(entry)):
            kept.append(provider)
    return kept


def kas_chat_sign_in_urls(providers: list[Provider]) -> dict[str, str | None]:
    """The url a KAS chat reads each provider's saved sign-in at, by slug. Blocking.

    The entry a KAS chat mounts is the main spec's, from one read, or while the
    spec holds none the registry's url, which Connect writes bare (as
    :func:`kiro_cli_sign_in_providers` reasons). ``None`` for an entry the engine
    is not sent bare (:func:`_sent_bare`): one with headers reaches it through
    Crew's stdio bridge, which forwards those headers and never reads the vault.
    """
    servers = _configured_servers()
    urls: dict[str, str | None] = {}
    for provider in providers:
        slug = str(provider["slug"])
        entry = servers.get(mcp_server_alias(slug))
        if not isinstance(entry, dict):
            urls[slug] = str(provider["mcp_url"])
        else:
            urls[slug] = str(entry["url"]) if _sent_bare(entry) else None
    return urls


def _configured_servers() -> dict[str, Any]:
    """The main agent spec's ``mcpServers``, through the hardened reader. Blocking."""
    from kiro_crew import agent as _agent
    from kiro_crew.agent_discovery import _read_agent_spec
    from kiro_crew.agent_files import AGENT_FILENAME

    spec = _read_agent_spec(
        _agent.kiro_agents_dir_path() / AGENT_FILENAME,
        operation="connections_mint",
        source="dashboard",
    )
    servers = spec.get("mcpServers") if isinstance(spec, dict) else None
    return servers if isinstance(servers, dict) else {}


def _configured_entry(slug: str) -> dict[str, Any] | None:
    """*slug*'s entry in the main agent spec. Blocking."""
    entry = _configured_servers().get(mcp_server_alias(slug))
    return dict(entry) if isinstance(entry, dict) else None


def kas_sign_in_routes(names: list[str]) -> dict[str, bool] | None:
    """Whether a sign-in for each MCP server in *names* runs on the KAS engine. Blocking.

    The answer :func:`kas_sign_in_entry` gives for one server, for every row of the
    MCP Servers table from one read of the main agent spec: a pre-registered
    provider, a name the spec does not hold, and an entry the engine is not sent
    bare keep the kiro-cli mint. With sign-in persistence off every name does.
    ``None`` while ordinary chats do not run on KAS, or the config cannot be read:
    then no KAS chat is there to read a sign-in.
    """
    if not _status.chat_backend_is_kas():
        return None
    if not mcp_grant_kas.persistence_enabled():
        return dict.fromkeys(names, False)
    servers = _configured_servers()
    routes: dict[str, bool] = {}
    for name in names:
        provider = get_provider(name)
        entry = servers.get(mcp_server_alias(name))
        routes[name] = (
            (provider is None or not is_preregistered(provider))
            and isinstance(entry, dict)
            and _sent_bare(entry)
        )
    return routes


def kas_sign_in_entry(slug: str, pinned: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The entry a KAS sign-in for *slug* mounts, or ``None`` for the kiro-cli mint.

    *pinned* is the validated entry of an owner-added server; a registry provider's
    entry is read from the main agent spec. Blocking (reads the spec and config).
    """
    if pinned is None:
        provider = get_provider(slug)
        if provider is not None and is_preregistered(provider):
            return None
    entry = dict(pinned) if pinned is not None else _configured_entry(slug)
    if entry is None or not kas_signs_in(entry):
        return None
    return entry


def forget_kas_sign_in(mcp_url: str) -> bool | None:
    """Delete the engine's saved sign-in for *mcp_url* from the grant vault. Blocking.

    Removes the ``tokens`` and ``client`` entries, as a kiro-cli Disconnect unlinks
    its token and registration pair. ``True``: entries were removed and no
    ``tokens`` entry is left. ``False``: none was stored. ``None``: one may still be
    there, because a vault directory is a link (refused, as the engine's store
    refuses it), the vault cannot be read, or the write failed. Entry names are
    compared; no value is read or logged.
    """
    if not mcp_url.isascii():
        return None
    root = mcp_grant_kas.grant_store_root(data_home())
    for path in (root.parent, root, root / ".vault"):
        if platform_compat.is_link_or_junction(path):
            return None
    # A stat, not ``exists()``: from Python 3.14 that answers "no store" for any
    # OSError, and an unreadable vault must not read as nothing stored.
    try:
        (root / ".vault" / "secrets.enc").stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None
    from kiro_crew.secrets import SecretVault

    vault = SecretVault(root)
    try:
        names = set(vault.list_names())
        stored = [name for name in mcp_grant_kas.kas_saved_sign_in_names(mcp_url) if name in names]
        for name in stored:
            vault.delete_sync(name)
    except (OSError, ValueError):
        logger.warning("KAS saved sign-in could not be removed from the grant vault")
        return None
    if mcp_grant_kas.kas_grant_presence(mcp_url, store_root=root) is not False:
        return None
    return bool(stored)


class _KasMintProcess:
    """One KAS engine process and the one session a sign-in holds on it.

    Shaped like the kiro-cli client the cold mint holds (``_pid``,
    ``is_process_alive``, ``shutdown``), so the row's PID claim, liveness check and
    release treat both the same. ``pid`` is what the kill gate's tenancy reads.
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.handle: Any = None

    @property
    def pid(self) -> int | None:
        pid = getattr(self.runtime, "pid", None)
        return pid if isinstance(pid, int) and pid > 0 else None

    @property
    def _pid(self) -> int:
        return self.pid or 0

    def is_process_alive(self) -> bool:  # pid-owner-ok: the mint owns this engine
        try:
            return bool(self.runtime.is_alive())
        except Exception:  # noqa: BLE001 -- liveness must never raise into a serve
            logger.debug("KAS mint liveness check failed", exc_info=True)
            return False

    async def open(self, agent: str) -> None:
        """Start the engine with *agent* and open the session that mounts its server."""
        priority = person_priority(True)
        await self.runtime.spawn(start_priority=priority)
        # An explicit empty array: the mint spec's one server is all this session
        # mounts, never the gateway's pooled servers.
        self.handle = await self.runtime.create_session(
            agent=agent, mcp_servers=[], start_priority=priority
        )

    async def settle(self) -> None:
        """Fold the frames waiting on the session's queue into its report."""
        await self.handle.drain_init(
            duration=_SETTLE_SECONDS, idle_exit=_SETTLE_SECONDS, no_report_ceiling=_SETTLE_SECONDS
        )

    def server_state(self, alias: str) -> str:
        """The bucket the session's report has *alias* in, or ``""``."""
        payload = self.handle.mcp_session_report().payload() or {}
        for state in _REPORTED_STATES:
            if alias in (payload.get(state) or ()):
                return state
        return ""

    def sign_in_in_flight(self, alias: str) -> bool:
        """Whether the engine has not yet answered this server's sign-in."""
        return bool(self.runtime.mcp_sign_in_holds(self.handle.session_id, alias))

    async def shutdown(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                await asyncio.wait_for(handle.destroy(), timeout=_SESSION_DESTROY_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001 -- the kill below frees the session too
                logger.debug("KAS mint session teardown failed", exc_info=True)
        await self.runtime.kill(expected=True, reason="connections mint teardown")


async def _start_process(slug: str, entry: dict[str, Any], holdings: MintState) -> _KasMintProcess:
    """Write the mint spec, start the engine on it and open its session.

    Every holding is recorded on *holdings* as soon as it exists, so the caller's
    release covers a start that fails or is cancelled part way. The PID claim races
    the start for the reason the kiro-cli mint gives (``_claim_mint_pid``).
    """
    work_dir = await asyncio.to_thread(data_home)
    agent_name, spec_path = await asyncio.to_thread(_mint._write_mint_agent_spec, slug, entry)
    holdings["agent"] = agent_name
    holdings["spec_path"] = spec_path
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    process = _KasMintProcess(
        _acp_runtime_factory()(
            work_dir=work_dir / "connections" / "mint",
            agent=agent_name,
            sandbox_mode="auto",
            acp_backend=ACP_BACKEND_KAS,
        )
    )
    holdings["client"] = process
    claim = asyncio.get_running_loop().create_task(
        _mint._claim_mint_pid_when_spawned(process, holdings)
    )
    try:
        await asyncio.wait_for(process.open(agent_name), timeout=_mint._MINT_READY_TIMEOUT_SECONDS)
    finally:
        claim.cancel()
        _mint._claim_mint_pid(process, holdings)
    return process


async def _first_server_state(process: _KasMintProcess, alias: str) -> str:
    """Drain the session until its report places *alias* in a bucket."""
    while True:
        state = process.server_state(alias)
        if state:
            return state
        if not process.is_process_alive():  # pid-owner-ok: the mint owns this engine
            raise _KasMintFailed("mint_process_gone")
        await process.settle()


async def _consent_url(process: _KasMintProcess, alias: str) -> str:
    """Start the engine's sign-in for *alias* and return the consent URL it opens.

    Whether the sign-in still runs is read before each drain, so a URL the engine
    queued before it ended the attempt is still found.
    """
    if not process.runtime.begin_mcp_sign_in(process.handle.session_id, alias):
        raise _KasMintFailed("mint_sign_in_refused")
    while True:
        ended = not process.sign_in_in_flight(alias)
        alive = process.is_process_alive()  # pid-owner-ok: the mint owns this engine
        await process.settle()
        for request in process.handle.pop_pending_oauth_requests():
            if request.get("serverName") == alias and request.get("oauthUrl"):
                return str(request["oauthUrl"])
        if not alive:
            raise _KasMintFailed("mint_process_gone")
        if ended:
            raise _KasMintFailed("mint_sign_in_ended")


def _mounted_url(entry: dict[str, Any], fallback: str) -> str:
    """The url the engine keys this entry's vault grant by: the entry's own, else *fallback*."""
    url = entry.get("url")
    return url if isinstance(url, str) and url else fallback


async def _signed_in(
    process: _KasMintProcess, alias: str, mcp_url: str, tokens_before: bool | None
) -> bool:
    """Whether this sign-in left the server's tokens in the vault.

    The vault must hold a ``tokens`` entry. When none was there as the sign-in
    began, its appearing is the proof. When one was (tokens the engine could not
    use), presence proves nothing, so the session must also report the server
    connected, which it does only after the new sign-in.
    """
    if await asyncio.to_thread(mcp_grant_kas.kas_grant_presence, mcp_url) is not True:
        return False
    return tokens_before is False or process.server_state(alias) == "ready"


async def _end_row(slug: str, token: str, state: str) -> None:
    """Set this flow's row terminal and release what it holds; a newer row is left alone."""
    doomed: MintState | None = None
    async with _mint._mints_lock:
        row = _mint._mints.get(slug)
        if row is not None and row.get("token") == token and row.get("state") == "waiting":
            row["state"] = state
            doomed = row
    if doomed is not None:
        await _mint._dispose_mint(doomed)


async def _kas_mint_watcher(
    slug: str,
    mcp_url: str,
    token: str,
    process: _KasMintProcess,
    alias: str,
    tokens_before: bool | None,
) -> None:
    """Hold the process until the sign-in stores its tokens, ends, or the TTL passes.

    Whether the engine still holds the sign-in is read BEFORE the drain: the
    server's status is queued before the engine answers the reset, so a sign-in
    seen ended is judged on every frame it sent. The engine answers the reset at
    most 60 s in while the connect, and its callback listener, run on to the
    server's ``timeout``, so an answered sign-in has ended only once the server
    reports ``failed``; the consent URL's own frame keeps it ``awaiting_auth``.
    """
    try:
        deadline = time.monotonic() + _mint._MINT_TTL_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(_mint._MINT_GRANT_POLL_SECONDS)
            alive = process.is_process_alive()  # pid-owner-ok: the mint owns this engine
            answered = not process.sign_in_in_flight(alias)
            await process.settle()
            if await _signed_in(process, alias, mcp_url, tokens_before):
                await _end_row(slug, token, "granted")
                logger.info("OAuth mint for %r completed on KAS (sign-in saved)", slug)
                return
            if not alive or (answered and process.server_state(alias) == "failed"):
                # The engine closed its callback listener with the attempt, so
                # nothing can redeem the URL the row shows. No reason: the card
                # reads a reasonless expiry as the sign-in window running out.
                await _end_row(slug, token, "expired")
                logger.info("OAuth mint for %r ended on KAS without a saved sign-in", slug)
                return
        await _end_row(slug, token, "expired")
        logger.info("OAuth mint for %r expired after %.0fs", slug, _mint._MINT_TTL_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- watcher must never take the gateway down
        logger.debug("KAS OAuth mint watcher failed [%s]", slug, exc_info=True)


async def _set_terminal(
    slug: str, token: str, state: str, reason: str = "", rejected_endpoint: str | None = None
) -> bool:
    """Replace this flow's row with a terminal one; False when a newer mint owns the slug."""
    row: MintState = {"state": state, "started": time.monotonic(), "token": token}
    if reason:
        row["reason"] = reason
    if rejected_endpoint is not None:
        row["rejected_endpoint"] = rejected_endpoint
    async with _mint._mints_lock:
        if _mint._mints.get(slug, {}).get("token") != token:
            return False
        _mint._mints[slug] = row
    return True


async def mint_on_kas(slug: str, mcp_url: str, token: str, entry: dict[str, Any]) -> None:
    """Run *slug*'s sign-in on a KAS engine process, as ``start_oauth_mint`` would on kiro-cli.

    Called by ``start_oauth_mint`` once the row *token* names is reserved and its
    predecessor released. Never raises: every outcome is the row's state and an SEL
    record. A URL the credential gate rejects gets one fresh process before the
    rejection is final, as on the kiro-cli path.
    """
    alias = mcp_server_alias(slug)
    holdings: MintState = {}
    # The engine stores tokens under the MOUNTED entry's url; the registry url can
    # differ by a trailing slash, which is a distinct vault key.
    grant_url = _mounted_url(entry, mcp_url)
    try:
        oauth_url = ""
        tokens_before: bool | None = None
        process: _KasMintProcess | None = None
        for attempt in range(_mint._MINT_URL_REJECTION_ATTEMPTS):
            oauth_url = ""
            process = await _start_process(slug, entry, holdings)
            state = await asyncio.wait_for(
                _first_server_state(process, alias), timeout=_mint._MINT_READY_TIMEOUT_SECONDS
            )
            if state == "ready":
                break
            tokens_before = await asyncio.to_thread(mcp_grant_kas.kas_grant_presence, grant_url)
            if state == "failed" and tokens_before is not True:
                raise _KasMintFailed("mint_server_failed")
            # A failed server with saved tokens is one whose refresh the vendor
            # refused with an error the engine does not read as a lost grant: it
            # keeps the dead tokens and asks for no sign-in. An explicit Connect
            # replaces them, since the sign-in below resets every credential first.
            oauth_url = await asyncio.wait_for(
                _consent_url(process, alias), timeout=_mint._MINT_READY_TIMEOUT_SECONDS
            )
            if not await asyncio.to_thread(oauth_url_contains_credential, oauth_url):
                break
            # The same refusal the kiro-cli mint applies; the value is never logged.
            logger.warning("OAuth mint for %r produced a URL with a credential pattern", slug)
            await _mint._dispose_mint(holdings)
            holdings = {}
            if attempt + 1 < _mint._MINT_URL_REJECTION_ATTEMPTS:
                continue
            rejected = await asyncio.to_thread(sanitized_oauth_endpoint_display, oauth_url)
            await _set_terminal(slug, token, "failed", "mint_url_rejected", rejected)
            await asyncio.to_thread(
                _mint._log_mint_outcome, slug, "error", "reason=mint_url_rejected route=kas"
            )
            return

        if not oauth_url:
            # Connected at once: the vault already held a working sign-in, or the
            # server asks for none.
            await _mint._dispose_mint(holdings)
            holdings = {}
            if await _set_terminal(slug, token, "granted"):
                await asyncio.to_thread(
                    _mint._log_mint_outcome, slug, "ok", "url_minted=False route=kas"
                )
            else:
                await asyncio.to_thread(_mint._log_mint_outcome, slug, "ok", "reason=superseded")
            return

        if process is None:
            raise _KasMintFailed("mint_process_gone")
        superseded = True
        async with _mint._mints_lock:
            row = _mint._mints.get(slug)
            if row is not None and row.get("token") == token:
                row.update(holdings)
                row.update(
                    {
                        "state": "waiting",
                        "oauth_url": oauth_url,
                        "watcher": asyncio.get_running_loop().create_task(
                            _kas_mint_watcher(slug, grant_url, token, process, alias, tokens_before)
                        ),
                    }
                )
                holdings = {}
                superseded = False
        if superseded:
            await _mint._dispose_mint(holdings)
            holdings = {}
            await asyncio.to_thread(_mint._log_mint_outcome, slug, "ok", "reason=superseded")
            return
        await asyncio.to_thread(_mint._log_mint_outcome, slug, "ok", "url_minted=True route=kas")
    except Exception as exc:  # noqa: BLE001 -- background task; record, never raise
        reason = (
            exc.reason
            if isinstance(exc, _KasMintFailed)
            else "mint_" + type(exc).__name__.lower()[:32]
        )
        logger.warning("OAuth mint for %r on KAS failed: %s", slug, reason)
        await _mint._dispose_mint(holdings)
        holdings = {}
        await _set_terminal(slug, token, "failed", reason)
        await asyncio.to_thread(
            _mint._log_mint_outcome, slug, "error", f"reason={reason} route=kas"
        )
    finally:
        # A cancellation skips the handler above while the process, its PID claim
        # and the spec are still held.
        if holdings:
            await _mint._dispose_mint(holdings)
