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
    sparse, downscaled copy to the dashboard adds load with no benefit.
    """
    if _EXTENSION_MODE:
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
            resp = urllib.request.urlopen(req, timeout=2)
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
    the user already sees their own Chrome).
    """
    if _EXTENSION_MODE:
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
        resp = urllib.request.urlopen(req, timeout=2)
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


def _forward_stdin_to_subprocess_tracked(client_stdin, proc_stdin) -> None:
    """Forward client→subprocess, tracking in-flight IDs to synthesize errors if subprocess dies."""
    while True:
        msg = _read_message_from_client(client_stdin)
        if msg is None:
            proc_stdin.close()
            break
        req_id = msg.get("id")
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


def _resolve_playwright_cmd() -> str | None:
    """Find the public ``@playwright/mcp`` CLI, resolving via PATH/npx.

    Resolution order:
      1. ``KIROCREW_PLAYWRIGHT_CMD`` override (explicit path/command).
      2. A ``mcp-server-playwright``/``playwright-mcp`` binary on PATH.
      3. ``npx`` — the public ``@playwright/mcp`` package is launched via
         ``npx @playwright/mcp`` when no standalone binary is installed.

    Returns ``None`` when no launcher is resolvable (e.g. Node/npm absent),
    so callers can fail gracefully rather than spawning a missing binary.
    """
    override = os.environ.get("KIROCREW_PLAYWRIGHT_CMD")
    if override:
        return override
    for binary in ("mcp-server-playwright", "playwright-mcp"):
        found = shutil.which(binary)
        if found:
            return found
    if shutil.which("npx"):
        return "npx"
    return None


def run_proxy(args: list[str]) -> None:
    """Main proxy loop."""
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
    if playwright_cmd.endswith(".js"):
        cmd = ["node", playwright_cmd] + args
    elif os.path.basename(playwright_cmd) == "npx":
        cmd = [playwright_cmd, "@playwright/mcp"] + args
    else:
        cmd = [playwright_cmd] + args

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            env=os.environ,
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
