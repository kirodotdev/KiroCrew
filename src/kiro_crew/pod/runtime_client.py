"""The control plane's client of a running pod's gateway.

Three calls, each gated on :mod:`kiro_crew.pod.runtime_attestation`'s verdict
about who answers the pod's port: :func:`health` (an identity-gated HTTP probe),
:func:`mint_token` (the pod's internal-API credential, sent only to the attested
gateway over the pod's private unix socket) and :func:`pod_api` (one
authenticated request whose token the caller never handles).

Core pod facts -- activity, the socket path, name validation, the platform flags
-- are read from :mod:`kiro_crew.pod.runtime` at call time, the namespace the pod
suite patches. The transports this module imports are its own seams: the runtime
module forwards reads and patches of them here.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from kiro_crew.instances import run_marker
from kiro_crew.loopback_http import loopback_urlopen, unix_socket_urlopen
from kiro_crew.mcp_gateway.socketsec import get_peer_pid
from kiro_crew.pod import runtime, runtime_attestation, runtime_ports
from kiro_crew.pod.config import PodConfig
from kiro_crew.pod.runtime import PodError, PodOwnershipUnproven

#: :func:`health` verdict for a port that answers but is NOT served by this pod.
#: Negative like ``_wait_healthy``'s crash sentinel, so it can never collide with
#: an HTTP status or with ``0`` (unreachable).
HEALTH_FOREIGN = -2


def _pod_secret_path(cfg: PodConfig, name: str, port: int) -> Path:
    """Path of pod *name*'s per-listener credential inside its isolated home."""
    return cfg.home_dir(name) / run_marker.RUN_DIR_NAME / run_marker.secret_file_name(port)


def _probe_health(port: int, timeout: int = 3) -> int:
    """Raw HTTP status of ``/api/health`` on *port*, or 0 if unreachable.

    Reachability ONLY — it says nothing about who answered, which is why it is
    private. Callers want :func:`health`.

    Every failure collapses to 0, ``http.client.HTTPException`` included: a
    process holding the port that answers the TCP handshake without speaking
    HTTP raises ``BadStatusLine``, which is neither ``OSError`` nor ``URLError``.
    That case is not hypothetical here — a foreign listener on a derived port is
    the reason this probe is being hardened — and it must read as "not serving"
    rather than escaping as a traceback out of ``pod status``.
    """
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        # Loopback-only probe to the pod's own gateway on 127.0.0.1; the URL is
        # internally derived (never attacker-supplied), so the dynamic-URL SSRF
        # audit rule is a false positive here.
        with loopback_urlopen(url, timeout=timeout) as resp:  # nosemgrep
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return 0


def health(cfg: PodConfig, name: str, port: int, timeout: int = 3) -> int:
    """HTTP status of **this pod's** ``/api/health``, or a non-positive verdict.

    * 200 = open; 401/403 = serving but gated — all three mean this pod is up.
    * 0 = nothing reachable on the port.
    * :data:`HEALTH_FOREIGN` = the port answers, but the responder is provably
      not this pod, so this pod is NOT up.

    The ownership check runs only once something has answered, which keeps the
    common case free: a stopped pod costs one refused connection and no process
    lookup, exactly as before.
    """
    code = _probe_health(port, timeout)
    if code == 0:
        return 0
    if runtime_attestation.port_owner(cfg, name, port) == runtime_attestation.OWNER_FOREIGN:
        return HEALTH_FOREIGN
    return code


