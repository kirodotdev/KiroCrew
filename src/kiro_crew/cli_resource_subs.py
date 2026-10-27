"""``kirocrew resource-subs``: the operator's view of MCP resource subscriptions.

``rfc-mcp-resource-subscriptions.md`` §4.5 and §4.8 (Phase 4). The operator can
see and remove any session's subscription; a session reaches only its own,
through the ``kirocrew-core`` tools.

* ``list`` reads the store under the data home. Every change the running service
  makes is written there before it is published, so the file is the record
  whether or not a gateway runs.
* ``remove <id>`` asks the running gateway first, so the holder lets go of the
  URI as well. It edits the store directly only when the connection is refused
  or the gateway explicitly says its service is not running, and then asks once
  more: a service built between the first ask and that edit loaded the
  subscription from the file and would keep serving it, and asking again is what
  makes it let go. An unreadable, failed or timed-out gateway answer is an
  error, never an offline edit.

Every value printed here came from a session, an MCP server, the store or the
gateway: an agent writes the instruction, the server accepts the URI and names the
pause reason. Each goes through :func:`_shown`, so a value carrying terminal
escapes cannot rewrite the listing the operator audits and removes from, and a
newline cannot fake a row of its own.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from kiro_crew.terminal_safe import safe_terminal_line

_OPERATOR_PATH = "/api/resource-subscriptions/operator"
_TIMEOUT_SECS = 30

#: Asks that confirm an offline edit, and the pause between them. A gateway
#: whose service is starting may be loading the entry the edit took out, so that
#: answer is asked again; the whole sequence stays inside ``_TIMEOUT_SECS``, the
#: bound a single ask already answers within.
_CONFIRM_ASKS = 10
_CONFIRM_PAUSE_SECS = 1.0


def _pause(seconds: float) -> None:
    """Wait *seconds* between confirming asks.

    Named rather than called inline so a caller can replace this one wait
    without rebinding the whole process's ``time.sleep``.
    """
    time.sleep(seconds)


def _shown(value: Any) -> str:
    return safe_terminal_line(str(value))


def _store() -> Any:
    from kiro_crew.mcp_gateway.resource_subscriptions import default_store

    return default_store()


def _rows() -> list[dict[str, Any]]:
    from kiro_crew.dashboard.handlers.resource_subscriptions import rows_for

    return rows_for(_store().load())


def _print_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No resource subscriptions.")
        return
    for row in rows:
        state = _shown(row["state"]) + (f" ({_shown(row['reason'])})" if row["reason"] else "")
        print(
            f"  {_shown(row['id'])}  {_shown(row['session_key'])}  {_shown(row['server'])}  {state}"
        )
        print(f"      uri: {_shown(row['uri'])}")
        wakes, instruction = _shown(row["wakes_today"]), _shown(row["instruction"])[:80]
        print(f"      wakes today: {wakes}  instruction: {instruction}")


def _gateway_error(message: str) -> None:
    print(f"Error: {_shown(message)}", file=sys.stderr)
    raise SystemExit(1)


def _connection_refused(exc: BaseException) -> bool:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, ConnectionRefusedError)


def _ask_gateway(body: dict[str, Any]) -> Optional[dict[str, Any]]:
    """POST *body* to the operator route; ``None`` only when connection is refused."""
    from kiro_crew.config.loader import read_local_secret
    from kiro_crew.loopback_http import loopback_urlopen
    from kiro_crew.port_resolution import resolve_client_port_ex

    port, _evidence = resolve_client_port_ex(None)
    headers = {"Content-Type": "application/json"}
    secret = read_local_secret(port, "127.0.0.1")
    if secret:
        headers["X-Internal-Secret"] = secret
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{_OPERATOR_PATH}",
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    try:
        with loopback_urlopen(request, timeout=_TIMEOUT_SECS) as response:
            reply = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        _gateway_error(f"the gateway answered HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        if _connection_refused(exc):
            return None
        from kiro_crew.loopback_http import deadline_expired

        if deadline_expired(exc):
            _gateway_error(f"the gateway did not answer within {_TIMEOUT_SECS} seconds; try again")
        _gateway_error(f"could not read the gateway reply: {exc}")
    except (TypeError, ValueError) as exc:
        _gateway_error(f"could not read the gateway reply: {exc}")
    if not isinstance(reply, dict):
        _gateway_error("the gateway reply was not a JSON object")
    return reply


def _removed_by_service(reply: dict[str, Any], shown_id: str) -> int:
    """What a running service's answer means for the operator's exit code."""
    if reply.get("ok"):
        print(f"Removed {shown_id}.")
        return 0
    code = reply.get("refused")
    label = f" ({_shown(code)})" if code else ""
    reason = _shown(reply.get("reason") or "not removed")
    print(f"Error{label}: {reason}", file=sys.stderr)
    return 1


def _unconfirmed(shown_id: str, detail: str) -> int:
    """The exit for an edit the gateway did not confirm nobody is still serving."""
    print(
        f"Error: {shown_id} is removed from the store, but {detail}, so a service may "
        "still be holding it; run this command again.",
        file=sys.stderr,
    )
    return 1


def _confirm_nothing_holds(body: dict[str, Any], shown_id: str) -> int:
    """Ask again, after an offline edit, whether a service holds the subscription.

    A service is the process's active one before it loads the store, and a closed
    one neither holds nor writes, so one that loaded this subscription before the
    edit landed answers this ask, and answering it is what makes it drop the
    holder and write the removal. A service that loaded after the edit never saw
    the subscription and answers that there is none.

    No store lock is held here: a running service needs that lock to take the
    removal, and holding it across the request would stall the route.
    """
    from kiro_crew.dashboard.handlers.resource_subscriptions import NOT_FOUND, OPERATOR_STARTING

    deadline = time.monotonic() + _TIMEOUT_SECS
    for remaining in range(_CONFIRM_ASKS - 1, -1, -1):
        try:
            reply = _ask_gateway(body)
        except SystemExit:
            # _ask_gateway has already named the failure; this says what it left.
            return _unconfirmed(shown_id, "the gateway could not be asked")
        if reply is None or reply.get("running") is False:
            print(f"Removed {shown_id} from the store (no subscription service running).")
            return 0
        refused = reply.get("refused")
        if reply.get("running") is True and (reply.get("ok") or refused == NOT_FOUND):
            print(f"Removed {shown_id}.")
            return 0
        if reply.get("running") is not True or refused != OPERATOR_STARTING:
            answer = _shown(reply.get("reason") or "no removal")
            return _unconfirmed(shown_id, f"the gateway answered {answer}")
        if not remaining or time.monotonic() >= deadline:
            break
        _pause(_CONFIRM_PAUSE_SECS)
    return _unconfirmed(shown_id, "its subscription service was still starting")


def _remove(subscription_id: str) -> int:
    body = {"subscription_id": subscription_id}
    reply = _ask_gateway(body)
    shown_id = _shown(subscription_id)
    if reply is not None:
        if reply.get("running") is True:
            return _removed_by_service(reply, shown_id)
        if reply.get("running") is not False:
            print(
                "Error: the gateway reply did not say whether the subscription service is "
                "running",
                file=sys.stderr,
            )
            return 1
    if not _store().remove_subscription(subscription_id):
        print(f"Error: no subscription {shown_id}", file=sys.stderr)
        return 1
    return _confirm_nothing_holds(body, shown_id)


def resource_subs(args: argparse.Namespace) -> None:
    """Dispatch ``kirocrew resource-subs``."""
    from kiro_crew.mcp_gateway.resource_subscription_store import StoreUnreadable

    action = getattr(args, "resource_subs_action", None)
    try:
        if action == "list":
            _print_rows(_rows())
            return
        if action == "remove":
            sys.exit(_remove(args.subscription_id))
    except StoreUnreadable as exc:
        print(f"Error: {_shown(exc)}", file=sys.stderr)
        sys.exit(1)
    print("Usage: kirocrew resource-subs {list,remove}", file=sys.stderr)
    sys.exit(2)
