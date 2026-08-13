"""The ``browser`` tool: submit one op to the native browser command panel.

``schemas()`` is the ADVERTISEMENT half (name, model-facing description, the
JSON Schema a call is validated against) and ``HANDLERS`` maps the name to the
function that runs it. Both halves live here so the contract and the behavior
read together, and ``test_mcp_tool_registry`` fails if one arrives without the
other.

This tool is the agent-facing SUBMIT side of the native browser command bus.
``kirocrew-core`` is a separate stdio shim, so the handler cannot reach the bus
in-process: it POSTs to the gateway route ``/api/browser/command`` over loopback
with the internal-secret handshake -- the same shape ``mcp_computer`` uses to
reach the gateway. The Electron panel drains the bus and posts results back; the
bus itself lives in the gateway process.

Deliberately OUT of scope: this handler never runs ``playwright-cli`` itself and
carries no op->verb map. When no native panel is serving the session it returns
guidance text pointing at the ``playwright-cli`` verbs, and stops there.

Visibility follows the browse capability: ``schemas()`` returns ``[]`` while
``playwright-cli`` is not installed, so a machine where browsing was never set
up does not advertise a tool that cannot work. Presence of the CLI is the
consent gate for browsing on this host (see ``browser_cli.install``), and the
native panel is another expression of that same capability. The handler
re-checks because kiro-cli caches ``tools/list`` for the life of a session.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from kiro_crew import mcp_core
from kiro_crew.browser_cli import install
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.security import canonicalize_ip, redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# The gateway route the handler submits to. Loopback + ``X-Internal-Secret``;
# the handler answers HTTP 200 ``{ok: true, result}`` / 200 ``{ok: false, error}``
# / 503 ``{code: "no_native_panel"}`` when no Electron panel serves the session.
COMMAND_PATH = "/api/browser/command"
COMMAND_TIMEOUT_SECS = 90.0

# The ops the native panel understands. Advertised as an enum so the model
# cannot invent a verb the panel would reject. ``evaluate`` is intentionally
# withheld: arbitrary in-page JS is not needed to drive a page and widens the
# surface an untrusted page could influence.
BROWSER_OPS: tuple[str, ...] = (
    "navigate",
    "snapshot",
    "click",
    "type",
    "press_key",
    "hover",
    "select_option",
    "screenshot",
    "wait_for",
    "back",
    "console",
)

_FALLBACK_TEXT = (
    "No native browser panel is serving this session (a remote gateway, or a "
    "plain-browser dashboard with no Electron panel). Use the playwright-cli "
    "browser verbs directly instead -- e.g. `playwright-cli open <url>`, "
    "`snapshot`, `click <ref>`, `screenshot`."
)

# Distinct from _FALLBACK_TEXT: the built-in panel is available but the user
# deliberately turned it OFF in Settings -> Browser. Naming the real cause (the
# setting, not a missing panel) keeps the agent from relaying a false "no native
# panel" diagnosis and steering the user toward gateway/Electron debugging.
_DISABLED_TEXT = (
    "The built-in browser is turned off in Settings -> Browser, so browsing uses "
    "playwright-cli. Use the playwright-cli browser verbs directly -- e.g. "
    "`playwright-cli open <url>`, `snapshot`, `click <ref>`, `screenshot`."
)

# Per-op ceilings (ms) forwarded to the bus. A page load or an explicit
# `wait_for` legitimately exceeds the 15s default and would otherwise 504 (a
# hard error the agent cannot fall back from); give those the room the 90s
# client budget (COMMAND_TIMEOUT_SECS) already allows. Everything else acts on
# an already-loaded page and is fast.
_SLOW_OPS: frozenset[str] = frozenset({"navigate", "wait_for"})
_SLOW_OP_TIMEOUT_MS = 60000
_DEFAULT_OP_TIMEOUT_MS = 15000

_LOOPBACK_HOST_NAMES = {"localhost", "ip6-localhost", "ip6-loopback"}


def _navigate_target_is_public(url: str) -> bool:
    """True iff ``url`` is an ordinary public http(s) page.

    Mirrors the playwright-cli path's gate (``chat_runner._is_remote_navigable_host``):
    the built-in Electron panel accepts ANY http(s) URL, so without this a
    ``navigate`` op could drive a loopback control plane (this dashboard
    included) or the ``169.254.169.254`` metadata endpoint -- an SSRF the CLI
    path holds behind interactive approval. Kept LOCAL (not imported from the
    dashboard) so the ``kirocrew-core`` shim stays free of dashboard deps.

    ``is_global`` subsumes loopback/link-local/private/CGNAT/reserved in one
    predicate, applied to the address and any embedded IPv4 (``ipv4_mapped`` /
    ``sixtofour``). The host is first run through ``security.canonicalize_ip`` so
    an alternate IPv4 encoding the browser would still normalize -- decimal
    (``2852039166``), hex (``0x...``), octal, short/mixed, IPv6-mapped -- is
    resolved to dotted-quad and caught rather than mistaken for a DNS name. DNS
    names are NOT resolved (a blocking call + rebinding TOCTOU on the hot path);
    the residual public-name->private-IP risk is accepted, exactly as the CLI
    gate documents.
    """
    raw = url.strip()
    # Parser-differential SSRF: reject backslash (Chromium treats it as '/'),
    # and any control/whitespace char (tab/CR/LF and the rest), which browsers
    # strip or normalize but urlsplit does not -- e.g.
    # ``http://169.254.169.254\@example.com/`` parses to host ``example.com``
    # here yet Chromium navigates to the metadata endpoint. A legitimate URL
    # needs none of these (percent-encode instead), so refuse rather than parse.
    if "\\" in raw or " " in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return False
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or host in _LOOPBACK_HOST_NAMES or host.endswith(".localhost"):
        return False
    # Reject non-ASCII hosts: a fullwidth-digit / IDN form the browser may
    # normalize toward a private IP would otherwise slip through the DNS branch
    # below. Legitimate IDN sites are reached by their ASCII punycode (xn--...).
    if any(ord(ch) > 0x7F for ch in host):
        return False
    try:
        addr: Any = ipaddress.ip_address(canonicalize_ip(host))
    except ValueError:
        return True  # a DNS name (not an IP in any encoding); public browsing unaffected
    candidates = [addr]
    for embedding in ("ipv4_mapped", "sixtofour"):
        embedded = getattr(addr, embedding, None)
        if embedded is not None:
            candidates.append(embedded)
    return all(c.is_global for c in candidates)


def _args_are_scalar(op_args: dict[str, Any]) -> bool:
    """Reject non-scalar op arguments (e.g. an object-valued ``type.text``).

    Every browser op takes string/number/bool values (a url, a ref, text, a
    key) or a list of those; none takes a nested object. An object would reach
    the native panel and render as ``[object Object]``, so refuse it here rather
    than inserting garbage into the page.
    """
    for value in op_args.values():
        if value is None or isinstance(value, (str, int, float, bool)):
            continue
        if isinstance(value, list) and all(
            item is None or isinstance(item, (str, int, float, bool)) for item in value
        ):
            continue
        return False
    return True


def _browsing_available() -> bool:
    """True iff the browse capability is set up on this host. Fail-CLOSED.

    Delegates to ``browser_cli.install.available`` (``playwright-cli`` present),
    the same signal that authorizes browsing today: presence of the CLI IS the
    consent, and the native panel is another expression of that capability. Any
    error resolving it reads as "not available" so the tool stays hidden rather
    than advertising a capability that cannot work.
    """
    try:
        return install.available()
    except Exception:
        return False


def schemas() -> list[dict[str, Any]]:
    """Descriptor for the browser tool.

    Always advertised (never gated on ``_browsing_available()``): the tool
    registry requires every ``HANDLERS`` entry to have a descriptor, and vice
    versa, so an env-gated ``[]`` here would fail ``test_mcp_tool_registry`` on
    any host without ``playwright-cli`` (e.g. CI). When browsing is not set up
    ``browser()`` returns a clear "install it" error at CALL time -- graceful
    runtime degradation rather than hiding the tool.
    """
    return [
        {
            "name": "browser",
            "description": (
                "Drive the native browser panel: submit ONE operation to the page "
                "the dashboard's Browser panel is showing, in-process (no separate "
                "Chromium). `op` is the action and `args` its parameters, e.g. "
                "op='navigate' args={'url': 'https://…'}, op='click' "
                "args={'ref': '<snapshot-ref>'}, op='type' args={'ref': '…', "
                "'text': '…', 'submit': true}. Call op='snapshot' first to get "
                "element refs. When no native panel is serving this session "
                "(remote gateway / plain-browser dashboard) the tool tells you to "
                "use the playwright-cli browser verbs directly instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": list(BROWSER_OPS),
                        "description": "The browser operation to perform.",
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Operation parameters (e.g. url, ref, text, key). "
                            "Optional; defaults to an empty object."
                        ),
                    },
                },
                "required": ["op"],
            },
        }
    ]


def _post_command(
    bus_key: str, op: str, args: dict[str, Any], session_header: str, timeout_ms: int
) -> tuple[int | None, dict]:
    """POST one op to the gateway, surfacing the HTTP status.

    Returns ``(status, body)`` -- ``status`` is ``None`` for a transport/connection
    failure, which the handler treats the same as a missing native panel. The
    status must survive because HTTP 503 ``no_native_panel`` (run it elsewhere)
    reads differently from HTTP 200 ``{ok: false}`` (the panel ran it and it
    failed).

    ``bus_key`` (bare ``chat-N-<ts>``) goes in the BODY -- it is the key the native
    panel is registered under. ``session_header`` (the ``dashboard:``-namespaced
    session key) goes in the ``X-Session-Key`` HEADER, which the strict route's
    AF_UNIX peer check compares against the HMAC-signed session the caller
    resolves to; a bare header would be a 403.
    """
    body = json.dumps(
        {"session_key": bus_key, "op": op, "args": args, "timeout_ms": timeout_ms}
    ).encode()
    req = urllib.request.Request(
        f"{mcp_core._api_base()}{COMMAND_PATH}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": mcp_core._internal_secret(),
            "X-Session-Key": session_header,
        },
        method="POST",
    )
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- URL is the loopback gateway (_api_base()) + a fixed internal path; never agent-controlled  # noqa: E501
        with mcp_core._api_urlopen(req, timeout=COMMAND_TIMEOUT_SECS) as resp:
            return int(resp.status), json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return int(exc.code), json.loads(exc.read())
        except Exception:
            return int(exc.code), {}
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        # Connection refused / DNS / timeout -- treated as "no native panel here".
        return None, {}
    except Exception:
        return None, {}


def _result_text(op: str, result: Any) -> str:
    """Concise success text, with untrusted panel content redacted."""
    if op == "screenshot":
        # A screenshot result is base64 image data; slicing it to the length cap
        # below would return a corrupt, unusable payload, and inlining a full
        # image into chat wedges the session (see the web-verify skill). The
        # frame renders in the Browser panel the user is watching; for an image
        # FILE to inspect, that is the web-verify / playwright-cli path.
        return (
            "Browser screenshot: captured -- it renders in the Browser panel. "
            "For an image file to inspect, use the web-verify skill / playwright-cli."
        )
    if result in (None, "", {}, []):
        return f"Browser {op}: ok"
    rendered = result if isinstance(result, str) else json.dumps(result, default=str)
    rendered, _ = redact_exfiltration_urls(rendered)
    rendered, _ = redact_credentials(rendered)
    return f"Browser {op}: {rendered[:2000]}"


def _use_builtin_browser() -> bool:
    """User preference (``dashboard.use_builtin_browser``): drive the built-in
    native panel (True) or always fall back to playwright-cli (False).

    Read fresh each call so a Settings toggle takes effect without restarting
    the shim, and fail-OPEN to True: a config read/parse error must not silently
    disable the panel. Cheap disk stat+parse, on the same defensive footing as
    ``_browsing_available``.
    """
    try:
        return bool(getattr(KiroCrewConfig.load().dashboard, "use_builtin_browser", True))
    except Exception:
        return True


def browser(name: str, args: dict[str, Any]) -> str:
    # Re-checked here as well as in ``schemas()``: kiro-cli caches the tool list
    # for a session's life, so a session that started with the CLI installed
    # keeps offering the tool after it is removed. Fail-CLOSED.
    if not _browsing_available():
        return (
            "Error: browsing is not set up on this host. Install it from "
            "Settings -> Browser first."
        )
    # Governance gate (capabilities.browse). A policy that denies web browsing
    # must stop it on the native path too, so refuse OUTRIGHT here -- do NOT
    # fall back to playwright-cli (that would let browsing continue and defeat
    # the control). The playwright fallback is only for the capability-ALLOWED
    # no-native-panel case below.
    gov_denied = mcp_core._vet_browse_governance(mcp_core._resolve_session_key())
    if gov_denied:
        return f"Error: {gov_denied}"
    # User preference: built-in panel turned OFF in Settings -> Browser. Browsing
    # is allowed (unlike the governance deny above) -- the user just wants the
    # playwright-cli path, so return the fallback guidance and never touch the
    # native panel. (When ON, the no-native-panel case below still degrades to
    # playwright too -- e.g. a remote gateway with no Electron.)
    if not _use_builtin_browser():
        logger.debug(
            "browser-cmdbus/tool: built-in browser disabled by setting -> playwright-cli fallback"
        )
        return _DISABLED_TEXT
    op = args.get("op")
    if not isinstance(op, str) or op not in BROWSER_OPS:
        return f"Error: op must be one of: {', '.join(BROWSER_OPS)}"
    op_args = args.get("args") or {}
    if not isinstance(op_args, dict):
        return "Error: args must be a JSON object"
    if not _args_are_scalar(op_args):
        return "Error: args values must be strings, numbers, or booleans (or lists of those)"

    # SSRF gate: the built-in panel accepts any http(s) URL, so a `navigate` to a
    # loopback/private/link-local target could drive a local control plane (this
    # dashboard included) or the cloud-metadata endpoint. Only public http(s) is
    # auto-driven here; route non-public targets through playwright-cli, whose
    # path prompts for the required approval (matching the CLI gate).
    if op == "navigate":
        url = op_args.get("url")
        if not isinstance(url, str) or not _navigate_target_is_public(url):
            return (
                "Error: the browser tool only opens public http(s) URLs in the "
                "built-in panel. For a localhost/loopback/private/link-local "
                "address (e.g. a local dev server or an internal host), use "
                "playwright-cli, which prompts for the required approval: "
                "`playwright-cli open <url>`."
            )

    # Resolve the caller's session key the SAME way every other X-Session-Key
    # tool does (artifacts, sessions, skills, wait): the LENIENT resolver, whose
    # ladder ends in a libproc/proc ancestor walk. This is deliberate, not a
    # relaxation of the strict resolver used by session-MUTATING tools
    # (monitor_start, set_project). On a DEFAULT install the pooled gateway is
    # off, so there is no per-call caller context and no KIROCREW_SESSION_KEY,
    # and macOS/Windows have no HMAC pid sidecar -- i.e. ALL THREE sources the
    # strict resolver accepts are absent, so strict returns "" for the user's
    # own main session too, not just subagents. The tool used to then fabricate
    # a bogus ``unresolved:<pid>`` key; the strict command route rejects that
    # header (the gateway kernel-resolves the AF_UNIX peer and denies a declared
    # key that differs from it), 403-ing every op on a default install. The
    # lenient resolver returns the REAL slot key, which matches what the peer
    # check resolves, so the op is admitted. (Tradeoff: a subagent's MCP-core
    # child walks up into the parent slot, so a subagent op resolves to the
    # PARENT's panel. BROWSER_OPS includes mutating verbs, so this is not
    # view-only -- but it is same-user/same-machine (no trust-boundary crossing),
    # `navigate` is already restricted to PUBLIC http(s) above, and subagents
    # rarely browse. The only robust own-slot signal is the STRICT resolver,
    # which returns "" on a default (no pooled-gateway) install -- gating on it
    # here would re-break the main-session case this fix exists to enable, so the
    # residual is accepted rather than papered over with a partial check.)
    #
    # Two identities, deliberately different:
    #  * The X-Session-Key HEADER stays dashboard:-namespaced -- the peer check
    #    compares the resolved peer session against it.
    #  * The BUS key in the request BODY is the BARE slot id the native panel is
    #    registered under (chat-N-<ts>), so it must be namespace-stripped to
    #    rendezvous with the panel.
    session_key = mcp_core._resolve_session_key()
    if not session_key:
        # No addressable session => no native panel an op could be keyed to. Do
        # NOT invent a placeholder key: it is guaranteed to fail the strict
        # route's peer check with a hard 403, masking the clean fallback. Route
        # to playwright-cli instead, exactly like the no-panel (503) case below.
        logger.debug(
            "browser-cmdbus/tool: unresolved session -> playwright-cli fallback"
        )
        return _FALLBACK_TEXT
    header_err = mcp_core._session_key_header_error(session_key)
    if header_err:
        return f"Error: {header_err}"
    bus_key = (
        session_key[len("dashboard:") :]
        if session_key.startswith("dashboard:")
        else session_key
    )

    logger.debug("browser-cmdbus/tool: op=%s session=%s -> POST /api/browser/command", op, bus_key)
    timeout_ms = _SLOW_OP_TIMEOUT_MS if op in _SLOW_OPS else _DEFAULT_OP_TIMEOUT_MS
    status, payload = _post_command(bus_key, op, op_args, session_key, timeout_ms)
    if status == 200 and isinstance(payload, dict) and payload.get("ok") is True:
        return _result_text(op, payload.get("result"))
    if status == 200 and isinstance(payload, dict) and payload.get("ok") is False:
        detail = str(payload.get("error") or "the browser op failed")
        detail, _ = redact_exfiltration_urls(detail)
        detail, _ = redact_credentials(detail)
        return f"Error: {detail}"
    # No native panel served the op, so run it elsewhere. Three cases all mean
    # "no native browser path on this gateway": HTTP 503 (no panel registered),
    # HTTP 404 (a gateway build without this channel registered), and a transport
    # failure (``status is None``). All point at the playwright-cli fallback. Any
    # OTHER non-200 (429 queue-full, 504 timeout, 4xx) is a real failure of THIS
    # path -- surface it rather than misdirecting the agent to playwright-cli.
    if status in (404, 503) or status is None:
        logger.debug(
            "browser-cmdbus/tool: no native panel (status=%s) -> playwright-cli fallback", status
        )
        return _FALLBACK_TEXT
    code = payload.get("code") if isinstance(payload, dict) else None
    err = payload.get("error") if isinstance(payload, dict) else None
    return f"Error: browser command failed (HTTP {status}{f', {code}' if code else ''}): {err or 'no detail'}"


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "browser": browser,
}