# --------------------------------------------------------------------------- #
# Token mint — reads the pod's OWN internal-API credential (in its isolated
# HOME, per listener first), then calls /api/token/local with X-Local-Secret over
# the pod's private unix socket. Keeps the secret read inside this process
# (never an agent-issued `cat`), and keeps the secret's delivery inside the pod's
# owner-only home (never a rebindable loopback port). The connected socket's peer
# is kernel-verified against the attested gateway pid before any bytes are sent,
# so even a same-UID rebind of the socket path receives nothing.
# --------------------------------------------------------------------------- #
def _attested_gateway_verifier(cfg: PodConfig, name: str, port: int, socket_path: Path):
    """Build a connect-time peer check pinned to pod *name*'s attested gateway.

    ``port_owner`` proves the pid RECORD is fresh, but a record cannot prove
    who answers the socket FILE: the path sits in a directory its owner can
    always rewrite, so a same-UID process can unlink it and bind its own
    listener there — and the mint request would hand that listener the pod's
    internal-API credential. The kernel can prove it: peer credentials on the
    connected socket (``SO_PEERCRED`` on Linux, ``LOCAL_PEERPID`` on macOS)
    name the listener's pid as of ``listen()``, so requiring that pid to equal
    the attested gateway pid refuses a rebound socket BEFORE any HTTP bytes.
    The concrete adversary is a CONFINED same-UID process — a sandboxed
    agent subprocess whose filesystem policy still reaches this owner-writable
    directory — which can rebind the path but cannot fake its kernel-reported
    pid. Deny-by-default, same shape as the server-side admission this feature
    added: a mismatched peer and an unreadable peer both refuse.
    """
    attested = runtime_attestation._pod_recorded_pid(cfg, name, port)
    if attested is None:
        raise PodOwnershipUnproven(
            f"withholding pod {name!r}'s credential: its gateway pid record "
            f"could not be re-proven at send time, so the process answering "
            f"{socket_path} cannot be verified. {runtime_attestation._unproven_remedy(cfg, name, port)}"
        )

    def _verify(sock: socket.socket) -> None:
        peer = get_peer_pid(sock)
        # Re-prove the record on the connected socket so a pid recycled between
        # the record read and connect cannot attest.
        current_attested = runtime_attestation._pod_recorded_pid(cfg, name, port)
        if current_attested is None or peer != current_attested:
            who = "an unidentifiable process" if peer is None else f"pid {peer}"
            expected = (
                "no gateway pid is currently attested"
                if current_attested is None
                else f"the currently attested gateway is pid {current_attested}"
            )
            raise PodError(
                f"refusing to send pod {name!r}'s credential: {socket_path} is "
                f"answered by {who}, which is not the currently attested gateway; "
                f"{expected}. The socket path may have been rebound since the pod "
                f"started; `kirocrew pod status {name}` shows the gateway's state."
            )

    return _verify


# Recreating a pod is the only restart the pod CLI offers, and `down` reclaims the
# pod's HOME, so every remedy that suggests it states that cost. It is offered only
# where it can actually repair the refusal, which is the pre-code gateway whose
# transport refusal may really be a missing AF_UNIX admission. A gate that judges
# who connected is never offered it, and neither is a credential mismatch: a live
# second gateway in the same home produces one without the pod's state being wrong.
_POD_RECREATE = (
    "`kirocrew pod down {name} && kirocrew pod up {name}` — and `down` DELETES the "
    "pod's HOME, so copy anything you still need out of it first"
)


# Every 403 this route sends is a fixed short JSON literal, so a longer reply is not
# one of them and holds no ``code`` worth reading. Bounding the read keeps a single
# oversized reply from costing the one-shot CLI its own length in memory, and a body
# over the cap is treated exactly like an unreadable one: the operator is sent to the
# pod's audit record rather than to a cause parsed out of an untrusted length.
_MINT_403_BODY_CAP = 64 * 1024


#: Longest slice of a pod's own ``error`` text this module echoes to the terminal.
_MAX_ECHOED_DETAIL_LEN = 64


