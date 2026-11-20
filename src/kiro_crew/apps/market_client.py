"""HTTP :class:`~kiro_crew.apps.marketplace_install.MarketClient` for Kiro Space.

Fills the installer's ``ResolvedApp`` and ``ListingDownload`` from the listing
API's ``GetInstallPlan`` and ``GetListingDownload`` operations
(the Kiro Space marketplace-core spec, sections 5 and 5.4). The API
speaks Smithy ``rpcv2Cbor``: one ``POST /service/KiroSpace/operation/<Op>`` with
a CBOR body and the ``smithy-protocol: rpc-v2-cbor`` header, authorised by the
caller's bearer token. The signed download URL is fetched with no credential.

The plan does not yet name an app's crons, MCP servers or agents, nor where a
bundled component sits in the zip. Those fields are left empty, never guessed,
so the installer refuses such an app (``market_undeclared_capability``, or a
malformed component) until the API carries them.

It also reads the store for the App Store's Kiro Space source: ``ListListings``
(one page) and ``GetListing`` (one listing), answered as the API's own
``ListingView`` dicts. ``apps/market_registries.py`` turns those into card rows.
The client is built by a store provider (``app["market_registries"]``); the
open-source build ships none.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

import httpx

from kiro_crew.apps.marketplace_install import (
    ListingDownload,
    MarketInstallError,
    ResolvedApp,
)
from kiro_crew.apps.rpc_cbor import cbor_decode, cbor_encode

SERVICE = "KiroSpace"
#: Largest API answer read; an install plan is at most 25 nodes.
MAX_RPC_BYTES = 1024 * 1024
TIMEOUT_S = 30.0
_CHUNK = 64 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def _https_or_loopback(url: str) -> bool:
    # Plain http is accepted only for a loopback host (a local test or dev
    # server); nothing off this machine is ever reached without TLS.
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return True
    return parts.scheme == "http" and _is_loopback(parts.hostname)


def _invalid(message: str) -> MarketInstallError:
    return MarketInstallError("market_resolve_invalid", message)


def _str(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise _invalid(f"install plan field {key!r} is missing")
    return value


def _int(obj: dict[str, Any], key: str) -> int:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _invalid(f"install plan field {key!r} is missing")
    return value


def _strings(raw: Any) -> list[str]:
    return [s for s in raw if isinstance(s, str)] if isinstance(raw, list) else []


def _permissions(raw: Any) -> dict[str, list[str]]:
    perms = raw if isinstance(raw, dict) else {}
    network = perms.get("network")
    if network == "ANY":
        hosts = ["*"]
    elif network == "HOSTS":
        hosts = _strings(perms.get("hosts"))
    else:
        hosts = []
    commands = perms.get("commands")
    return {
        "commands": (
            [" ".join(_strings(argv)) for argv in commands if isinstance(argv, list)]
            if isinstance(commands, list)
            else []
        ),
        "network": hosts,
        "files": _strings(perms.get("files")),
        "secrets": _strings(perms.get("secrets")),
        # Not in ListingPermissions yet: empty, so an app that has any is refused.
        "background": [],
        "mcpServers": [],
        "agents": [],
    }


def _publisher(node: dict[str, Any]) -> str:
    view = node.get("publishedBy")
    if isinstance(view, dict) and isinstance(view.get("display"), str) and view["display"]:
        return view["display"]
    ref = node.get("publisher")
    if isinstance(ref, dict):
        return f"{ref.get('provider', '')}:{ref.get('id', '')}"
    return ""


class HttpMarketClient:
    """Reaches the Kiro Space listing API over HTTPS with a bearer token."""

    def __init__(
        self,
        endpoint: str,
        *,
        token: Callable[[], str],
        timeout: float = TIMEOUT_S,
    ) -> None:
        if not _https_or_loopback(endpoint):
            raise ValueError("market endpoint must be an https:// URL")
        self._endpoint = endpoint.rstrip("/")
        self._token = token
        self._timeout = timeout
        # listingId of each version this client resolved: GetListingDownload
        # takes the listing id, the installer passes the ref.
        self._listing_ids: dict[tuple[str, int], str] = {}

    def _call(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._endpoint}/service/{SERVICE}/operation/{op}"
        headers = {
            "smithy-protocol": "rpc-v2-cbor",
            "Content-Type": "application/cbor",
            "Accept": "application/cbor",
            "Authorization": f"Bearer {self._token()}",
        }
        try:
            with httpx.Client(timeout=self._timeout, follow_redirects=False) as http:
                with http.stream("POST", url, content=cbor_encode(body), headers=headers) as resp:
                    raw = bytearray()
                    for chunk in resp.iter_bytes(_CHUNK):
                        raw += chunk
                        if len(raw) > MAX_RPC_BYTES:
                            raise MarketInstallError(
                                "market_api_invalid", f"{op} answer is too large"
                            )
                    status = resp.status_code
        except httpx.HTTPError as exc:
            raise MarketInstallError(
                "market_api_unreachable", f"{op} failed: {type(exc).__name__}"
            ) from exc
        try:
            answer = cbor_decode(bytes(raw)) if raw else {}
        except (ValueError, UnicodeDecodeError) as exc:
            # A 401 means "no usable credential" whoever wrote the body (a
            # proxy may answer it in plain text): the person must sign in again.
            if status == 401:
                raise MarketInstallError("market_signed_out", f"{op} answered 401") from exc
            raise MarketInstallError("market_api_invalid", f"{op} answer is not CBOR") from exc
        if status != 200:
            detail = answer if isinstance(answer, dict) else {}
            code = detail.get("code") or detail.get("__type") or "error"
            message = detail.get("message") or ""
            # Only 401 is a sign-out. A 403 stays a refusal: the store can
            # also answer 403 for a business rule, not only a dead session.
            raise MarketInstallError(
                "market_signed_out" if status == 401 else "market_api_refused",
                f"{op} answered {status} {code}: {message}".rstrip(": "),
            )
        if not isinstance(answer, dict):
            raise MarketInstallError("market_api_invalid", f"{op} answer is not an object")
        return answer

    def resolve_install(self, ref: str, version: int | None) -> ResolvedApp:
        body: dict[str, Any] = {"ref": ref}
        if version is not None:
            body["version"] = version
        nodes = self._call("GetInstallPlan", body).get("nodes")
        nodes = [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []
        requested = [n for n in nodes if n.get("reason") == "REQUESTED"]
        if len(requested) != 1:
            raise _invalid("install plan has no single requested listing")
        node = requested[0]
        if node.get("kind") != "app":
            raise _invalid("listing is not an app")
        if node.get("installable") is not True or node.get("yanked") is not False:
            raise _invalid("listing version is not installable")
        sha = _str(node, "sha256")
        if not _SHA256_RE.fullmatch(sha):
            raise _invalid("install plan sha256 is invalid")
        resolved_ref, resolved_version = _str(node, "ref"), _int(node, "version")
        self._listing_ids[(resolved_ref, resolved_version)] = _str(node, "listingId")
        components = tuple(
            {
                "name": _str(n, "ref").partition("#")[2],
                "kind": _str(n, "kind"),
                "sha256": _str(n, "sha256"),
            }
            for n in nodes
            if n.get("reason") == "BUNDLED"
        )
        requires = tuple(
            {"ref": _str(n, "ref"), "version": _int(n, "version"), "kind": _str(n, "kind")}
            for n in nodes
            if n.get("reason") == "DEPENDENCY"
        )
        return ResolvedApp(
            ref=resolved_ref,
            version=resolved_version,
            content_sha256=sha,
            size=_int(node, "sizeBytes"),
            published_by=_publisher(node),
            app_version=node.get("semver") if isinstance(node.get("semver"), str) else "",
            permissions=_permissions(node.get("permissions")),
            components=components,
            requires=requires,
        )

    def get_download(self, ref: str, version: int) -> ListingDownload:
        listing_id = self._listing_ids.get((ref, version))
        if listing_id is None:
            raise _invalid("resolve the version before downloading it")
        answer = self._call(
            "GetListingDownload", {"listingId": listing_id, "version": version, "explicit": True}
        )
        if answer.get("version") != version:
            raise MarketInstallError(
                "market_download_refused", "download answered a different version"
            )
        url, sha = answer.get("url"), answer.get("sha256")
        if not isinstance(url, str) or not isinstance(sha, str):
            raise MarketInstallError("market_download_refused", "download answer is incomplete")
        return ListingDownload(url=url, sha256=sha)

    def list_listings(
        self, *, kind: str = "app", cursor: str | None = None, limit: int = 100
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One ``ListListings`` page: the ``ListingView`` items and the next cursor."""
        body: dict[str, Any] = {"kind": kind, "limit": limit}
        if cursor:
            body["cursor"] = cursor
        answer = self._call("ListListings", body)
        items = answer.get("items")
        if not isinstance(items, list):
            raise MarketInstallError("market_api_invalid", "ListListings answer has no items")
        next_cursor = answer.get("nextCursor")
        return (
            [i for i in items if isinstance(i, dict)],
            next_cursor if isinstance(next_cursor, str) and next_cursor else None,
        )

    def get_listing(self, ref: str) -> dict[str, Any]:
        """``GetListing`` by ``<namespace>/<name>``: the ``ListingView`` dict."""
        listing = self._call("GetListing", {"ref": ref}).get("listing")
        if not isinstance(listing, dict):
            raise MarketInstallError("market_api_invalid", "GetListing answer has no listing")
        return listing

    def open_download(self, url: str) -> Iterator[bytes]:
        # The signed URL carries its own authorisation: no bearer is sent.
        if not _https_or_loopback(url):
            raise MarketInstallError("market_download_refused", "signed URL must use https")
        try:
            with httpx.Client(timeout=self._timeout, follow_redirects=False) as http:
                with http.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        raise MarketInstallError(
                            "market_download_refused", f"download answered {resp.status_code}"
                        )
                    yield from resp.iter_bytes(_CHUNK)
        except httpx.HTTPError as exc:
            raise MarketInstallError(
                "market_api_unreachable", f"download failed: {type(exc).__name__}"
            ) from exc
