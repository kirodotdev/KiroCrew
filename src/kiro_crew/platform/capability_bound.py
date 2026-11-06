"""Seam-boundary LIVENESS bound for the ``CapabilityManager`` extension point.

The :class:`CapabilityManager` Protocol requires that every operation be
internally time-bounded so a slow companion op cannot stall MCP handlers. As
defense-in-depth the core ALSO wraps every mutation op with ``asyncio.wait_for``
here, and — critically — applies that wrapper at **context composition time**
(``PlatformContext.__post_init__``), NOT at a leaf dashboard accessor. That way
EVERY reader of ``current_context().capability_manager`` inherits the bound: the
dashboard handlers, and any future non-dashboard consumer (subagent, conductor,
MCP-tool code, an apps backend) that follows the documented CPP pattern of
reading the context directly. Enforcing it at the seam — where the contract is
declared — closes the gap where a new call site could obtain an unbounded
manager and reintroduce the exact hang class this exists to prevent.

The constants live here (not in a dashboard module) because the bound is now a
platform-layer contract; ``mcp.py`` imports ``CAPABILITY_UNINSTALL_TIMEOUT`` to
size its pre-lock uninstall phase budget from the same source of truth.
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
import string
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from kiro_crew import extension_governance as _gov
from kiro_crew.apps import marketplace_install as _app_installer
from kiro_crew.mcp_utils import registry_accepts_query
from kiro_crew.platform.interfaces import CapabilityResult as _CapabilityResult

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import CapabilityManager, CapabilityResult

#: Max seconds the core waits on a CapabilityManager UNINSTALL op. Tight,
#: because uninstall is quick and (in the apply path) the core wants a bounded
#: worst case.
CAPABILITY_UNINSTALL_TIMEOUT = 60

#: Max seconds the core waits on a CapabilityManager INSTALL op. Generous — a
#: legitimate install shells out to a package manager (npm/pip cold download
#: routinely exceeds 60s), and cancelling it mid-flight could leave partially
#: applied companion registry/filesystem state. Install call sites do NOT hold
#: the process-wide MCP lock, so a longer bound costs no lock availability.
CAPABILITY_INSTALL_TIMEOUT = 600

#: Max seconds the core waits on a CapabilityManager READ op
#: (``list_mcp``/``list_skills``/``list_agents``/``registry``). Reads are quick
#: (list a registry / parse installed entries), so a generous 30s still catches
#: a wedged companion. Bounding reads too keeps the LIVENESS guarantee symmetric:
#: the dashboard POLLS these list endpoints, so an unbounded stalled read would
#: accumulate pending gateway tasks on every poll — the exact wedge class the
#: seam bound exists to prevent — even though reads mutate nothing.
CAPABILITY_READ_TIMEOUT = 30


async def _off_loop(fn: Any, *args: str) -> "str | None":
    """Run a governance check on the governance executor, off the event loop.

    A check reads the profile store, which stats the profiles directory.
    """
    from kiro_crew.executors import governance_executor

    return await asyncio.get_running_loop().run_in_executor(governance_executor(), fn, *args)


_ID_FIRST = frozenset(string.ascii_letters + string.digits)
_ID_REST = _ID_FIRST | frozenset("._-")


def is_id_segment(value: str, limit: int) -> bool:
    """True when ``value`` is one id segment of at most ``limit`` characters.

    It starts alphanumeric (so it can never be read as an option) and the rest is
    alphanumeric or ``.``, ``_``, ``-``. No whitespace, so no trailing newline.
    """
    return (
        0 < len(value) <= limit and value[0] in _ID_FIRST and all(c in _ID_REST for c in value[1:])
    )


_MCP_TARGET_PREFIX = "mcpServers."


def market_install_id(item: object) -> Optional[str]:
    """The id a marketplace plan item is installed as, or ``None`` if unreadable.

    That is the name a direct ``install_mcp`` / ``install_skill`` uses, so
    governance can ask both install paths under the same list entry. An edition
    may say it outright as ``installId``; otherwise it is read from ``target``:
    ``mcpServers.<id>`` for an MCP item, the last path segment of the folder for
    a skill. Only ``mcp`` and ``skill`` items have one.
    """
    if not isinstance(item, dict) or item.get("kind") not in ("mcp", "skill"):
        return None
    explicit = item.get("installId")
    if explicit is not None:
        ident = explicit if isinstance(explicit, str) else ""
    else:
        target = item.get("target")
        if not isinstance(target, str):
            return None
        if item["kind"] == "mcp":
            if not target.startswith(_MCP_TARGET_PREFIX):
                return None
            ident = target[len(_MCP_TARGET_PREFIX) :]
        else:
            ident = target.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    # A server or skill id as a direct install names it: one path segment.
    if ".." in ident or not is_id_segment(ident, 128):
        return None
    return ident


def _refused(reason: str) -> "CapabilityResult":
    """The result an op returns when governance refuses it (``extension_governance``).

    Checked here, at the seam every reader of ``current_context().capability_manager``
    goes through, so no call site can reach the companion's installer around it. In
    an enterprise build the ``capabilities.extension_install`` baseline is off, so
    the extension signature and version rules that only apply to an install are off
    with it (RFC enterprise edition, Red "Signed extensions only" and "Extension
    versions").
    """
    from kiro_crew.platform.interfaces import CapabilityResult

    return CapabilityResult(ok=False, message=f"Blocked by governance policy: {reason}")


#: The dashboard feature preview (``dashboard.feature_previews.<name>``) that
#: turns on installing a ``kind = app`` listing through the app installer.
#: Default off: an app listing is then refused as before this route existed.
MARKET_APPS_PREVIEW = "marketplace_apps"


def market_apps_enabled() -> bool:
    """Whether the ``marketplace_apps`` preview is on in this gateway's config.

    Read from config at call time, so the switch takes effect on the next
    request. Any failure to read it answers off (fail closed). Blocking: call it
    in a worker thread from the loop.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        previews = KiroCrewConfig.load().dashboard.feature_previews
        return isinstance(previews, dict) and previews.get(MARKET_APPS_PREVIEW) is True
    except Exception:  # noqa: BLE001 -- an unreadable config keeps the route off
        return False


#: How long a prepared marketplace app waits for the yes before its staging
#: folder is deleted (checked on every apply and confirm).
MARKET_APP_PENDING_TTL = 15 * 60


def _clock() -> float:
    """The expiry clock; a seam of its own so a test never rebinds ``time.monotonic``."""
    return time.monotonic()


def _requested_item(plan: Dict[str, Any]) -> "Dict[str, Any] | None":
    """The plan's root: the item the person asked for (``reason == "REQUESTED"``)."""
    items = plan.get("items")
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("reason") == "REQUESTED":
            return item
    return None


@dataclass(frozen=True)
class MarketAppPending(_CapabilityResult):
    """``market_apply`` on an app plan: prepared, not installed, waiting for the yes.

    ``confirm`` is what the dashboard shows (the installer's confirm data with
    ``inFolder``, plus the plan's permission union as ``planPermissions``);
    ``token`` names this prepared install for :meth:`market_app_complete`.
    """

    confirm: Dict[str, Any] = field(default_factory=dict)
    token: str = ""


@dataclass(frozen=True)
class MarketAppInstallFailed(_CapabilityResult):
    """The yes reached the installer and it failed: the app may be partly installed.

    Distinct from a plain refusal, which is only ever answered before
    ``install_app`` runs, so the dashboard never says nothing was installed.
    """


class BoundedCapabilityManager:
    """Wraps a ``CapabilityManager`` so every async MUTATION op is time-bounded
    with ``asyncio.wait_for``.

    Enforces the Protocol's LIVENESS contract ONCE at the seam boundary — the
    context is composed with this wrapper in ``PlatformContext.__post_init__``,
    so ``mcp.py`` uninstall, the ``agents.py`` capability handlers, and every
    future reader of ``current_context().capability_manager`` inherit the bound
    without having to remember it. Bounds are differentiated: uninstall ops are
    tight (:data:`CAPABILITY_UNINSTALL_TIMEOUT`), install ops are generous
    (:data:`CAPABILITY_INSTALL_TIMEOUT`) so a slow-but-legitimate package-manager
    download is not cancelled mid-mutation; the async READ ops
    (``list_*``/``registry``) are bounded by the tight
    :data:`CAPABILITY_READ_TIMEOUT`. Bounding reads too keeps the guarantee
    symmetric — the dashboard polls the list endpoints, so a stalled unbounded
    read would accumulate pending gateway tasks, the same wedge class the bound
    exists to prevent. Only the sync ``available()`` probe passes straight
    through (it does no I/O).

    An explicit proxy (not a ``__getattr__`` delegator) so a Protocol method
    added later surfaces as a loud ``AttributeError`` here rather than silently
    slipping past the bound — a maintenance trap is preferable to a silent hole
    in the liveness guarantee.
    """

    def __init__(self, inner: "CapabilityManager") -> None:
        self._inner = inner
        # Marketplace apps prepared by ``market_apply`` and waiting for the yes,
        # keyed by a one-time token: ``(ref, plan_digest, PreparedInstall)``.
        self._pending_apps: Dict[str, tuple] = {}

    def available(self) -> bool:
        return self._inner.available()

    async def list_mcp(self) -> List[Dict[str, Any]]:
        return await asyncio.wait_for(self._inner.list_mcp(), timeout=CAPABILITY_READ_TIMEOUT)

    async def list_skills(self) -> List[Dict[str, Any]]:
        return await asyncio.wait_for(self._inner.list_skills(), timeout=CAPABILITY_READ_TIMEOUT)

    async def list_agents(self) -> List[Dict[str, Any]]:
        return await asyncio.wait_for(self._inner.list_agents(), timeout=CAPABILITY_READ_TIMEOUT)

    async def registry(self, query: "str | None" = None) -> List[Dict[str, Any]]:
        """Forward the optional search hint, but only to an inner op that takes it.

        The wrapper must advertise ``query`` in its OWN signature: every caller
        reaches the manager through this bind, so a wrapper that dropped the
        parameter would make the hint undetectable and every edition registry
        unsearchable past the caller's row guard. Forwarding is feature-detected
        so an edition still on the zero-arg signature is called the old way
        instead of raising ``TypeError`` — the hint is dropped, which the caller's
        own filter already tolerates.
        """
        inner = self._inner.registry
        call = inner(query=query) if query and registry_accepts_query(inner) else inner()
        return await asyncio.wait_for(call, timeout=CAPABILITY_READ_TIMEOUT)

    async def install_mcp(self, server_id: str) -> "CapabilityResult":
        denied = await _off_loop(_gov.extension_install_denied, "mcp", server_id)
        if denied:
            return _refused(denied)
        return await asyncio.wait_for(
            self._inner.install_mcp(server_id), timeout=CAPABILITY_INSTALL_TIMEOUT
        )

    async def uninstall_mcp(self, server_id: str) -> "CapabilityResult":
        denied = await _off_loop(_gov.extensions_off, "uninstall_mcp")
        if denied:
            return _refused(denied)
        return await asyncio.wait_for(
            self._inner.uninstall_mcp(server_id), timeout=CAPABILITY_UNINSTALL_TIMEOUT
        )

    async def install_skill(self, package: str) -> "CapabilityResult":
        denied = await _off_loop(_gov.extension_install_denied, "skill", package)
        if denied:
            return _refused(denied)
        return await asyncio.wait_for(
            self._inner.install_skill(package), timeout=CAPABILITY_INSTALL_TIMEOUT
        )

    async def uninstall_skill(self, package: str) -> "CapabilityResult":
        denied = await _off_loop(_gov.extensions_off, "uninstall_skill")
        if denied:
            return _refused(denied)
        return await asyncio.wait_for(
            self._inner.uninstall_skill(package), timeout=CAPABILITY_UNINSTALL_TIMEOUT
        )

    async def install_agent(self, package: str) -> "CapabilityResult":
        denied = await _off_loop(_gov.extension_install_denied, "agent", package)
        if denied:
            return _refused(denied)
        return await asyncio.wait_for(
            self._inner.install_agent(package), timeout=CAPABILITY_INSTALL_TIMEOUT
        )

    async def uninstall_agent(self, package: str) -> "CapabilityResult":
        denied = await _off_loop(_gov.extensions_off, "uninstall_agent")
        if denied:
            return _refused(denied)
        return await asyncio.wait_for(
            self._inner.uninstall_agent(package), timeout=CAPABILITY_UNINSTALL_TIMEOUT
        )

    async def list_plugins(self) -> List[Dict[str, Any]]:
        return await asyncio.wait_for(self._inner.list_plugins(), timeout=CAPABILITY_READ_TIMEOUT)

    async def plugins_out_of_sync(self) -> List[str]:
        return await asyncio.wait_for(
            self._inner.plugins_out_of_sync(), timeout=CAPABILITY_READ_TIMEOUT
        )

    async def sync_plugins(self) -> "CapabilityResult":
        # An INSTALL bound: reconciling drift can shell a package manager once
        # per drifted package, so it inherits the generous install budget rather
        # than the tight uninstall one.
        # Each package the sync would install is asked as ``plugin:<package>``.
        # Off denies before the drift read, so a pinned-off host makes no call.
        denied = await _off_loop(_gov.extensions_off, "sync_plugins")
        if not denied:
            for package in await self.plugins_out_of_sync():
                denied = await _off_loop(_gov.extension_install_denied, "plugin", str(package))
                if denied:
                    break
        if denied:
            return _refused(denied)
        return await asyncio.wait_for(
            self._inner.sync_plugins(), timeout=CAPABILITY_INSTALL_TIMEOUT
        )

    def market_available(self) -> bool:
        # Feature-detected, unlike every other op: an edition written before the
        # marketplace ops has none of them, and must keep working unchanged
        # rather than fail every marketplace route with an AttributeError.
        probe = getattr(self._inner, "market_available", None)
        return bool(self._inner.available() and callable(probe) and probe())

    async def market_plan(self, ref: str) -> Dict[str, Any]:
        # A read: resolving a plan writes nothing, so governance is asked at apply.
        if not self.market_available():
            return {}
        return await asyncio.wait_for(self._inner.market_plan(ref), timeout=CAPABILITY_READ_TIMEOUT)

    async def market_apply(self, ref: str, plan_digest: str) -> "CapabilityResult":
        # A plan can carry several kinds at once (a skill with the MCP server it
        # needs), so each item is asked as its own ``extensions:<kind>:<ref>``,
        # the way plugin sync asks each drifted package. The plan is re-read here
        # and must still carry the confirmed digest, so the items asked are the
        # items the edition is told to install: it refuses any other plan.
        # A theme item is asked of capabilities.theme_install, below.
        denied = await _off_loop(_gov.extensions_off, "market_apply")
        if denied:
            return _refused(denied)
        from kiro_crew.platform.interfaces import CapabilityResult

        plan = await self.market_plan(ref)
        if not isinstance(plan, dict) or plan.get("planDigest") != plan_digest:
            return CapabilityResult(
                ok=False,
                message="the plan changed since it was shown; nothing was installed",
            )
        refused = await _plan_items_denied(plan, "capability_market_apply")
        if refused is not None:
            return refused
        root = _requested_item(plan)
        # Off (the default), an app plan takes the edition's package path, as it
        # did before this route existed: the app installer is never reached.
        if (
            root is not None
            and root.get("kind") == "app"
            and await asyncio.to_thread(market_apps_enabled)
        ):
            return await self._prepare_app(ref, plan_digest, plan, root)
        return await asyncio.wait_for(
            self._inner.market_apply(ref, plan_digest), timeout=CAPABILITY_INSTALL_TIMEOUT
        )

    async def _prepare_app(
        self, ref: str, plan_digest: str, plan: Dict[str, Any], root: Dict[str, Any]
    ) -> "CapabilityResult":
        """A ``kind = app`` plan: the gateway app installer, never the package path.

        Resolve, download, verify the sha256 and unpack safely into staging, and
        read the capabilities in the folder. Nothing is installed: the confirm data
        goes back to the dashboard and only :meth:`market_app_complete` installs.
        """
        from kiro_crew.platform.interfaces import CapabilityResult

        probe = getattr(self._inner, "market_client", None)
        client = probe() if callable(probe) else None
        if client is None:
            return CapabilityResult(
                ok=False, message="this build cannot install apps from Kiro Space Discover"
            )
        await self._expire_pending_apps()
        installer = _app_installer
        version = pinned_app_version(root)
        if version is None:
            # The person approves one release; an unpinned item would let the
            # listing's CURRENT release be installed instead (fail closed).
            return CapabilityResult(
                ok=False,
                message="the app in this plan has no pinned version; nothing was installed",
            )
        fut = asyncio.ensure_future(
            asyncio.to_thread(
                installer.prepare_market_install, client, str(root.get("ref", ref)), version
            )
        )
        try:
            prepared = await asyncio.wait_for(
                asyncio.shield(fut), timeout=CAPABILITY_INSTALL_TIMEOUT
            )
        except BaseException as exc:
            # A timeout or a cancel leaves the thread running: whatever it
            # prepares is deleted when it finishes, never left in staging.
            fut.add_done_callback(_discard_result_when_done)
            if isinstance(exc, asyncio.TimeoutError):
                return CapabilityResult(
                    ok=False,
                    message="the app download did not finish in time; nothing was installed",
                )
            if not isinstance(exc, Exception):
                raise
            # The installer's refusal, shown as is.
            return CapabilityResult(ok=False, message=str(getattr(exc, "message", exc)))
        try:
            mismatch = _prepared_mismatch(prepared, root, version, plan.get("permissions"))
            if mismatch:
                await _discard_off_loop([prepared])
                return CapabilityResult(
                    ok=False, message=f"{mismatch} differs from the plan; nothing was installed"
                )
            stale = [t for t, p in self._pending_apps.items() if p[0] == ref]
            await _discard_off_loop([self._pending_apps.pop(t)[2] for t in stale])
        except BaseException:
            await asyncio.shield(_discard_off_loop([prepared]))
            raise
        token = secrets.token_urlsafe(24)
        self._pending_apps[token] = (ref, plan_digest, prepared, _clock())
        confirm = dict(prepared.confirm)
        confirm["planPermissions"] = plan.get("permissions")
        return MarketAppPending(ok=True, confirm=confirm, token=token)

    async def _expire_pending_apps(self) -> None:
        """Delete prepared installs older than :data:`MARKET_APP_PENDING_TTL` seconds.

        The entries are popped on the loop; their staging folders are deleted
        together in one worker thread.
        """
        cutoff = _clock() - MARKET_APP_PENDING_TTL
        expired = [t for t, p in self._pending_apps.items() if p[3] < cutoff]
        await _discard_off_loop([self._pending_apps.pop(t)[2] for t in expired])

    async def market_app_complete(
        self, ref: str, plan_digest: str, token: str, *, confirmed: bool
    ) -> "CapabilityResult":
        """After an explicit yes: re-check the digest, then install the app disabled.

        The token is spent either way. A changed plan, a missing yes or an unknown
        token installs nothing and deletes the staging folder.
        """
        from kiro_crew.platform.interfaces import CapabilityResult

        await self._expire_pending_apps()
        entry = self._pending_apps.pop(token, None)
        if entry is None or entry[0] != ref or entry[1] != plan_digest:
            if entry is not None:
                await _discard_off_loop([entry[2]])
            return CapabilityResult(ok=False, message="no prepared install matches; start again")
        prepared = entry[2]
        try:
            return await self._complete_prepared(ref, plan_digest, prepared, confirmed)
        except BaseException:
            # A plan re-read, governance or installer fault: the staging folder
            # never outlives the spent token.
            await asyncio.shield(_discard_off_loop([prepared]))
            raise

    async def _complete_prepared(
        self, ref: str, plan_digest: str, prepared: Any, confirmed: bool
    ) -> "CapabilityResult":
        from kiro_crew.platform.interfaces import CapabilityResult

        if confirmed is not True:
            await _discard_off_loop([prepared])
            return CapabilityResult(ok=False, message="installing needs an explicit yes")
        # The preview turned off while the yes was pending: install nothing.
        if not await asyncio.to_thread(market_apps_enabled):
            await _discard_off_loop([prepared])
            return CapabilityResult(
                ok=False,
                message="installing apps from Kiro Space Discover is turned off; "
                "nothing was installed",
            )
        # Governance is asked again: a ceiling tightened between apply and the yes
        # still stops the install.
        denied = await _off_loop(_gov.extensions_off, "market_apply")
        if denied:
            await _discard_off_loop([prepared])
            return _refused(denied)
        plan = await self.market_plan(ref)
        if not isinstance(plan, dict) or plan.get("planDigest") != plan_digest:
            await _discard_off_loop([prepared])
            return CapabilityResult(
                ok=False,
                message="the plan changed since it was shown; nothing was installed",
            )
        # Every item is asked again, as at apply: a list entry or a theme ceiling
        # tightened while the yes was pending still stops the install.
        refused = await _plan_items_denied(plan, "capability_market_confirm")
        if refused is not None:
            await _discard_off_loop([prepared])
            return refused
        result = await _install_holding_lifecycle_lock(prepared)
        if not result.ok:
            return MarketAppInstallFailed(ok=False, message=str(result.error or "install failed"))
        # The install wrote a new proxy key: drop any key cached from an earlier
        # install, as the local-path install route does.
        from kiro_crew.apps.routes_proxy import invalidate_app_secret_cache

        invalidate_app_secret_cache(result.name)
        return CapabilityResult(ok=True, message="")


async def _install_holding_lifecycle_lock(prepared: Any) -> Any:
    """Run the confirmed install under the app's lifecycle lock, bounded in time.

    Uninstall and every other install path hold ``app_lifecycle_lock(name)``, so
    without it a confirm could interleave with an uninstall of the same app and
    delete the data that uninstall preserves. A timeout or cancellation answers
    the caller at once, but the worker thread keeps copying, so the lock is only
    released once that thread has finished.
    """
    from kiro_crew.apps.manager import app_lifecycle_lock

    lock = app_lifecycle_lock(prepared.listing_name)
    await lock.acquire()
    worker = asyncio.ensure_future(
        asyncio.to_thread(_app_installer.complete_market_install, prepared, confirmed=True)
    )
    try:
        result = await asyncio.wait_for(asyncio.shield(worker), timeout=CAPABILITY_INSTALL_TIMEOUT)
    except BaseException:
        worker.add_done_callback(_release_after(lock))
        raise
    lock.release()
    return result


def _release_after(lock: Any) -> Any:
    """A done-callback that releases *lock* once the install thread has ended."""

    def _done(fut: "asyncio.Future[Any]") -> None:
        if not fut.cancelled():
            fut.exception()  # retrieved here, so it is not logged as never retrieved
        lock.release()

    return _done


async def _discard_off_loop(prepared: List[Any]) -> None:
    """Delete the staging folders of *prepared* installs in one worker thread.

    ``discard`` runs ``shutil.rmtree``, which must not block the gateway loop.
    """
    if not prepared:
        return

    def _run() -> None:
        for item in prepared:
            item.discard()

    await asyncio.to_thread(_run)


async def _plan_items_denied(plan: Dict[str, Any], operation: str) -> "Optional[CapabilityResult]":
    """Ask governance about every item in *plan*; the refusal, or ``None`` if allowed.

    A theme (alone or bundled) serves sandboxed JS into the dashboard, so it
    answers to the same capabilities.theme_install ceiling as a theme installed
    from the Themes page. A skill or MCP item is asked under the id it is
    installed as (the name a direct install_mcp / install_skill asks under) AND
    under the listing ref, so a list entry written either way binds both install
    paths. An item whose installed id cannot be read is refused (fail closed).
    Called at apply and again at an app's confirm.
    """
    from kiro_crew.platform.interfaces import CapabilityResult

    items = plan.get("items")
    items = items if isinstance(items, list) else []
    if any(isinstance(i, dict) and i.get("kind") == "theme" for i in items):
        denied = await _off_loop(_gov.theme_install_denied, operation)
        if denied:
            return _refused(denied)
    for item in items:
        kind = item.get("kind") if isinstance(item, dict) else None
        if kind not in ("mcp", "skill"):
            continue
        install_id = market_install_id(item)
        if install_id is None:
            return CapabilityResult(
                ok=False,
                message=f"cannot tell what {kind} the plan installs; nothing was installed",
            )
        for ident in (install_id, str(item.get("ref", ""))):
            denied = await _off_loop(_gov.extension_install_denied, kind, ident)
            if denied:
                return _refused(denied)
    return None


#: Late discards (a prepare that finished after its caller timed out), held so
#: the event loop keeps a reference until each one has run.
_LATE_DISCARDS: "set[asyncio.Future[None]]" = set()


def _discard_result_when_done(fut: "asyncio.Future[Any]") -> None:
    """Delete what a timed-out or cancelled prepare produced, once it has."""
    if fut.cancelled() or fut.exception() is not None:
        return
    late = asyncio.ensure_future(_discard_off_loop([fut.result()]))
    _LATE_DISCARDS.add(late)
    late.add_done_callback(_LATE_DISCARDS.discard)


def pinned_app_version(item: object) -> Optional[int]:
    """The integer release an app plan item pins, or ``None`` when it pins none."""
    if not isinstance(item, dict):
        return None
    version = item.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return None
    return version


def _command_texts(commands: object) -> List[str]:
    out: List[str] = []
    for c in commands if isinstance(commands, list) else []:
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.append(" ".join(str(a) for a in c))
    return sorted(out)


def _str_set(values: object) -> List[str]:
    return sorted(str(v) for v in values) if isinstance(values, list) else []


def _prepared_mismatch(
    prepared: Any, root: Dict[str, Any], version: int, plan_permissions: object
) -> Optional[str]:
    """Name the first way the prepared release differs from the plan, or ``None``.

    The person approved the plan's release, sha256, commands and secrets; the
    prepared one must be exactly that release or no token is handed out.
    """
    resolved = getattr(prepared, "resolved", None)
    if resolved is None:
        return "the prepared app"
    if getattr(resolved, "ref", None) != root.get("ref"):
        return "the app's listing"
    if getattr(resolved, "version", None) != version:
        return "the app's version"
    if getattr(resolved, "content_sha256", None) != root.get("sha256"):
        return "the app's sha256"
    perms = getattr(resolved, "permissions", None) or {}
    shown = plan_permissions if isinstance(plan_permissions, dict) else {}
    if _command_texts(perms.get("commands")) != _command_texts(shown.get("commands")):
        return "the app's commands"
    if _str_set(perms.get("secrets")) != _str_set(shown.get("secrets")):
        return "the app's secrets"
    return None


def sweep_stale_market_staging(max_age: float = MARKET_APP_PENDING_TTL) -> int:
    """Delete ``install-*`` staging folders older than *max_age* seconds.

    A gateway that stopped with an app waiting for the yes leaves its staging
    folder behind; the pending map that would have expired it is gone. Run at
    startup in a worker thread. Returns how many folders were deleted.
    """
    root = _app_installer.market_staging_root()
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age
    removed = 0
    for child in root.iterdir():
        try:
            if (
                not child.name.startswith("install-")
                or child.is_symlink()
                or not child.is_dir()
                or child.stat().st_mtime >= cutoff
            ):
                continue
        except OSError:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    return removed


def bind_capability_manager(inner: "CapabilityManager") -> "CapabilityManager":
    """Return *inner* wrapped in :class:`BoundedCapabilityManager` (idempotent).

    Called from ``PlatformContext.__post_init__`` on every composition, including
    the companion's ``dataclasses.replace`` path. Idempotent so a ``replace``
    that copies an already-bounded manager forward is not double-wrapped.
    """
    if isinstance(inner, BoundedCapabilityManager):
        return inner
    return BoundedCapabilityManager(inner)