def _terminal_safe_detail(detail: str) -> str:
    """Reduce a pod's ``error`` text to characters safe to print.

    A pod's gateway runs that pod's own worktree checkout, so this text is chosen
    by the code under test rather than by this process, and it lands on the
    operator's terminal through :func:`kiro_crew.pod.cli._die`, which prints it
    unchanged. Every non-printable code point is dropped — C0/C1 controls, so ESC
    and BEL, which start and end ANSI SGR and OSC sequences, and Unicode format
    characters — and the result is capped, so a crafted reply can neither drive the
    terminal nor flood the line. ``str.isprintable`` is the filter, which also
    rejects newlines, so the reply cannot forge extra lines of this message; it
    keeps letters, digits, punctuation and ordinary spaces of every script without
    enumerating escape grammars.

    Same filter and cap as :func:`kiro_crew.cli_server._terminal_safe_name`, which
    reduces an untrusted process name for the same reason.
    """
    return "".join(ch for ch in detail if ch.isprintable())[:_MAX_ECHOED_DETAIL_LEN]


def _mint_403_cause(exc: urllib.error.HTTPError, name: str) -> str:
    """The pod's OWN reason for refusing a token mint, as the remedy that matches it.

    ``/api/token/local`` refuses at three independent gates — transport admission,
    the credential comparison, and owner provenance — and returns a
    machine-readable ``code`` for each, alongside the same distinction in its
    ``security_events.jsonl`` record. Reading that code is what lets a refusal name
    the gate that applied instead of listing candidates, which matters because the
    remedies differ and none of the three is a recreate. Transport admission and
    owner provenance both judge WHO CONNECTED, so recreating the pod clears neither;
    a credential mismatch has two causes and a live second gateway in the same home
    produces one of them without the pod's state being wrong. A recreate deletes the
    pod's HOME.

    A gateway that sends any of these codes carries this endpoint's current shape, so
    the one cause that a worktree update plus a recreate does fix — a gateway too old
    to admit ``AF_UNIX`` on this route at all — can only appear on a reply carrying NO
    code, and is named only there.

    Never raises. An unreadable, over-long, non-JSON or ``code``-less body yields
    guidance that sends the operator to the pod's own audit record rather than to a
    guess. Where the pod's own ``error`` text is quoted back, it is reduced to
    printable characters and capped first, because it reaches the terminal unchanged.
    """
    try:
        raw = exc.read(_MINT_403_BODY_CAP + 1)
    except Exception:
        raw = b""
    if len(raw) > _MINT_403_BODY_CAP:
        raw = b""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    code = payload.get("code") if isinstance(payload.get("code"), str) else ""
    sent = payload.get("error")
    detail = _terminal_safe_detail(sent) if isinstance(sent, str) else ""
    recreate = _POD_RECREATE.format(name=name)
    if code == "member_owner_token_refused":
        return (
            "the gateway did not accept this CALLER as the pod's local owner "
            "(code member_owner_token_refused). The transport was admitted and the "
            "pod's internal-API credential was accepted; the process behind them was "
            "refused, "
            "and the pod records that as resources=unverified-owner-process in its "
            "security_events.jsonl.\n"
            "  Recreating the pod does NOT change this verdict — the verdict is "
            "about which process called, not about the pod's state or its secret — "
            "and `kirocrew pod down` DELETES the pod's HOME.\n"
            "  The gate admits a caller whose process start id the gateway can read "
            "and which its own platform accepts as an ordinary host process: on Linux "
            "that is a caller sharing the gateway's user and mount namespaces, so a "
            "different namespace is refused even at the same uid, and on macOS it is a "
            "caller running outside a sandbox profile. Mint from a host process that "
            "meets this platform's condition."
        )
    if code == "loopback_only":
        return (
            "the gateway refused this TRANSPORT before it read the secret (code "
            "loopback_only), and records it as resources=non-loopback in its "
            "security_events.jsonl. A gateway that sends this code carries the "
            "unix-socket admission on /api/token/local, so it did not reject the "
            "socket for being one: it declined to confirm the connecting peer as its "
            "own principal. That admission takes only a positive kernel MATCH, so a "
            "mismatched peer credential and an unverifiable one are both refused.\n"
            "  Recreating the pod does NOT change this verdict either, because it is "
            "about who connected rather than about the pod's state, and `kirocrew pod "
            "down` DELETES the pod's HOME. Check that this caller runs as the uid that "
            "owns the pod's home, and that the kernel can report peer credentials on "
            "this socket at all: a credential it cannot report is refused exactly like "
            "one that names another principal."
        )
    if code == "invalid_secret":
        return (
            "the gateway rejected the credential this mint sent as not matching "
            "its own (code invalid_secret). Two different things look identical "
            "here, and only one of them is the pod's state: a credential left "
            "behind by an earlier run, or a live second gateway in this pod's home "
            "holding the shared .local_secret slot while the port belongs to "
            "another generation.\n"
            "  The pod's run directory tells them apart: the per-listener file for "
            "this port carries the credential of the process that owns it, and the "
            "mint prefers it. If that file is absent while the pod is serving, its "
            "gateway predates the per-listener credential and only then is the "
            "shared slot the whole story."
        )
    said = f" It said: {detail}" if detail else ""
    return (
        "the pod sent no machine-readable cause, so its gateway is older than these "
        f"codes or its reply could not be read.{said}\n"
        "  The pod's own security_events.jsonl names the gate in its token.local "
        "record: resources=non-loopback is transport admission, invalid-secret is "
        "the secret comparison, and unverified-owner-process is caller provenance. "
        "On a gateway that old, non-loopback can also mean it predates unix-socket "
        "admission on /api/token/local and refuses this transport whatever the secret; "
        f"that cause is repaired by updating the pod's worktree before recreating it: "
        f"{recreate}. Caller provenance is repaired by neither, and a credential "
        "mismatch only when no second gateway is live in this pod's home."
    )


