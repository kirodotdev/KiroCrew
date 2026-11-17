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

Nothing here is wired to a route or the dashboard yet.
"""

from __future__ import annotations

import ipaddress
import re
import struct
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

import httpx

from kiro_crew.apps.marketplace_install import (
    ListingDownload,
    MarketInstallError,
    ResolvedApp,
)

SERVICE = "KiroSpace"
#: Largest API answer read; an install plan is at most 25 nodes.
MAX_RPC_BYTES = 1024 * 1024
TIMEOUT_S = 30.0
_CHUNK = 64 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# CBOR (RFC 8949), the subset rpcv2Cbor uses: no third-party codec ships here.
# ---------------------------------------------------------------------------


def _head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([(major << 5) | n])
    for info, fmt in ((24, ">B"), (25, ">H"), (26, ">I"), (27, ">Q")):
        if n < 1 << (8 * struct.calcsize(fmt)):
            return bytes([(major << 5) | info]) + struct.pack(fmt, n)
    raise ValueError("integer too large for CBOR")


def cbor_encode(value: Any) -> bytes:
    """Encode None, bool, int, float, str, bytes, list/tuple and dict."""
    if value is None:
        return b"\xf6"
    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if isinstance(value, int):
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, float):
        return b"\xfb" + struct.pack(">d", value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _head(3, len(raw)) + raw
    if isinstance(value, (bytes, bytearray)):
        return _head(2, len(value)) + bytes(value)
    if isinstance(value, (list, tuple)):
        return _head(4, len(value)) + b"".join(cbor_encode(v) for v in value)
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(
            cbor_encode(k) + cbor_encode(v) for k, v in value.items()
        )
    raise TypeError(f"cannot CBOR-encode {type(value).__name__}")


_BREAK = object()
_MAX_DEPTH = 64


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError("truncated CBOR")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def arg(self, info: int) -> int | None:
        if info < 24:
            return info
        if info == 31:
            return None
        sizes = {24: 1, 25: 2, 26: 4, 27: 8}
        if info not in sizes:
            raise ValueError("invalid CBOR length")
        return int.from_bytes(self.take(sizes[info]), "big")

    def item(self, depth: int = 0) -> Any:
        if depth > _MAX_DEPTH:
            raise ValueError("CBOR nested too deep")
        ib = self.take(1)[0]
        major, info = ib >> 5, ib & 0x1F
        if major == 7:
            return self._simple(info)
        n = self.arg(info)
        if major == 0:
            return self._definite(n)
        if major == 1:
            return -1 - self._definite(n)
        if major in (2, 3):
            if n is None:
                parts = []
                while (part := self.item(depth + 1)) is not _BREAK:
                    if not isinstance(part, bytes if major == 2 else str):
                        raise ValueError("bad CBOR string chunk")
                    parts.append(part)
                return (b"" if major == 2 else "").join(parts)
            raw = self.take(n)
            return raw if major == 2 else raw.decode("utf-8")
        if major == 4:
            out: list[Any] = []
            while n is None or len(out) < n:
                v = self.item(depth + 1)
                if v is _BREAK:
                    if n is not None:
                        raise ValueError("unexpected CBOR break")
                    break
                out.append(v)
            return out
        if major == 5:
            obj: dict[Any, Any] = {}
            count = 0
            while n is None or count < n:
                k = self.item(depth + 1)
                if k is _BREAK:
                    if n is not None:
                        raise ValueError("unexpected CBOR break")
                    break
                obj[k] = self.item(depth + 1)
                count += 1
            return obj
        # major 6: a tag (e.g. 1 = epoch timestamp); the tagged value is kept.
        self._definite(n)
        return self.item(depth + 1)

    @staticmethod
    def _definite(n: int | None) -> int:
        if n is None:
            raise ValueError("indefinite length not allowed here")
        return n

    def _simple(self, info: int) -> Any:
        if info == 20:
            return False
        if info == 21:
            return True
        if info in (22, 23):
            return None
        if info == 25:
            return _half(int.from_bytes(self.take(2), "big"))
        if info == 26:
            return struct.unpack(">f", self.take(4))[0]
        if info == 27:
            return struct.unpack(">d", self.take(8))[0]
        if info == 31:
            return _BREAK
        raise ValueError("unsupported CBOR simple value")


def _half(h: int) -> float:
    exp, frac = (h >> 10) & 0x1F, h & 0x3FF
    sign = -1.0 if h & 0x8000 else 1.0
    if exp == 0:
        return sign * frac * 2.0**-24
    if exp == 31:
        return sign * float("inf") if frac == 0 else float("nan")
    return sign * (1 + frac / 1024) * 2.0 ** (exp - 15)


def cbor_decode(data: bytes) -> Any:
    """Decode exactly one CBOR item; trailing bytes or truncation raise ValueError."""
    reader = _Reader(data)
    value = reader.item()
    if value is _BREAK or reader.pos != len(data):
        raise ValueError("trailing CBOR bytes")
    return value


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
            raise MarketInstallError("market_api_invalid", f"{op} answer is not CBOR") from exc
        if status != 200:
            detail = answer if isinstance(answer, dict) else {}
            code = detail.get("code") or detail.get("__type") or "error"
            message = detail.get("message") or ""
            raise MarketInstallError(
                "market_api_refused", f"{op} answered {status} {code}: {message}".rstrip(": ")
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
