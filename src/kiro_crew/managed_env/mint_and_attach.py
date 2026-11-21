#!/usr/bin/env python3
"""ssh ``ProxyCommand`` for ``crew-sbx-<id>``: one fresh StartSshSession, then the plugin.

Usage (from the ``Host crew-sbx-*`` block that :mod:`.ssh_config` writes)::

    ProxyCommand '/path/to/python3' -m kiro_crew.managed_env.mint_and_attach %n

Every ssh invocation runs this once, so every invocation mints a new single-use tuple
(``StartSshSession`` is not idempotent). Nothing resumes a stopped sandbox: a sandbox that
is not RUNNING answers 409 and the line says so.

The bearer lives only in the dashboard's memory, so this process does not call the
service. It asks the dashboard's owner-only loopback route
``POST /api/managed-environments/<id>/ssh-session``, which goes through the client (known
ids only, call gap, stop rules, call log). Where the dashboard is comes from the environment
of the ssh that runs this: ``KIROCREW_SBX_DASHBOARD`` (``http://127.0.0.1:<port>``, loopback
only), else the port the gateway already exports to every child it spawns
(``KIROCREW_BOUND_PORT``). This process mints its own owner credential the way
``kirocrew token`` does: it runs ``python -m kiro_crew token --port <port>``, which reads the
core's own local secret and asks the gateway's ``/api/token/local``. The dashboard passes
none to the ssh it spawns, and the credential lives only in this process's memory. The
answer is read into memory and handed to the plugin in the environment variable the plugin
names (``AWS_SSM_START_SESSION_RESPONSE``), never in argv, a file or a log. The answer also
names the Region the plugin starts in.

Other environment: ``KIROCREW_SSM_PLUGIN`` (default ``session-manager-plugin``, looked up on
PATH).

Any failure is one ``mint-and-attach:`` line on stderr and exit 255, so ssh reports a failed
connect and the caller's stderr capture carries the reason. A tuple, token or stream URL is
never printed.
"""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from kiro_crew.managed_env.engine import sandbox_id_from_host
from kiro_crew.managed_env.guard import POLICY_KIND_WORD

ENV_DASHBOARD = "KIROCREW_SBX_DASHBOARD"
ENV_BOUND_PORT = (
    "KIROCREW_BOUND_PORT"  # exported by the core gateway to its children (port_resolution.py)
)
MINT_TIMEOUT_S = 30.0
ENV_PLUGIN = "KIROCREW_SSM_PLUGIN"
PLUGIN_ENV_VAR = "AWS_SSM_START_SESSION_RESPONSE"
DEFAULT_PLUGIN = "session-manager-plugin"
TIMEOUT_S = 90.0
_MESSAGE_MAX = 200

# The plain lines a crew row shows for the common failures (the core prefixes its own
# "ssh exited 255: " to whatever this prints).
TEXT_STOPPED = "Stopped. Resume it, then Connect."
TEXT_SIGN_IN = "Sign in under Settings > Kiro account, then Connect."
TEXT_NO_DASHBOARD = "Can't reach this Crew dashboard. Is it running?"
TEXT_NO_ALIAS = "This crew's connection settings are missing. Connect it again from Your crews."


class MintError(Exception):
    """The one-line reason; it never holds a tuple, token or URL."""


def _fail(message: str) -> None:
    sys.stderr.write(f"mint-and-attach: {message}\n")
    sys.stderr.flush()
    sys.exit(255)