def _pod_secret_candidates(cfg: PodConfig, name: str, port: int) -> tuple[Path, Path]:
    """The two files a pod's internal-API credential can live in, in read order.

    ONE definition, because two callers must agree about it: the mint below reads
    these paths, and :func:`published_credential` reports what a caller would find
    there. A reader that checked a different pair than the mint uses would report a
    pod ready whose credential the mint cannot find, which is the exact failure that
    reader exists to prevent.
    """
    return (_pod_secret_path(cfg, name, port), cfg.home_dir(name) / ".local_secret")


def _read_pod_secret_file(path: Path) -> str:
    """The credential recorded at *path*, or ``""`` when it cannot be read there.

    Explicit ``utf-8``, never the locale default, because ``read_text()`` without
    an encoding decodes with whatever the host prefers: a Windows runner reading
    cp1252 accepts byte sequences a UTF-8 host rejects, so the same corrupt file
    would be a credential on one machine and an error on another. The gateway
    writes ASCII, so pinning the encoding changes nothing about a healthy file and
    makes an unhealthy one behave the same everywhere.

    ``ValueError`` is caught beside ``OSError`` because ``UnicodeDecodeError`` is a
    ``ValueError``, NOT an ``OSError``. Both mean the same thing to every caller --
    "no credential HERE" -- so both fall through to the next candidate, and a
    corrupt per-listener file lets the shared ``.local_secret`` still answer
    instead of turning into a traceback out of a mint or a boot poll.
    """
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def published_credential(cfg: PodConfig, name: str, port: int) -> str:
    """Pod *name*'s currently published credential for *port*, or ``""``.

    The VALUE, not a boolean, because a caller that just started a gateway has to
    tell one GENERATION's credential from another's and a predicate cannot express
    that. Presence is not proof of a live pod: ``clear_marker`` runs only on a
    graceful shutdown and the stale-marker prune deliberately never removes the
    credential, so a pod home that survived a crash still holds the PREVIOUS
    generation's ``run/gateway-<port>.secret``. A boot that read that as
    "published" would call the pod ready the instant it looked, and be handed a
    secret the new gateway never minted.

    A pod's gateway publishes this only AFTER its listener is bound, so an
    answering ``/api/health`` does NOT imply the credential exists yet: the
    listener is already accepting while the remaining post-bind startup work runs,
    and the credential write sits at the end of it. A caller that treats health as
    the whole readiness signal races that write and reads nothing.

    Reads through the same helper and the same path pair as the mint, so a
    non-empty return means exactly "the mint would find this" -- including the
    emptiness test, because the file is created and then written, and a caller that
    stopped at presence could report a pod ready whose credential is still blank.
    """
    for candidate in _pod_secret_candidates(cfg, name, port):
        secret = _read_pod_secret_file(candidate)
        if secret:
            return secret
    return ""


