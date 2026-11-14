"""``kirocrew learn`` and ``kirocrew memory`` while a provider replaces Crew's memory.

The CLI normally opens Crew's stores directly. A provider is reached only by the
gateway, so while one replaces Crew's memory the lesson verbs go through the gateway's
own ``/api/lessons`` routes (the ones ``learn_add`` uses), and the verbs that only make
sense for Crew's own stores (migrate, carve, dream, import) refuse. A gateway that is
not running is exit 2 with a message, never a silent write to Crew's own stores.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any


def provider_replaces() -> bool:
    """Whether a provider replaces Crew's memory for the global store right now."""
    from kiro_crew.memory_provider.resolver import provider_configured, route_for

    return provider_configured() and route_for("default").replaces


def refuse(verb: str) -> None:
    print(
        f"`{verb}` works on Crew's own memory, which a memory provider replaces right now. "
        "Switch back in Settings > Memory > Memory provider to use it.",
        file=sys.stderr,
    )
    sys.exit(2)


def _call(method: str, path: str, body: dict[str, Any] | None, port: int | None) -> dict:
    from kiro_crew.cli_commands_gateway_api import _client_port, _internal_secret
    from kiro_crew.loopback_http import loopback_urlopen

    resolved = _client_port(port)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{resolved}{path}",
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": _internal_secret(resolved),
            # The dashboard's static key, as ``memory_forget_client`` sends: the
            # operator's terminal writes the global store, as the dashboard does.
            "X-Session-Key": "dashboard:ui",
        },
        method=method,
    )
    try:
        with loopback_urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except ValueError:
            return {"ok": False, "error": f"HTTP {exc.code}"}
    except OSError as exc:
        print(
            f"The memory provider is reached through the gateway, which is not running ({exc}). "
            "Start it with `kirocrew start`.",
            file=sys.stderr,
        )
        sys.exit(2)


def learn(action: str, cleaned_add: dict[str, str], remove_query: str, port: int | None) -> None:
    if action == "add":
        answer = _call("POST", "/api/lessons", cleaned_add, port)
        if answer.get("ok"):
            print(f"Saved ({answer.get('outcome', 'inserted')}): {cleaned_add.get('rule', '')}")
            return
        reason = answer.get("reason") or answer.get("error") or answer.get("outcome")
        print(f"Lesson NOT saved: {reason}", file=sys.stderr)
        sys.exit(1)
    if action == "remove":
        answer = _call("DELETE", "/api/lessons", {"rule": remove_query}, port)
        if answer.get("ok"):
            print(f"Removed lessons matching: {remove_query}")
            return
        # A provider that could not be reached is not "no match": say why.
        why = answer.get("reason") or answer.get("error")
        print(
            f"Not removed: {why}" if why else f"No lessons match: {remove_query}", file=sys.stderr
        )
        sys.exit(1)
    answer = _call("GET", "/api/lessons?limit=200", None, port)
    for row in answer.get("lessons") or []:
        print(f"- {row.get('rule', '')}")
    if answer.get("lessons_withheld"):
        print(f"(lessons unavailable: {answer['lessons_withheld']})", file=sys.stderr)