def _loopback(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http":
        return False
    host = parts.hostname or ""
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[override]
        return None


def _dashboard_base(env) -> str:
    """The dashboard URL: KIROCREW_SBX_DASHBOARD, else loopback on the gateway's bound port."""
    base = (env.get(ENV_DASHBOARD) or "").rstrip("/")
    if base:
        return base
    port = (env.get(ENV_BOUND_PORT) or "").strip()
    if port.isdigit() and 1 <= int(port) <= 65535:
        return f"http://127.0.0.1:{int(port)}"
    return ""


def mint_owner_token(base: str, env, run=subprocess.run) -> str:
    """Mint a fresh owner token the way ``kirocrew token`` does; held in memory, never logged.

    Runs the core CLI (``python -m kiro_crew token --port <port>``) with this ssh's environment minus
    any token variable; stdout is the sign-in URL carrying ``token=``. Only a generic reason is
    raised: no output, URL or token is included.
    """
    port = urllib.parse.urlsplit(base).port
    if not port:
        raise MintError("the dashboard URL has no port")
    child = dict(env)
    try:
        res = run(
            [sys.executable, "-m", "kiro_crew", "token", "--port", str(port)],
            env=child,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=MINT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise MintError(f"could not mint an owner token ({type(e).__name__})") from None
    if res.returncode != 0:
        raise MintError(f"could not mint an owner token (kirocrew token exit {res.returncode})")
    lines = [ln.strip() for ln in (res.stdout or "").splitlines() if "token=" in ln]
    token = (
        urllib.parse.parse_qs(urllib.parse.urlsplit(lines[-1]).query).get("token", [""])[0]
        if lines
        else ""
    )
    if not token:
        raise MintError("kirocrew token printed no token")
    return token


def mint_tuple(
    sandbox_id: str, env: dict | os._Environ = os.environ, opener=None, token_source=None
) -> dict:
    """One StartSshSession through the dashboard; returns the three tuple fields."""
    base = _dashboard_base(env)
    if not base:
        raise MintError(f"{TEXT_NO_DASHBOARD} ({ENV_DASHBOARD} and {ENV_BOUND_PORT} are not set)")
    if not _loopback(base):
        raise MintError(f"{ENV_DASHBOARD} must be an http loopback URL")
    token = (token_source or mint_owner_token)(base, env)
    url = f"{base}/api/managed-environments/{urllib.parse.quote(sandbox_id, safe='')}/ssh-session"
    url += "?" + urllib.parse.urlencode({"token": token})
    req = urllib.request.Request(url, data=b"", method="POST")
    opener = opener or urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise MintError(_http_reason(e)) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise MintError(f"{TEXT_NO_DASHBOARD} ({type(e).__name__})") from None
    if not isinstance(body, dict) or not all(
        isinstance(body.get(k), str) and body.get(k) for k in _TUPLE_KEYS
    ):
        raise MintError("the dashboard answer has no ssh tuple")
    return {k: body[k] for k in _TUPLE_KEYS}


#: The route's answer: the session's three fields and the Region the plugin starts in
#: (``instances.managed_env_ssh_region``; the route refuses when it is unset).
_TUPLE_KEYS = ("ssmSessionId", "streamUrl", "tokenValue", "region")


def _http_reason(e: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(e.read())
    except Exception:  # noqa: BLE001 - any unreadable body just has no detail
        body = {}
    body = body if isinstance(body, dict) else {}
    if e.code == 409 and body.get("kind") == "not_ready":
        return TEXT_STOPPED
    if e.code == 409 and body.get("kind") == "not_signed_in":
        return TEXT_SIGN_IN
    if e.code == 409 and body.get("kind") == "not_configured" and body.get("error"):
        return str(body["error"])[:_MESSAGE_MAX]
    if e.code in (401, 403):
        return f"the dashboard refused the request (HTTP {e.code}); sign in again"
    text = str(body.get("error") or "")[:_MESSAGE_MAX]
    if e.code in (422, 424) and body.get("kind") == POLICY_KIND_WORD and text:
        # the organization's policy refused this box: the plain line alone, no op or code
        return text
    return f"StartSshSession failed (HTTP {e.code}{': ' + text if text else ''})"


def plugin_env_and_argv(tup: dict, plugin: str, region: str) -> tuple[list[str], dict]:
    """The plugin argv and env: the tuple goes in the env var the plugin names."""
    session = json.dumps(
        {
            "SessionId": tup["ssmSessionId"],
            "StreamUrl": tup["streamUrl"],
            "TokenValue": tup["tokenValue"],
        }
    )
    env = {k: v for k, v in os.environ.items() if k != ENV_DASHBOARD}
    env[PLUGIN_ENV_VAR] = session
    return [plugin, PLUGIN_ENV_VAR, region, "StartSession"], env


def main(argv: list[str], *, execvpe=os.execvpe) -> None:
    if len(argv) != 2:
        _fail("usage: python -m kiro_crew.managed_env.mint_and_attach crew-sbx-<id>")
    sandbox_id = sandbox_id_from_host(argv[1])
    if not sandbox_id:
        _fail(f"{TEXT_NO_ALIAS} (host alias is not crew-sbx-<id>)")
    try:
        tup = mint_tuple(sandbox_id)
    except MintError as e:
        _fail(str(e))
    plugin = os.environ.get(ENV_PLUGIN, DEFAULT_PLUGIN)
    args, env = plugin_env_and_argv(tup, plugin, tup["region"])
    try:
        execvpe(plugin, args, env)
    except OSError as e:
        _fail(f"cannot run {plugin} ({type(e).__name__}); is session-manager-plugin on PATH?")


if __name__ == "__main__":
    main(sys.argv)