def _pod_mint_secret(cfg: PodConfig, name: str, port: int) -> str:
    """Pod *name*'s internal-API credential for the gateway on *port*.

    Resolution is per LISTENER first, then the shared ``.local_secret``, which is
    the order :func:`kiro_crew.config.loader.read_local_secret` states as the
    invariant. That helper cannot be reused here because it resolves against the
    CALLING process's data home, while this reads a pod's isolated one.

    The order is what keeps the mint honest rather than merely tidy. The shared
    file holds one slot per data home, and a gateway starting while a sibling is
    still serving on another port publishes its credential ONLY to the per-port
    file, leaving the shared slot pointing at the sibling. A caller reading the
    shared slot then authenticates as one generation while dialling another and
    is refused, which is indistinguishable at the wire from a secret left behind
    by an earlier run.

    Raises :class:`PodError` when neither file can be read, naming both.
    """
    per_port, shared = _pod_secret_candidates(cfg, name, port)
    for candidate in (per_port, shared):
        secret = _read_pod_secret_file(candidate)
        if secret:
            return secret
    raise PodError(
        f"no internal-API credential for pod {name!r} — is it running? Looked for "
        f"{per_port}, then {shared}."
    )


def mint_token(cfg: PodConfig, name: str, ttl: str = "2h") -> str:
    port = runtime_ports.derive_port(cfg, name)
    secret = _pod_mint_secret(cfg, name, port)
    owner = runtime_attestation.port_owner(cfg, name, port)
    if owner != runtime_attestation.OWNER_POD:
        # Positive proof kept even though the send below rides the pod's own
        # unix socket: the pre-check costs one process lookup and buys the
        # refusal messages below, which name WHY the pod cannot answer instead
        # of surfacing a bare connection error from the socket. The transport
        # is what makes the secret safe; this is what makes the failure legible.
        if owner == runtime_attestation.OWNER_FOREIGN:
            raise PodError(
                f"refusing to mint a credential for pod {name!r}: :{port} is held "
                f"by another process, not this pod's gateway. `kirocrew pod status "
                f"{name}` shows the same verdict; a credential minted here would "
                f"belong to whatever owns that port."
            )
        raise PodOwnershipUnproven(
            f"withholding a credential for pod {name!r}: could not prove "
            f"ownership of :{port} from the gateway pid record and the service "
            f"manager's current MainPID, so this call cannot prove which "
            f"process would receive the pod's secret. "
            f"{runtime_attestation._unproven_remedy(cfg, name, port)}"
        )
    if runtime.IS_WINDOWS:
        # Windows has no AF_UNIX, so the pod binds no dashboard socket there.
        # The mint rides the OWNER_POD-attested loopback port, the strongest
        # transport the platform offers.
        url = f"http://127.0.0.1:{port}/api/token/local?ttl={urllib.parse.quote(str(ttl))}"
        req = urllib.request.Request(url, headers={"X-Local-Secret": secret})
        try:
            with loopback_urlopen(req, timeout=5) as resp:  # nosemgrep
                token = json.loads(resp.read()).get("token", "")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PodError(f"token mint failed on :{port} ({name}): {exc}") from exc
        if not token:
            raise PodError(f"gateway returned empty token on :{port} ({name})")
        return token
    socket_path = runtime.pod_socket_path(cfg, name, port)
    verify_peer = _attested_gateway_verifier(cfg, name, port, socket_path)
    url = f"http://127.0.0.1:{port}/api/token/local?ttl={urllib.parse.quote(str(ttl))}"
    req = urllib.request.Request(url, headers={"X-Local-Secret": secret})
    try:
        # Over the pod's own unix socket, never TCP: this request CARRIES
        # the pod's internal-API credential, and the pod's TCP port is ordinary
        # loopback that any local user can bind the moment the pod releases it — with
        # no peer-credential API on the wire to tell the squatter from the gateway.
        # `unix_socket_urlopen` has no TCP handler, so "no fallback" is structural:
        # a missing, stale, or refusing socket raises instead of handing the
        # header to whatever answered. The URL keeps the loopback host so the
        # gateway's Host validation sees exactly what it saw on TCP; the socket
        # path is derived, never caller-supplied. verify_peer closes the residual
        # window on the socket itself: a same-UID process that rebinds the path
        # fails the kernel peer-pid check and never sees the header.
        with unix_socket_urlopen(  # nosemgrep
            req, timeout=5, socket_path=socket_path, verify_peer=verify_peer
        ) as resp:
            token = json.loads(resp.read()).get("token", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            # A 403 here is one of three distinct refusals, and the pod says which
            # in the body it already sends. Report that one: the remedies differ,
            # and the owner-provenance refusal is not repaired by the recreate the
            # other two want — a recreate deletes the pod's HOME.
            raise PodError(
                f"pod {name!r} refused the token mint over its API socket "
                f"(HTTP 403): {_mint_403_cause(exc, name)}\n"
                f"  Not retried on 127.0.0.1:{port} — the pod's secret must not "
                f"be sent to a process that is not this pod's gateway."
            ) from exc
        raise PodError(
            f"token mint for pod {name!r} did not complete over its API socket "
            f"({socket_path}): HTTP {exc.code}. Not retried on 127.0.0.1:{port} — "
            f"the pod's secret must not be sent to a process that is not this "
            f"pod's gateway."
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PodError(
            f"token mint for pod {name!r} did not complete over its API socket "
            f"({socket_path}): {exc}. Not retried on 127.0.0.1:{port} — the pod's "
            f"secret must not be sent to a process that is not this pod's gateway."
        ) from exc
    if not token:
        raise PodError(f"gateway returned empty token on :{port} ({name})")
    return token


# --------------------------------------------------------------------------- #
# Authenticated API front door. The caller never handles the pod credential:
# mint_token() proves ownership, then this module adds the query token expected
# by dashboard.token_auth — and sends it over the pod's private unix socket, so
# only the pod that owns the credential can ever receive it.
# --------------------------------------------------------------------------- #
API_METHODS: tuple[str, ...] = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")


API_READ_METHODS: tuple[str, ...] = ("GET", "HEAD")


API_TIMEOUT_SECS = 30


API_BODY_MAX_BYTES = 32 * 1024 * 1024


def api_path(path: str) -> str:
    """Return *path* as a rooted ``/api`` path without any credential.

    Full URLs are accepted but their authority is discarded: every request is
    still sent to the selected pod on loopback. A caller-supplied ``token`` query
    parameter is refused before host access; the error never repeats its value or
    the credential-bearing URL.
    """
    raw = path.strip()
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise PodError("invalid request path (value withheld)") from exc
    if parts.scheme or parts.netloc:
        raw = urllib.parse.urlunsplit(("", "", parts.path, parts.query, ""))
        try:
            parts = urllib.parse.urlsplit(raw)
        except ValueError as exc:  # defensive: the authority was already removed
            raise PodError("invalid request path (value withheld)") from exc
    try:
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    except ValueError as exc:
        raise PodError("invalid request query (value withheld)") from exc
    if any(key == "token" for key, _value in query):
        raise PodError(
            "the request path carries a `token` query parameter; refusing to "
            "forward or display it. `pod api` mints the pod credential itself — "
            "remove that parameter and retry."
        )
    normalized = parts.path or "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized != "/api" and not normalized.startswith("/api/"):
        normalized = "/api" + normalized
    if parts.query:
        normalized += "?" + parts.query
    return normalized


def _authenticated_url(port: int, path: str, token: str) -> str:
    parts = urllib.parse.urlsplit(api_path(path))
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.append(("token", token))
    target = urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query), fragment="")
    )
    return f"http://127.0.0.1:{port}{target}"


