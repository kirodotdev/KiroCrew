"""The daemon's environment: telemetry off, cloud unreachable, browser = the relay.

Browser Harness 0.1.13 reads its switches from the environment, and the defaults
point the wrong way for Kiro Crew: telemetry is PostHog, ON by default, and its
``browser_harness_run`` event carries the agent's whole script plus up to 20,000
characters of output with no scrubbing; a ``BROWSER_USE_API_KEY`` or ``BU_BROWSER_ID``
in the environment turns the daemon into a client of Browser Use's hosted browser.

:func:`daemon_env` is the ONE function that builds the daemon's environment, so the
fence below is pinned by a single test rather than spread over call sites. Every
variable it sets is forced -- a value already in the base environment is overwritten
-- and every variable it removes stays removed even when the gateway's own
environment carries it (``test_browser_harness_env``).
"""

from __future__ import annotations

from collections.abc import Mapping

#: Harness's three telemetry switches (``telemetry.DISABLE_ENVS``). All three are
#: set because the package reads any one of them.
TELEMETRY_SWITCHES: tuple[str, ...] = (
    "BH_TELEMETRY",
    "BROWSER_HARNESS_TELEMETRY",
    "ANONYMIZED_TELEMETRY",
)

#: Where a telemetry POST would go if a switch were ever missed: a loopback port
#: with no listener (the discard port). A second fence, not the first.
DEAD_POSTHOG_HOST = "http://127.0.0.1:9"

#: The relay URL variable. Harness resolves it with an HTTP ``/json/version`` call
#: and connects to the ``webSocketDebuggerUrl`` it answers; the relay serves both.
CDP_URL_ENV = "BU_CDP_URL"

#: Values forced into the daemon's environment, over anything the base carries.
FORCED_ENV: Mapping[str, str] = {
    **{switch: "false" for switch in TELEMETRY_SWITCHES},
    "BH_POSTHOG_HOST": DEAD_POSTHOG_HOST,
    # A timeout on a POST that never happens; kept short so a missed switch
    # could not stall a helper either.
    "BH_TELEMETRY_TIMEOUT": "1",
    # No cosmetic title marker painted into the page the person is watching.
    "BH_TAB_MARKER": "0",
    # The CLI's PyPI version probe. The gateway never runs the CLI, but the switch
    # costs nothing and keeps the daemon's process tree off the network.
    "BH_UPDATE_CHECK": "0",
}

#: Variables removed from the daemon's environment. Each is a way into Browser
#: Use's cloud or a way to point the daemon at a browser other than the relay.
REMOVED_ENV: tuple[str, ...] = (
    # The cloud API key: with it, ``admin.py`` can start a hosted browser.
    "BROWSER_USE_API_KEY",
    # A hosted browser id: ``daemon.BROWSER_KIND`` becomes ``cloud`` when set.
    "BU_BROWSER_ID",
    # A raw websocket URL takes precedence over ``BU_CDP_URL`` in ``get_ws_url``.
    "BU_CDP_WS",
    # Lets the CLI launch its own browser instead of connecting to ours.
    "BU_AUTOSPAWN",
    # The cloud API base and OAuth client: unreachable without the key, removed so
    # a future release that reads them first finds nothing.
    "BROWSER_USE_CLOUD_API_URL",
    "BROWSER_HARNESS_OAUTH_CLIENT_ID",
    "BH_AUTH_PATH",
    # ``daemon.py`` loads ``<workspace>/.env`` with ``setdefault``: a stray file
    # could re-add a removed key. The supervisor sets ``BH_AGENT_WORKSPACE`` to a
    # directory it owns; a value inherited from the gateway's shell is dropped.
    "BH_AGENT_WORKSPACE",
    # Harness's own home and config roots. The supervisor pins them to the hidden
    # IPC directory; an inherited value would send the socket somewhere visible.
    "BH_HOME",
    "BROWSER_HARNESS_HOME",
    "BH_CONFIG_DIR",
    "BH_RUNTIME_DIR",
    "BH_TMP_DIR",
    "BH_RUNTIME_DIR_SHARED",
    "BH_TMP_DIR_SHARED",
)


#: The loopback names the daemon's two hops to the relay use. Both ``urllib``
#: (``/json/version``) and ``websockets`` honour ``NO_PROXY`` and nothing else:
#: neither bypasses a proxy for loopback on its own, so a shell with
#: ``HTTPS_PROXY`` set and no ``NO_PROXY`` would send the relay URL -- and the
#: per-start secret in it -- to the proxy host, which cannot reach our loopback.
LOOPBACK_NO_PROXY: tuple[str, ...] = ("127.0.0.1", "localhost")

#: Both spellings, because ``urllib`` reads the lower-case one first and
#: ``websockets`` reads whichever the platform gives it.
NO_PROXY_KEYS: tuple[str, ...] = ("NO_PROXY", "no_proxy")


def _with_loopback_bypass(value: str) -> str:
    """*value* (an existing ``NO_PROXY`` list, maybe empty) with the loopback names added."""
    entries = [e.strip() for e in value.split(",") if e.strip()]
    for name in LOOPBACK_NO_PROXY:
        if name not in entries:
            entries.append(name)
    return ",".join(entries)


def daemon_env(base: Mapping[str, str], relay_url: str) -> dict[str, str]:
    """The environment the Harness daemon is started with.

    *base* is the gateway's own environment (``PATH``, locale, ``HOME`` and the
    like are inherited so the venv's Python and Chrome's helpers resolve).
    *relay_url* is the loopback HTTP URL of the gateway's CDP relay; it is the
    only browser the daemon can reach. The proxy variables are inherited (the
    daemon itself fetches nothing, but a helper may), with the loopback names
    appended to ``NO_PROXY`` so the relay hop is never routed through a proxy.
    """
    env = {k: v for k, v in base.items() if k not in REMOVED_ENV}
    env.update(FORCED_ENV)
    env[CDP_URL_ENV] = relay_url
    # One merged value for both spellings: a bypass present under one and not the
    # other would still leave one of the two libraries proxying the loopback hop.
    merged = ",".join(env.get(k, "") for k in NO_PROXY_KEYS)
    bypass = _with_loopback_bypass(merged)
    for key in NO_PROXY_KEYS:
        env[key] = bypass
    return env
