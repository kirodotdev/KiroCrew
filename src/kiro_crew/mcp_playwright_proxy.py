"""Playwright MCP Proxy — compresses accessibility tree responses.

Sits between the agent backend and the real Playwright MCP server,
intercepting responses that contain large accessibility trees and
compressing them to compact outlines with element refs (~95% token
reduction).

Runs as ``kirocrew mcp-playwright-proxy [playwright-args...]``.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from typing import Any

# Stdlib-only leaf (it imports urllib.request and nothing else), so this
# top-level import does not pull the gateway into this stdio proxy -- the same
# constraint that makes _internal_secret() reach for the config.paths leaf.
from kiro_crew.loopback_http import loopback_urlopen

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

_KEEP_PATTERN = re.compile(
    r"(heading|link|button|textbox|combobox|checkbox|radio|tab|menu"
    r"|img|image|navigation|main|banner|contentinfo|search|alert"
    r"|dialog|listitem|row|cell|ref=)",
    re.IGNORECASE,
)

_TREE_INDICATOR = re.compile(r"^\s*-\s+(link|button|heading|navigation|main|textbox|img)\b")

_MAX_OUTLINE_LINES = 150


def _is_accessibility_tree(text: str) -> bool:
    """Heuristic: does this text look like a Playwright accessibility snapshot?"""
    lines = text.split("\n", 20)
    tree_lines = sum(1 for line in lines if _TREE_INDICATOR.match(line))
    return tree_lines >= 3


def _compress_to_outline(text: str) -> str:
    """Compress accessibility tree to compact outline with refs."""
    lines = text.split("\n")
    outline: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped == "-":
            continue
        if _KEEP_PATTERN.search(stripped):
            indent = len(line) - len(line.lstrip())
            compact_indent = "  " * min(indent // 2, 4)
            outline.append(f"{compact_indent}{stripped}")
            if len(outline) >= _MAX_OUTLINE_LINES:
                outline.append(f"... (truncated at {_MAX_OUTLINE_LINES} lines)")
                break

    if not outline:
        return text

    total = len([ln for ln in lines if ln.strip()])
    header = f"[Compressed: {total} elements → {len(outline)} interactive]\n"
    return header + "\n".join(outline)


# Use tempfile.gettempdir() rather than a hardcoded ``/tmp`` fallback so the
# screenshot dir resolves to the platform-native temp location — POSIX honours
# ``$TMPDIR``/``$TEMP``/``$TMP`` and falls back to ``/tmp``; on Windows the
# fallback is ``%TEMP%`` / ``%USERPROFILE%\\AppData\\Local\\Temp`` (``/tmp``
# does not exist and would fail on ``os.makedirs``).
_SCREENSHOT_DIR = os.path.join(tempfile.gettempdir(), "kirocrew-screenshots")


def _env_int(name: str, default: int) -> int:
    """Parse a non-negative int env override, falling back to ``default``."""
    try:
        val = int(os.environ.get(name, "") or default)
        return val if val >= 0 else default
    except ValueError:
        return default


# Max width (px) for relayed/saved frames — 1920 so a resized mirror panel
# shows real pixels instead of an upscaled blur; set KIROCREW_BROWSE_MAX_WIDTH=0
# to disable downscaling entirely (send native resolution). JPEG quality is
# likewise tunable. Both apply to the on-disk screenshot and the live mirror
# frame, which share one encode.
_MAX_FRAME_WIDTH = _env_int("KIROCREW_BROWSE_MAX_WIDTH", 1920)
_FRAME_JPEG_QUALITY = _env_int("KIROCREW_BROWSE_JPEG_QUALITY", 70)

# The browse session this proxy serves. kiro-cli freezes KIROCREW_SESSION_KEY in
# the MCP subprocess env at spawn, so it identifies the session whose browse is
# being mirrored. Sent with each frame so the dashboard panel can label which
# session it's showing; empty when unknown (e.g. warm-pool processes).
_SESSION_KEY = os.environ.get("KIROCREW_SESSION_KEY", "")


def _encode_frame(data: str, media_type: str) -> tuple[bytes, str]:
    """Decode a base64 image; downscale + JPEG-encode if PIL is available.

    Returns ``(bytes, ext)``. Shared by the on-disk save and the live-frame POST
    so the (relatively expensive) decode/resize/encode runs once per screenshot.
    """
    img_bytes = base64.b64decode(data)
    ext = "jpeg" if ("jpeg" in media_type or "jpg" in media_type) else "png"
    if _HAS_PIL:
        try:
            img: Image.Image = Image.open(io.BytesIO(img_bytes))
            if _MAX_FRAME_WIDTH and img.width > _MAX_FRAME_WIDTH:
                ratio = _MAX_FRAME_WIDTH / img.width
                resample = getattr(Image, "LANCZOS", getattr(Image, "ANTIALIAS", None))
                img = img.resize((_MAX_FRAME_WIDTH, int(img.height * ratio)), resample)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=_FRAME_JPEG_QUALITY)
            return buf.getvalue(), "jpeg"
        except Exception:
            pass
    return img_bytes, ext


_SCREENSHOT_KEEP = 200


def _prune_screenshot_dir() -> None:
    """Keep at most ``_SCREENSHOT_KEEP`` newest screenshots; best-effort.

    The dir grows one file per agent screenshot, so ring-trim the oldest on
    each save to bound disk use.
    """
    try:
        entries = [
            os.path.join(_SCREENSHOT_DIR, f) for f in os.listdir(_SCREENSHOT_DIR)
        ]
        if len(entries) <= _SCREENSHOT_KEEP:
            return
        entries.sort(key=lambda p: os.path.getmtime(p))
        for stale in entries[: len(entries) - _SCREENSHOT_KEEP]:
            try:
                os.remove(stale)
            except OSError:
                pass
    except OSError:
        pass


def _write_screenshot(img_bytes: bytes, ext: str) -> str:
    """Write pre-encoded image bytes to the screenshot dir; prune; return path."""
    os.makedirs(_SCREENSHOT_DIR, mode=0o700, exist_ok=True)
    ts = int(time.time() * 1000)
    filepath = os.path.join(_SCREENSHOT_DIR, f"screenshot-{ts}.{ext}")
    with open(filepath, "wb") as f:
        f.write(img_bytes)
    _prune_screenshot_dir()
    return filepath


def _gateway_frame_url() -> str:
    """Loopback gateway endpoint that rebroadcasts a browse frame to the dashboard."""
    port = os.environ.get("KIROCREW_PORT", "5476")
    return f"http://127.0.0.1:{port}/api/browser/frame"


def _internal_secret() -> str:
    """Read the per-session IPC secret the gateway requires on internal paths.

    Same source as ``mcp_core._internal_secret`` (``<config_dir>/.local_secret``,
    which honors ``$KIROCREW_HOME`` and defaults to ``~/.kiro/crew``). The path is
    resolved via the stdlib-only ``config.paths`` leaf to avoid importing the
    gateway into this stdio proxy. Returns "" if unreadable — the POST then fails
    the gate and is silently dropped (frames are best-effort).
    """
    from kiro_crew.config.paths import config_dir

    home = str(config_dir())
    try:
        with open(os.path.join(home, ".local_secret"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# Extension mode attaches to the user's own (visible) Chrome rather than launching
# a headless browser, so the live mirror is redundant — they already see the real
# window. setup.py passes ``--extension`` to this proxy in that mode, so we read it
# from our own argv and suppress the frame POST (the screenshot is still saved to
# disk for the agent's Read tool; only the dashboard mirror is skipped).
_EXTENSION_MODE = "--extension" in sys.argv

# `browser_*` tool name -> the closed wire-op verb the native control plane accepts.
#
# The embedded Electron ``WebContentsView`` now serves the agent's core browsing
# workflow over the ``webContents.debugger`` + command-bus channel. The proxy does
# NOT rewrite ``tools/list``, so the tool NAMES are Playwright's, but their
# SEMANTICS are ours: ``snapshot`` mints our own ``eN`` refs and
# ``click``/``type``/``hover``/``select_option`` resolve those same refs, so the
# pair is internally consistent without any Playwright ref compatibility.
#
# The vocabulary here is the authoritative shared contract with the Electron
# handler (``navigate, snapshot, click, type, press_key, hover, select_option,
# screenshot, evaluate, wait_for, back, console``). See ``_translate_native_args``
# for the per-op argument shape sent over the wire.
_NATIVE_OPS: dict[str, str] = {
    "browser_navigate": "navigate",
    "browser_snapshot": "snapshot",
    "browser_click": "click",
    "browser_type": "type",
    "browser_press_key": "press_key",
    "browser_hover": "hover",
    "browser_select_option": "select_option",
    "browser_take_screenshot": "screenshot",
    "browser_evaluate": "evaluate",
    "browser_wait_for": "wait_for",
    "browser_navigate_back": "back",
    "browser_console_messages": "console",
}

# Native mode: this build routes the agent's ``browser_*`` ops to the embedded
# Electron view (``_NATIVE_OPS`` non-empty). When active, the screenshot-mirror
# pump is disabled exactly like extension mode -- the native ``WebContentsView`` is
# already visible in the dashboard panel, so injecting/mirroring a Playwright
# screenshot would paint a stale surface over the live page. ``_maybe_compress_response``
# stays active for the warm-pool fallback path and for ``tools/list``.
# Set once the native command endpoint answers in a way that PROVES a live panel
# is driving a page: ok:true, or an ok:false that is a genuine authorization deny.
# An ok:false that merely names an ABSENT view does NOT count -- a mounted-but-
# empty Browser panel registers a poller and answers `no-browser-view`, and
# latching on that would both suppress the legitimate Playwright fallback and
# strip every unmapped browser_* tool for the rest of the process. Transport
# failures clear it, so closing the panel restores full Playwright behaviour.
_native_panel_seen = False

# Error text from the Electron side meaning "no view/panel to drive" rather than
# "not allowed to drive it". An absent panel is a transport gap; a deny is not.
_NATIVE_ABSENT_MARKERS = ("no-browser-view", "no native browser panel", "no-native-panel")


def _names_absent_panel(detail: str) -> bool:
    """True when a refusal is really an absent panel, not an authorization deny."""
    low = (detail or "").lower()
    return any(marker in low for marker in _NATIVE_ABSENT_MARKERS)


# Shared lock around writes to the Playwright subprocess stdin. Both the client→
# subprocess forwarder and the active-pump thread (below) write JSON-RPC there;
# an unlocked interleave could split a message on the pipe.
_proc_stdin_lock = threading.Lock()

# Live dashboard subscriber count, learned from the gateway's frame-POST response
# ({"ok": true, "subscribers": N}). The active pump uses it to stop screenshotting
# when nobody is watching. Optimistic (1) until the first response arrives.
_last_subscriber_count = 1


def _record_subscriber_count(body: bytes) -> None:
    """Update the cached subscriber count from a frame-POST response body."""
    global _last_subscriber_count
    try:
        parsed = json.loads(body.decode("utf-8"))
        if isinstance(parsed, dict) and isinstance(parsed.get("subscribers"), int):
            _last_subscriber_count = parsed["subscribers"]
    except (ValueError, UnicodeDecodeError):
        pass


def _post_frame_to_gateway(img_bytes: bytes, fmt: str, source: str = "agent") -> None:
    """Best-effort POST of a browse frame to the gateway for the live dashboard mirror.

    Runs on a daemon thread so it never blocks the JSON-RPC relay (a synchronous
    POST in the relay loop would add latency to the agent's own screenshot call).
    Swallows every error: frames are non-critical, and the gateway may be down,
    on a different port, or unreachable — the agent's screenshot must not depend
    on the mirror succeeding. The ``/api/browser/frame`` ingress is a loopback +
    internal-secret path, so we send the same ``X-Internal-Secret`` header the
    other MCP-side callers use, and read back the live subscriber count.

    No-op in extension mode: the user is watching their own Chrome, so mirroring a
    sparse, downscaled copy to the dashboard adds load with no benefit. Also a
    no-op in native mode: the embedded ``WebContentsView`` is already visible, so a
    mirrored frame would paint a stale surface over the live page.
    """
    # Suppress the mirror only once native routing is PROVEN live for this
    # session. A static module constant would be wrong here (the op map is
    # never empty),
    # so gating on it silenced the mirror on every host -- including a remote
    # gateway, where the proxy is the only frame producer and the panel would
    # therefore render nothing at all.
    if _EXTENSION_MODE or _native_panel_seen:
        return

    def _send() -> None:
        try:
            b64 = base64.b64encode(img_bytes).decode("ascii")
            body = json.dumps(
                {
                    "data": b64,
                    "format": fmt,
                    "source": source,
                    # Frozen-env session key: correct for per-session spawns, but
                    # empty for warm-pool workers (pre-spawned before a slot is
                    # assigned, so KIROCREW_SESSION_KEY was never set). Sent as a
                    # fallback only.
                    "session_key": _SESSION_KEY,
                    # This proxy's pid, so the gateway can resolve the AUTHORITATIVE
                    # session key by walking our process ancestry to the kiro-cli
                    # worker and verifying its gateway-signed session_pid sidecar
                    # (the same per-turn mapping every managed MCP tool resolves).
                    # This is what makes the live mirror work under the warm pool,
                    # where the frozen env key above is empty.
                    "host_pid": os.getpid(),
                }
            ).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            secret = _internal_secret()
            if secret:
                headers["X-Internal-Secret"] = secret
            req = urllib.request.Request(
                _gateway_frame_url(),
                data=body,
                headers=headers,
                method="POST",
            )
            resp = loopback_urlopen(req, timeout=2)
            try:
                _record_subscriber_count(resp.read())
            finally:
                resp.close()
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def _gateway_pump_audit_url() -> str:
    """Loopback gateway endpoint that records a pump-injected tool invocation."""
    port = os.environ.get("KIROCREW_PORT", "5476")
    return f"http://127.0.0.1:{port}/api/browser/pump-audit"


def _post_pump_audit() -> bool:
    """Synchronously record a pump screenshot injection with the gateway.

    This proxy is stdlib-only and cannot reach ``sel.py``, so the gateway emits
    the SEL audit event for the injected ``browser_take_screenshot`` tool call on
    our behalf. Returns ``True`` only when the gateway acknowledged the audit
    (HTTP 2xx); the caller MUST gate the injection on this result so an
    unacknowledged audit skips the injection rather than executing an unaudited
    tool call. Returns ``False`` in extension mode (the pump is disabled there;
    the user already sees their own Chrome) and in native mode (the pump is
    disabled; the embedded view is already visible).
    """
    if _EXTENSION_MODE or _native_panel_seen:
        return False
    try:
        headers = {"Content-Type": "application/json"}
        secret = _internal_secret()
        if secret:
            headers["X-Internal-Secret"] = secret
        req = urllib.request.Request(
            _gateway_pump_audit_url(),
            data=b"{}",
            headers=headers,
            method="POST",
        )
        resp = loopback_urlopen(req, timeout=2)
        try:
            status = resp.getcode()
        finally:
            resp.close()
        return status is not None and 200 <= status < 300
    except Exception as exc:
        # Audit delivery failed, so the caller skips this injection rather than
        # run an unaudited browser_take_screenshot. Log the failure type to stderr
        # (stdlib-only subprocess — captured in the proxy log) so audit gaps are
        # discoverable; the next pump cycle retries naturally.
        sys.stderr.write(
            f"kirocrew: pump-audit POST failed ({type(exc).__name__}); skipping pump injection\n"
        )
        return False


def _save_screenshot(data: str, media_type: str) -> str:
    """Encode, persist, and mirror a screenshot. Returns the on-disk path.

    Encodes once, writes the file (for the agent's Read tool), and fires a
    best-effort live-frame POST to the gateway (for the dashboard mirror).
    """
    img_bytes, ext = _encode_frame(data, media_type)
    filepath = _write_screenshot(img_bytes, ext)
    _post_frame_to_gateway(img_bytes, ext)
    return filepath


# ── Active pump (B′): keep the mirror current between agent screenshots ──
#
# In B-minus the dashboard only updates when the agent itself calls
# browser_take_screenshot. The active pump fills the gaps: a background thread
# injects its OWN browser_take_screenshot tools/call into the Playwright server
# during idle windows, demuxes the proxy-namespaced response (never forwarded to
# kiro-cli), and relays the frame. It cannot match a CDP push stream (that needs
# the debug port we deliberately do not open); idle-gating bounds it to ~1-3 fps.
#
# All gates must hold to inject (see _should_pump):
#   * pump enabled (not extension mode — the user already sees their own Chrome);
#   * no agent request in flight (_PENDING_REQUESTS empty) — zero contention;
#   * no pump frame already in flight (single-in-flight), with a timeout so a
#     hung browser cannot wedge the pump forever;
#   * recent real browse activity (a browser_* tool ran lately) — do not pump
#     when no page is open / the session is idle-cold;
#   * a dashboard is actually watching (subscribers > 0).
_PUMP_INTERVAL = float(os.environ.get("KIROCREW_BROWSE_PUMP_INTERVAL", "") or 1.5)
_PUMP_ACTIVE_WINDOW = 20.0  # seconds since the last real browser_* tool response
_PUMP_TIMEOUT = 10.0  # seconds before a stuck in-flight pump is abandoned
_PUMP_ID_PREFIX = "__mc_pump_"
_BROWSE_TOOL_PREFIX = "browser_"

_pump_enabled = "--extension" not in sys.argv
_pump_seq = 0
_pump_inflight_id: str | None = None
_pump_sent_at = 0.0
_last_browse_activity = 0.0


def _note_browse_activity(original: dict[str, Any] | None) -> None:
    """Mark browse activity when a completed request was a ``browser_*`` tool call."""
    global _last_browse_activity
    if not isinstance(original, dict) or original.get("method") != "tools/call":
        return
    name = (original.get("params") or {}).get("name", "")
    if isinstance(name, str) and name.startswith(_BROWSE_TOOL_PREFIX):
        _last_browse_activity = time.time()


def _is_pump_id(req_id: Any) -> bool:
    """True if a response id belongs to a proxy-injected active-pump screenshot."""
    return isinstance(req_id, str) and req_id.startswith(_PUMP_ID_PREFIX)


def _clear_pump_inflight(req_id: Any) -> None:
    """Release the single-in-flight pump slot when its response arrives."""
    global _pump_inflight_id
    if req_id == _pump_inflight_id:
        _pump_inflight_id = None


def _should_pump(now: float) -> bool:
    """Whether to inject an active-pump screenshot now (pure; all gates)."""
    if not _pump_enabled:
        return False
    # Once native routing is proven live the embedded view is directly visible,
    # so a mirror frame would paint a stale surface over the real page -- and the
    # injected screenshot RPC is pointless work besides. Checked at call time, not
    # as a static constant, so hosts that still fall back to Playwright (remote
    # gateway, or before the user opens a page) keep a working mirror.
    if _native_panel_seen:
        return False
    if _PENDING_REQUESTS:
        return False
    if _pump_inflight_id is not None and (now - _pump_sent_at) < _PUMP_TIMEOUT:
        return False
    if (now - _last_browse_activity) > _PUMP_ACTIVE_WINDOW:
        return False
    if _last_subscriber_count <= 0:
        return False
    return True


def _relay_pump_frame(msg: dict[str, Any]) -> None:
    """Extract the image from a pump screenshot response and relay it (ephemeral).

    Unlike agent screenshots, pump frames are never written to disk — they exist
    only to refresh the live dashboard mirror. Best-effort: a malformed pump
    response (e.g. bad base64 from a corrupted screenshot) must never crash the
    main relay loop, so all errors are swallowed — the frame is just skipped.
    """
    try:
        result = msg.get("result")
        if not isinstance(result, dict):
            return
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "image" and item.get("data"):
                img_bytes, ext = _encode_frame(item["data"], item.get("mimeType", "image/png"))
                _post_frame_to_gateway(img_bytes, ext, source="pump")
                return
    except Exception:
        pass


def _pump_loop(proc_stdin) -> None:
    """Background thread: inject idle-gated ``browser_take_screenshot`` calls."""
    global _pump_seq, _pump_inflight_id, _pump_sent_at
    while True:
        time.sleep(_PUMP_INTERVAL)
        now = time.time()
        # Abandon a stuck in-flight pump so a hung browser can't wedge us.
        if _pump_inflight_id is not None and (now - _pump_sent_at) >= _PUMP_TIMEOUT:
            _pump_inflight_id = None
        if not _should_pump(now):
            continue
        # Audit BEFORE injecting: the gateway emits the SEL tool-invocation event
        # on our behalf (the proxy can't reach sel.py). If the audit can't be
        # delivered we skip this cycle rather than run an unaudited
        # browser_take_screenshot; the next tick (~_PUMP_INTERVAL later) retries.
        # The pump only fires while a dashboard is subscribed, which needs the
        # same loopback gateway up — so a failed audit reliably coincides with
        # "nothing is watching anyway."
        if not _post_pump_audit():
            continue
        _pump_seq += 1
        pid = f"{_PUMP_ID_PREFIX}{_pump_seq}"
        _pump_inflight_id = pid
        _pump_sent_at = time.time()
        req = {
            "jsonrpc": "2.0",
            "id": pid,
            "method": "tools/call",
            "params": {"name": "browser_take_screenshot", "arguments": {"type": "jpeg"}},
        }
        try:
            with _proc_stdin_lock:
                _write_message_to_subprocess(proc_stdin, req)
        except Exception:
            _pump_inflight_id = None


def _maybe_compress_response(msg: dict[str, Any]) -> dict[str, Any]:
    """Compress accessibility trees and save screenshots to files."""
    result = msg.get("result")
    if not isinstance(result, dict):
        return msg
    content = result.get("content")
    if not isinstance(content, list):
        return msg
    new_content = []
    for item in content:
        if not isinstance(item, dict):
            new_content.append(item)
            continue
        if item.get("type") == "image":
            data = item.get("data", "")
            media_type = item.get("mimeType", "image/png")
            if data:
                filepath = _save_screenshot(data, media_type)
                new_content.append({
                    "type": "text",
                    "text": f"Screenshot saved: {filepath}\nUse Read tool to view it if needed.",
                })
            else:
                new_content.append(item)
            continue
        if item.get("type") == "text":
            text = item.get("text", "")
            if len(text) > 5000 and _is_accessibility_tree(text):
                item["text"] = _compress_to_outline(text)
        new_content.append(item)
    result["content"] = new_content
    return msg


def _read_message(stream) -> dict[str, Any] | None:
    """Read one JSON-RPC message from a binary stream."""
    while True:
        line = stream.readline()
        if not line:
            return None
        line_str = line.decode("utf-8").strip()
        if not line_str:
            continue
        if line_str.lower().startswith("content-length:"):
            try:
                length = int(line_str.split(":", 1)[1].strip())
                while True:
                    sep = stream.readline()
                    if sep.strip() == b"":
                        break
                body = stream.read(length)
                parsed = json.loads(body.decode("utf-8"))
                if isinstance(parsed, dict):
                    return parsed
                continue
            except (ValueError, json.JSONDecodeError):
                continue
        try:
            parsed = json.loads(line_str)
            if isinstance(parsed, dict):
                return parsed
            continue
        except json.JSONDecodeError:
            continue


_client_uses_content_length: bool | None = None


def _read_message_from_client(stream) -> dict[str, Any] | None:
    """Read from client (kiro-cli/probe), detecting framing style."""
    global _client_uses_content_length
    while True:
        line = stream.readline()
        if not line:
            return None
        line_str = line.decode("utf-8").strip()
        if not line_str:
            continue
        if line_str.lower().startswith("content-length:"):
            _client_uses_content_length = True
            try:
                length = int(line_str.split(":", 1)[1].strip())
                while True:
                    sep = stream.readline()
                    if sep.strip() == b"":
                        break
                body = stream.read(length)
                return json.loads(body.decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                continue
        try:
            if _client_uses_content_length is None:
                _client_uses_content_length = False
            return json.loads(line_str)
        except json.JSONDecodeError:
            continue


def _write_message(stream, msg: dict[str, Any]) -> None:
    """Write a JSON-RPC message, mirroring the client's framing style."""
    body = json.dumps(msg).encode("utf-8")
    if _client_uses_content_length:
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        stream.write(header + body)
    else:
        stream.write(body + b"\n")
    stream.flush()


def _write_message_to_subprocess(stream, msg: dict[str, Any]) -> None:
    """Write to the Playwright MCP subprocess — bare JSON lines (Node expects this)."""
    body = json.dumps(msg).encode("utf-8")
    stream.write(body + b"\n")
    stream.flush()


_PENDING_REQUESTS: dict[Any, dict[str, Any]] = {}


# Slightly longer than the gateway's own default command timeout (15s) so the
# gateway's 504 is what surfaces, rather than this side giving up first.
_NATIVE_CALL_TIMEOUT_S = 20.0


def _gateway_command_url() -> str:
    """Loopback gateway endpoint that runs one op on a NATIVE browser panel."""
    port = os.environ.get("KIROCREW_PORT", "5476")
    return f"http://127.0.0.1:{port}/api/browser/command"


# `browser_*` tool name -> wire-op mapping lives at module top (``_NATIVE_OPS``,
# ~line 212) so the mirror-suppression state (``_native_panel_seen``) can be read
# pump section is defined. The interception logic is here.

# Element-addressing ref shape minted by our native ``snapshot`` op (``e5`` ...).
# ``@playwright/mcp`` sends the element as ``target`` (a snapshot ref OR a CSS/text
# selector) plus an optional human-readable ``element`` description. We only accept
# our own refs; a selector cannot be resolved against ``window.__kcRefs`` and must
# error rather than silently mis-target.
_NATIVE_REF_RE = re.compile(r"^e\d+$")

# Ops whose semantics REQUIRE an element ref (Playwright marks ``target`` required).
_NATIVE_REF_REQUIRED_OPS = frozenset({"click", "type", "hover", "select_option"})
# Ops that MAY carry an element ref (element-scoped evaluate); ``target`` optional.
_NATIVE_REF_OPTIONAL_OPS = frozenset({"evaluate"})


def _native_error(req_id: Any, message: str) -> dict[str, Any]:
    """Build a JSON-RPC error response (surfaced to the agent as an MCP error)."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32000, "message": message},
    }


def _native_text_result(req_id: Any, text: str) -> dict[str, Any]:
    """Build a JSON-RPC tools/call result carrying a single text block."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }


def _translate_native_args(op: str, arguments: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Map a Playwright tool's arguments onto the native wire-op's arg shape.

    Returns ``(args, None)`` on success or ``(None, error_message)`` to REFUSE the
    call with an MCP error (never a Playwright fall-back). The element-addressing
    param is Playwright's ``target`` (ref OR selector); we translate a valid ``eN``
    ref to the wire field ``ref`` and reject a selector, because the native side
    resolves elements only via ``window.__kcRefs.get(ref)``. The human-readable
    ``element`` description is dropped -- it exists only for Playwright's own
    permission prompt.

    Field RENAMES matter as much as the ref translation: Playwright and the native
    handler spell some payloads differently, and a mismatch fails SILENTLY (an
    unread field just reads as absent), which no per-side unit test can catch.
    ``browser_evaluate`` sends ``function`` where the native handler reads
    ``expression`` -- without this rename it would evaluate the empty string and
    report success. ``browser_take_screenshot`` sends ``type`` (png|jpeg) where the
    handler reads ``format``.
    """
    args = dict(arguments or {})
    args.pop("element", None)
    target = args.pop("target", None)

    if op == "evaluate" and "function" in args:
        args["expression"] = args.pop("function")
    elif op == "screenshot":
        if "type" in args:
            args["format"] = args.pop("type")
        # The native view composites at its own size; Playwright's page-scaling
        # and full-page stitching have no native analogue, so drop them rather
        # than imply they were honoured.
        args.pop("scale", None)
        args.pop("fullPage", None)

    if op in _NATIVE_REF_REQUIRED_OPS:
        if not isinstance(target, str) or not target:
            return None, (
                f"native browser: '{op}' needs a 'target' element reference; "
                "call browser_snapshot first to get 'eN' refs"
            )
        if not _NATIVE_REF_RE.match(target):
            return None, (
                f"native browser: '{op}' 'target' must be a snapshot ref like 'e5', got "
                f"{target!r}. The native browser resolves elements by refs minted by "
                "browser_snapshot, not selectors -- call browser_snapshot and use its refs."
            )
        args["ref"] = target
        return args, None

    if op in _NATIVE_REF_OPTIONAL_OPS:
        if target is not None:
            if not isinstance(target, str) or not _NATIVE_REF_RE.match(target):
                return None, (
                    f"native browser: '{op}' element 'target' must be a snapshot ref like "
                    f"'e5', got {target!r}. Call browser_snapshot and use its refs."
                )
            args["ref"] = target
        return args, None

    # Any remaining op (navigate/snapshot/screenshot/press_key/wait_for/back/console)
    # does not address a single element. If a caller nonetheless passed ``target``,
    # honouring it would be a silent mis-target, so refuse explicitly.
    if target is not None:
        return None, (
            f"native browser: '{op}' does not support element targeting; omit 'target'"
        )
    return args, None


def _extract_screenshot_payload(result: Any) -> tuple[str, str] | None:
    """Pull ``(base64_data, media_type)`` out of a native ``screenshot`` result.

    The Electron handler returns the frame as ``{"data": "<base64>", "mimeType":
    "image/png"|"image/jpeg"}``. Returns ``None`` if the shape does not match, so
    the caller falls back to rendering the result as text.
    """
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, str) and data:
            media_type = result.get("mimeType") or result.get("media_type") or "image/png"
            return data, str(media_type)
    return None


def _try_native_tool_call(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Route a ``browser_*`` tools/call to the native embedded panel.

    Returns a JSON-RPC response to send back to the client, or ``None`` to mean
    "not handled -- forward to the Playwright subprocess as usual".

    No-split-brain rule: once this session routes ANY op natively (session key
    known -> mapped ops go to the embedded view), a page-touching ``browser_*``
    tool that is NOT mapped must never reach Playwright -- it would act on a
    DIFFERENT page than the native view the agent is driving and report success.
    Such tools are refused with an explicit MCP error naming the tool. Only when
    native routing is inactive for this session (no session key -> warm-pool
    worker that cannot identify a panel) does everything fall through to
    Playwright, which then coherently owns the whole workflow.

    Fall-back is confined to TRANSPORT unavailability (no panel / 503 / timeout /
    connection error): a panel that ANSWERS and refuses (``ok:false``) is surfaced
    as an MCP error, never re-routed to Playwright, so a revoked "let the agent
    act" cannot be bypassed by another route.
    """
    if msg.get("method") != "tools/call":
        return None
    global _native_panel_seen
    params = msg.get("params") or {}
    name = params.get("name") or ""
    if not name.startswith(_BROWSE_TOOL_PREFIX):
        # Not a browser tool (e.g. tools/list plumbing) -- never our concern.
        return None

    # The frozen-env key is correct for per-session spawns but EMPTY for a
    # warm-pool worker (pre-spawned before a slot is assigned, so
    # KIROCREW_SESSION_KEY was never set). We do NOT bail on an empty key here:
    # the command POST also carries this proxy's ``host_pid``, from which the
    # GATEWAY resolves the AUTHORITATIVE session key by walking our process
    # ancestry to the kiro-cli worker and verifying its signed session_pid
    # sidecar -- the same mechanism the frame path already uses to make the live
    # mirror work under the warm pool. When no native panel is registered for the
    # resolved session the gateway answers 503 and we fall back to Playwright
    # below, so attempting the POST costs nothing on a remote/non-Electron host.
    session_key = _SESSION_KEY

    op = _NATIVE_OPS.get(name)
    if op is None:
        # A page-touching browser_* tool with no native mapping.
        #
        # Split brain is only REACHABLE once a native panel is actually driving a
        # page: that is what makes "read via Playwright" answer about a different
        # page than the agent is looking at. Until we have seen a panel answer,
        # there is no native page to be inconsistent with, so Playwright coherently
        # owns the whole workflow and must keep working -- that is the case on a
        # remote gateway or any non-Electron host, which has a session key but no
        # panel and would otherwise lose every unmapped browser_* tool.
        #
        # So refuse only once presence is PROVEN (see _native_panel_seen below).
        if not _native_panel_seen:
            return None
        return _native_error(
            msg.get("id"),
            f"{name} is not supported on the native browser. Supported operations: "
            + ", ".join(sorted(_NATIVE_OPS)),
        )

    args, arg_error = _translate_native_args(op, params.get("arguments") or {})
    if arg_error is not None:
        # A client-input problem (selector where a ref is required, stray target).
        # This is a definite refusal, not a transport gap -- surface it, do not
        # fall back and silently mis-target on Playwright.
        return _native_error(msg.get("id"), arg_error)

    # ``host_pid`` lets the gateway resolve the authoritative session key when
    # ``session_key`` is empty (warm pool); ``session_key`` stays as the fallback
    # for per-session spawns. Same pair the frame ingress accepts.
    body = json.dumps(
        {"session_key": session_key, "host_pid": os.getpid(), "op": op, "args": args}
    ).encode()
    req = urllib.request.Request(
        _gateway_command_url(),
        data=body,
        headers={"Content-Type": "application/json", "X-Internal-Secret": _internal_secret()},
        method="POST",
    )
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- URL is the loopback gateway (http://127.0.0.1 + the fixed /api/browser/command path from _gateway_command_url); only the port varies, from KIROCREW_PORT local config, never user/agent/request input, so no file:// or arbitrary-read is reachable  # noqa: E501
        with loopback_urlopen(req, timeout=_NATIVE_CALL_TIMEOUT_S) as resp:
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        # The gateway ANSWERED with a status. Only "there is no panel to drive"
        # may fall back; anything else must surface.
        #
        # A blanket `except Exception` here would be an authorization hole: a 403
        # (internal-secret mismatch) or 429 comes from a REACHABLE gateway, and
        # treating it as absent transport would re-run the op on Playwright --
        # converting a refusal into an allow by another route, which is exactly
        # what invariant 2 forbids.
        if exc.code in (503, 504):
            _native_panel_seen = False
            return None
        return _native_error(
            msg.get("id"),
            f"native browser command failed with HTTP {exc.code}: {name}",
        )
    except (TimeoutError, urllib.error.URLError, OSError):
        # TRANSPORT unavailable -- connection refused, DNS/socket error, timeout.
        # Nothing native can answer, so Playwright is the correct destination.
        # This and 503/504 are the ONLY cases that may fall back.
        _native_panel_seen = False
        return None
    except (ValueError, json.JSONDecodeError):
        # The panel answered with something undecodable. It IS reachable, so
        # falling back would again risk running a refused op elsewhere.
        return _native_error(
            msg.get("id"), f"native browser returned an undecodable response: {name}"
        )
    if not isinstance(payload, dict):
        return _native_error(
            msg.get("id"), f"native browser returned a malformed response: {name}"
        )

    if not payload.get("ok"):
        detail = str(payload.get("error") or "native browser refused the operation")
        # An answered refusal that names an ABSENT panel/view is a transport gap
        # wearing a refusal's clothes, not an authorization decision: a mounted
        # but empty Browser panel registers a poller while no view is open, so it
        # answers `no-browser-view`. Latching presence on that would (a) suppress
        # the legitimate Playwright fallback and (b) leave every unmapped
        # browser_* tool refused for the life of the proxy once the user closes
        # the panel. Treat it as unavailable instead: do not latch, and fall back.
        if _names_absent_panel(detail):
            _native_panel_seen = False
            return None
        # A genuine refusal (e.g. the user revoked "let the agent act") proves a
        # panel exists, so latch presence and surface the refusal. Falling back
        # here would convert a deny into an allow by another route.
        _native_panel_seen = True
        return _native_error(msg.get("id"), detail)

    # An `ok:true` answer likewise proves the panel is live and driving the page.
    _native_panel_seen = True

    result = payload.get("result")

    # Screenshots come back as base64. Route them through the same save-to-file
    # path the subprocess->client direction uses (_save_screenshot), so the agent
    # receives a PATH to Read, not inline image bytes flooding its context. The
    # dashboard mirror POST inside _save_screenshot is a no-op in native mode.
    if op == "screenshot":
        shot = _extract_screenshot_payload(result)
        if shot is not None:
            filepath = _save_screenshot(shot[0], shot[1])
            return _native_text_result(
                msg.get("id"),
                f"Screenshot saved: {filepath}\nUse Read tool to view it if needed.",
            )

    text = result if isinstance(result, str) else json.dumps(result, default=str)
    return _native_text_result(msg.get("id"), text)


def _forward_stdin_to_subprocess_tracked(client_stdin, proc_stdin) -> None:
    """Forward client→subprocess, tracking in-flight IDs to synthesize errors if subprocess dies."""
    while True:
        msg = _read_message_from_client(client_stdin)
        if msg is None:
            proc_stdin.close()
            break
        req_id = msg.get("id")
        # A browser_* call goes to the NATIVE embedded panel when one exists for
        # this session; otherwise this returns None and we forward as usual.
        native = _try_native_tool_call(msg)
        if native is not None:
            _write_message(sys.stdout.buffer, native)
            continue
        if req_id is not None:
            _PENDING_REQUESTS[req_id] = msg
        with _proc_stdin_lock:
            _write_message_to_subprocess(proc_stdin, msg)


def _drain_pending_with_error() -> None:
    """Send error responses for all pending requests when subprocess dies."""
    extension_mode = "--extension" in sys.argv
    if extension_mode:
        hint = (
            "Playwright MCP connection closed. Chrome may not be running or "
            "the Playwright extension is not active. Open Chrome and verify "
            "the extension icon shows the correct token."
        )
    else:
        hint = "Playwright MCP subprocess exited unexpectedly."

    for req_id in list(_PENDING_REQUESTS.keys()):
        error_resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": hint},
        }
        _write_message(sys.stdout.buffer, error_resp)
    _PENDING_REQUESTS.clear()


# The PUBLIC npm registry. ``@playwright/mcp`` is public, but a user's ambient
# ``.npmrc`` may point the DEFAULT registry at a private mirror (corporate proxy,
# AWS CodeArtifact) whose auth token expires — so a bare ``npx @playwright/mcp``
# 401s on this public package. When we launch via npx we pin this registry (argv
# ``--registry`` + ``npm_config_registry`` in the child env) so the on-demand
# fetch never routes through a private/stale-token default. npm and npx honor both
# forms identically on macOS, Linux, and Windows.
PUBLIC_NPM_REGISTRY = "https://registry.npmjs.org/"


def _is_npx_launcher(cmd: str) -> bool:
    """True when ``cmd`` resolves to npx (``npx`` on POSIX, ``npx.CMD`` on Windows).

    Extension-insensitive: the resolved launcher is ``npx.CMD`` on Windows and
    bare ``npx`` on POSIX. Matching on the extension-stripped basename keeps the
    registry-pin and ``@playwright/mcp`` argv logic identical across platforms.
    """
    return os.path.splitext(os.path.basename(cmd))[0].lower() == "npx"


def _resolve_playwright_cmd(search_path: str | None = None) -> str | None:
    """Find the public ``@playwright/mcp`` CLI, resolving via PATH/npx.

    Resolution order:
      1. ``KIROCREW_PLAYWRIGHT_CMD`` override (explicit path/command).
      2. A ``mcp-server-playwright``/``playwright-mcp`` binary on PATH.
      3. ``npx`` — the public ``@playwright/mcp`` package is launched via
         ``npx @playwright/mcp`` when no standalone binary is installed.

    ``search_path`` overrides the PATH ``shutil.which`` searches. The proxy (a
    standalone CLI process) passes ``None`` and searches its own inherited PATH.
    The setup path passes a Node-AUGMENTED PATH so a version-manager /
    ``ensure-node.sh`` toolchain the gateway daemon did not inherit is still
    found — without it, a daemon that bootstrapped Node via the ``node-bin-dir``
    marker (which is NOT written into ``os.environ["PATH"]``) would resolve
    ``ensure_node()`` yet see no ``npx`` here and wrongly conclude no launcher.

    Returns ``None`` when no launcher is resolvable (e.g. Node/npm absent),
    so callers can fail gracefully rather than spawning a missing binary.
    """
    override = os.environ.get("KIROCREW_PLAYWRIGHT_CMD")
    if override:
        return override
    for binary in ("mcp-server-playwright", "playwright-mcp"):
        found = shutil.which(binary, path=search_path)
        if found:
            return found
    # Return the RESOLVED path, never the bare name: on Windows npx ships only
    # as ``npx.CMD`` and CreateProcess does not apply PATHEXT, so spawning the
    # literal "npx" raises FileNotFoundError even though PATHEXT-aware
    # shutil.which found it.
    npx = shutil.which("npx", path=search_path)
    if npx:
        return npx
    return None


def run_proxy(args: list[str]) -> None:
    """Main proxy loop."""
    # Augment PATH with the Node toolchain dirs BEFORE resolving, and export it to
    # every child. The gateway spawns this proxy with its own inherited PATH, which
    # on a marker-bootstrapped host (Node installed by ensure-node.sh, recorded in
    # the node-bin-dir marker, NOT written into os.environ["PATH"]) lacks npx. Setup
    # resolves the launcher on exactly this augmented PATH, so without matching it
    # here setup would detect npx and skip priming while the runtime proxy then
    # can't find npx to launch — the "setup and runtime resolve from different
    # PATHs" split. Aligning them is what keeps enable and launch consistent.
    from kiro_crew.env import node_augmented_path

    os.environ["PATH"] = node_augmented_path(os.environ.get("PATH", ""))
    playwright_cmd = _resolve_playwright_cmd()
    if playwright_cmd is None:
        error_resp = {
            "jsonrpc": "2.0",
            "id": 0,
            "error": {
                "code": -32000,
                "message": (
                    "Playwright MCP not available: install the public "
                    "@playwright/mcp package (e.g. `npx @playwright/mcp`) "
                    "or set KIROCREW_PLAYWRIGHT_CMD."
                ),
            },
        }
        _write_message(sys.stdout.buffer, error_resp)
        sys.exit(1)
    spawn_env = dict(os.environ)
    if playwright_cmd.endswith(".js"):
        cmd = ["node", playwright_cmd] + args
    elif _is_npx_launcher(playwright_cmd):
        # npx fetches the package on first use. ``--yes`` suppresses the install
        # prompt (an npx flag, so it precedes the package spec). Launch the EXACT
        # version the enable-time prime recorded, falling back to ``@latest`` when
        # none is pinned: a pinned version resolves from the warm cache with no
        # registry round-trip (so an offline launch works) and can't drift past the
        # browser revision provisioned at enable time. The PUBLIC-registry pin rides
        # ONLY on ``npm_config_registry`` in the child env — not an argv flag: npx
        # option support varies by version and any flag after the package spec is
        # forwarded to @playwright/mcp instead, whereas the env var is honored by
        # npm/npx on every version and OS. This is what stops a private/stale-token
        # default ``.npmrc`` from 401-ing this public package.
        # circular import: browser.setup imports PUBLIC_NPM_REGISTRY / _is_npx_launcher
        # / _resolve_playwright_cmd from THIS module at its top level, so importing it
        # at module scope here would be a cycle. Deferred to call time (this runs once
        # per proxy spawn, not hot) — same reason config.paths is imported lazily above.
        from kiro_crew.browser.setup import get_pinned_playwright_version

        pinned = get_pinned_playwright_version()
        spec = f"@playwright/mcp@{pinned}" if pinned else "@playwright/mcp@latest"
        cmd = [playwright_cmd, "--yes", spec] + args
        spawn_env["npm_config_registry"] = PUBLIC_NPM_REGISTRY
        # prefer-offline: when the pinned version is already in the npx cache, launch
        # it WITHOUT a registry round-trip (so an offline host still starts); npx only
        # reaches the network when the version is genuinely absent. npm config, so it
        # rides the env var cross-platform like the registry pin above.
        spawn_env["npm_config_prefer_offline"] = "true"
    else:
        cmd = [playwright_cmd] + args

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            env=spawn_env,
        )
    except (OSError, FileNotFoundError) as exc:
        error_resp = {
            "jsonrpc": "2.0",
            "id": 0,
            "error": {"code": -32000, "message": f"Cannot start Playwright MCP: {exc}"},
        }
        _write_message(sys.stdout.buffer, error_resp)
        sys.exit(1)

    stdin_thread = threading.Thread(
        target=_forward_stdin_to_subprocess_tracked,
        args=(sys.stdin.buffer, proc.stdin),
        daemon=True,
    )
    stdin_thread.start()

    # Active pump: keep the dashboard mirror current during idle gaps. Disabled
    # in extension mode and a no-op until a browse session is active + watched.
    if _pump_enabled:
        threading.Thread(
            target=_pump_loop, args=(proc.stdin,), daemon=True
        ).start()

    while True:
        msg = _read_message(proc.stdout)
        if msg is None:
            break
        req_id = msg.get("id")
        if _is_pump_id(req_id):
            # Proxy-injected active-pump screenshot: relay it, never forward it
            # to kiro-cli, and don't touch _PENDING_REQUESTS (pump ids aren't
            # tracked there).
            _clear_pump_inflight(req_id)
            _relay_pump_frame(msg)
            continue
        if req_id is None and "error" in msg:
            continue
        if req_id is not None:
            original = _PENDING_REQUESTS.pop(req_id, None)
            _note_browse_activity(original)
        msg = _maybe_compress_response(msg)
        _write_message(sys.stdout.buffer, msg)

    _drain_pending_with_error()
    proc.wait()
    sys.exit(proc.returncode or 0)


def main() -> None:
    """Entry point for ``kirocrew mcp-playwright-proxy``."""
    run_proxy(sys.argv[1:])


if __name__ == "__main__":
    main()