def _read_capped(stream: object, method: str, path: str, name: str) -> str:
    """Read and decode one response without buffering more than the hard cap."""
    try:
        chunk = stream.read(API_BODY_MAX_BYTES + 1)  # type: ignore[attr-defined]
    except (OSError, http.client.HTTPException) as exc:
        raise PodError(
            f"{method} {api_path(path)} on pod {name!r}: the response body could "
            f"not be read ({type(exc).__name__}).\n"
            f"  Is it healthy? kirocrew pod status {name}\n"
            f"  Its logs:      kirocrew pod logs {name}"
        ) from exc
    if len(chunk) > API_BODY_MAX_BYTES:
        raise PodError(
            f"{method} {api_path(path)} on pod {name!r} returned more than "
            f"{API_BODY_MAX_BYTES} bytes; refusing to buffer it. Narrow the request."
        )
    return chunk.decode("utf-8", "replace")


def _scrub_token_string(text: str, token: str) -> str:
    """Remove printable encodings of *token* from one string."""
    escaped = "".join(f"\\u{ord(char):04x}" for char in token)
    forms = {
        token,
        urllib.parse.quote(token, safe=""),
        urllib.parse.quote_plus(token, safe=""),
        escaped,
        escaped.upper().replace("\\U", "\\u"),
    }
    for form in sorted(forms, key=len, reverse=True):
        if form:
            text = text.replace(form, "<token>")
    return text


def _scrub_json_tokens(value: object, token: str) -> object:
    """Recursively scrub every JSON string key and value."""
    if isinstance(value, str):
        return _scrub_token_string(value, token)
    if isinstance(value, list):
        return [_scrub_json_tokens(item, token) for item in value]
    if isinstance(value, dict):
        return {
            _scrub_token_string(key, token): _scrub_json_tokens(item, token)
            for key, item in value.items()
        }
    return value


def _scrub_token(text: str, token: str) -> str:
    """Remove raw, encoded, and JSON-escaped forms of *token* from text."""
    if not token:
        return text
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return _scrub_token_string(text, token)
    try:
        scrubbed = _scrub_json_tokens(parsed, token)
    except RecursionError:
        return "<response omitted: JSON nesting exceeds scrubber limit>"
    return json.dumps(scrubbed, ensure_ascii=False)


def pod_api(
    cfg: PodConfig,
    name: str,
    method: str,
    path: str,
    *,
    data: str = "",
    allow_write: bool = False,
) -> tuple[int, str]:
    """Make one authenticated request to pod *name* and return status/body.

    The request is delivered over the pod's own dashboard unix socket and nowhere
    else. Its port is not a transport here, only the ``Host`` the gateway sees:
    the credential this function mints is valid as an ``mc_token_<port>`` cookie,
    so delivering it on a fresh TCP connection would hand it to whatever holds
    that port by then -- a pod that exits between the mint and the send leaves
    the port free for any local user to bind. The socket cannot be answered by
    another user, because it lives in the pod's owner-only home.
    """
    runtime.validate_name(name)
    method = method.upper()
    normalized = api_path(path)  # validation happens before any host access
    if method not in API_METHODS:
        raise PodError(f"unsupported method {method!r} (expected one of: {', '.join(API_METHODS)})")
    if method not in API_READ_METHODS and not allow_write:
        raise PodError(
            f"refusing to send {method} to pod {name!r}: `pod api` permits only "
            f"{', '.join(API_READ_METHODS)} unless --allow-write is passed.\n"
            f"  Retry: kirocrew pod api {name} {method} {normalized} --allow-write"
        )
    if not runtime.is_active(cfg, name):
        raise PodError(
            f"pod {name!r} is not running, so there is nothing to call.\n"
            f"  Start it:   kirocrew pod up {name}\n"
            f"  What is up: kirocrew pod ls"
        )
    port = runtime_ports.derive_port(cfg, name)
    socket_path = runtime.pod_socket_path(cfg, name, port)
    if runtime.IS_WINDOWS:
        # Answered BEFORE the existence check, because on this platform the file
        # is not missing -- it is never created. CPython on Windows exposes no
        # `AF_UNIX` (measured: `hasattr(socket, "AF_UNIX")` is False on 3.12
        # win32), so the pod's gateway binds no dashboard socket and no restart
        # can produce one. The generic refusal below would send the operator
        # round a diagnose-and-restart loop that cannot succeed and would reset
        # the pod's state each time, while never naming the cause or the way
        # through. There IS a way through, and it is the one the README
        # documents: mint a token and drive the loopback port yourself.
        raise PodError(
            f"`pod api` cannot reach pod {name!r} on Windows: it needs the pod's "
            "private AF_UNIX dashboard socket, and CPython on this platform has no "
            "AF_UNIX at all, so the pod binds none.\n"
            "  This is not a broken pod and a restart will not fix it.\n"
            f"  Do this instead: kirocrew pod token {name}\n"
            f"  then send your own authenticated request to 127.0.0.1:{port}.\n"
            "  `pod api` will not fall back to that port itself: the port is "
            "ordinary loopback, so a process that is not this pod can hold it, and "
            "the credential would go to whatever answered."
        )
    if not socket_path.exists():
        # Refuse BEFORE minting: `mint_token` sends the pod's internal-API
        # credential to obtain one, so a request that cannot be delivered must not
        # pay for it. Existence is checked only to produce this actionable message
        # -- correctness does not rest on it, because the send below has no TCP
        # path to fall back to if the file disappears in between.
        raise PodError(
            f"refusing to send {method} {normalized} to pod {name!r}: its private API "
            f"socket is not there ({socket_path}).\n"
            f"  `pod api` will not retry on 127.0.0.1:{port}. That port is ordinary "
            f"loopback, so a process that is not this pod can hold it, and the "
            f"credential this command mints would be sent to whatever answered.\n"
            f"  Is it healthy? kirocrew pod status {name}\n"
            f"  Its logs:      kirocrew pod logs {name}\n"
            f"  Restart it:    kirocrew pod down {name} && kirocrew pod up {name}"
        )
    token = mint_token(cfg, name)
    verify_peer = _attested_gateway_verifier(cfg, name, port, socket_path)
    url = _authenticated_url(port, normalized, token)
    body = data.encode("utf-8") if data else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        # Over the pod's own unix socket, never TCP: `unix_socket_urlopen` has no
        # TCP handler, so a dead or replaced listener cannot receive this token.
        # Caller input contributes only the path, never the host or the socket.
        # This is a NEW connection after the mint's, so it re-verifies the peer:
        # a socket rebound between the two sends would otherwise capture a live
        # (if short-TTL) credential.
        with unix_socket_urlopen(  # nosemgrep
            request,
            timeout=API_TIMEOUT_SECS,
            socket_path=socket_path,
            verify_peer=verify_peer,
        ) as response:
            raw = _read_capped(response, method, normalized, name)
            return response.status, _scrub_token(raw, token)
    except urllib.error.HTTPError as exc:
        raw = _read_capped(exc, method, normalized, name)
        return exc.code, _scrub_token(raw, token)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # urllib may include request.full_url in exception text. Do not render
        # exception values at all: the authenticated URL is credential-bearing.
        raise PodError(
            f"{method} {normalized} on pod {name!r} did not complete over its API "
            f"socket ({socket_path}): {type(exc).__name__}.\n"
            f"  Not retried on 127.0.0.1:{port} — the credential must not be sent "
            f"to a process that is not this pod.\n"
            f"  Is it healthy? kirocrew pod status {name}\n"
            f"  Its logs:      kirocrew pod logs {name}"
        ) from exc
