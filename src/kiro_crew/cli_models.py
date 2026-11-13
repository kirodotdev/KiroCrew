"""``kirocrew models [NAME] [--json]``: the model names ``--model`` accepts.

The list is the running gateway's own ``GET /api/models`` answer, the one the
dashboard picker shows: the configured backend's catalog with its cache and
entitlement narrowing. The CLI reads it through the same owner-verified secret
send as ``kirocrew spawn`` (``cli_commands_gateway_api._gateway_send``), so it
never spawns ``kiro-cli`` itself and never forks a second copy of that logic.

With no NAME it prints each model's exact ``--model`` string, its label and a
default marker. With NAME it says whether the name is in the list and, when it
is not, the closest names. ``--json`` prints the same data for scripts. A
degraded gateway answer (signed out, list unavailable) is reported with the
gateway's own message and exits 1, as the picker shows its notice.

shortcut: the gateway serves a cached list without saying how old it is, so this
command cannot flag a stale list; add an age field to ``/api/models`` when a
reader needs it.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import urllib.error
import urllib.request

from kiro_crew.cli_commands_gateway_api import (
    _client_port,
    _gateway_send,
    _internal_secret,
    _peer_refusal,
)
from kiro_crew.terminal_safe import safe_terminal_line

# The ``code`` values ``/api/models`` puts on a 503: signed out (kiro-cli, or the
# bundled KAS engine), and the degraded answers a retry can clear. Any other code
# (a missing bundle, no sandbox) is printed without inviting a retry.
_SIGNED_OUT_CODES = frozenset({"kiro_prerequisite_required", "kas_bundle_sign_in_required"})
_RETRYABLE_CODES = frozenset({"model_list_unavailable", "model_list_revalidating"})
_AUTO = "auto"


def configured_default() -> str:
    """The configured ``agent.model``, with unset and ``default`` read as ``auto``."""
    from kiro_crew.config import KiroCrewConfig

    try:
        model = str(getattr(KiroCrewConfig.load().agent, "model", "") or "").strip()
    except Exception:
        model = ""
    return _AUTO if model.lower() in ("", "default", _AUTO) else model


def model_rows(payload: object, default: str) -> list[dict[str, object]]:
    """The ``/api/models`` rows as ``{name, label, description, default}``.

    A row without a string ``model_name`` names nothing ``--model`` could take,
    so it is dropped.
    """
    rows: list[dict[str, object]] = []
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict):
            continue
        name = row.get("model_name")
        if not isinstance(name, str) or not name.strip():
            continue
        label = row.get("display_name")
        description = row.get("description")
        rows.append(
            {
                "name": name,
                "label": label if isinstance(label, str) and label else name,
                "description": description if isinstance(description, str) else "",
                "default": name == default,
            }
        )
    return rows


def check_name(name: str, names: list[str]) -> tuple[bool, list[str]]:
    """Whether *name* is a listed model, and if not up to three closest names.

    ``auto`` is always accepted: it is the configured default's own spelling and
    means "let the backend pick". Closeness ignores case, so ``Claude-Sonnet``
    still suggests ``claude-sonnet``.
    """
    if name in names or name.strip().lower() == _AUTO:
        return True, []
    by_fold = {n.lower(): n for n in names}
    close = difflib.get_close_matches(name.lower(), list(by_fold), n=3, cutoff=0.5)
    return False, [by_fold[c] for c in close]


def _fetch(port: int) -> tuple[object | None, str]:
    """GET ``/api/models`` from the gateway: ``(payload, "")`` or ``(None, error)``."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/models",
        headers={"X-Internal-Secret": _internal_secret(port)},
    )
    try:
        # The gateway answers a cold catalog within its own 10 s bound.
        with _gateway_send(req, port, 20, "cli_models_list") as resp:
            return json.loads(resp.read()), ""
    except _peer_refusal() as exc:
        return None, f"gateway ownership not verified: {exc}"
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        message = str(body.get("error") or f"{exc.code} {exc.reason}")
        code = body.get("code")
        if code in _SIGNED_OUT_CODES:
            return None, f"the backend is signed out: {message}"
        if code in _RETRYABLE_CODES:
            return None, f"model list unavailable: {message}; try again in a moment"
        return None, f"model list unavailable: {message}"
    except (urllib.error.URLError, OSError) as exc:
        from kiro_crew.loopback_http import deadline_expired

        if deadline_expired(exc):
            return None, f"gateway on port {port} is up but did not answer in time; try again"
        return None, f"gateway not running (cannot reach dashboard on port {port})"
    except ValueError:
        return None, "gateway returned an unreadable answer"


def _models(args: argparse.Namespace) -> int:
    """Run ``kirocrew models``; returns the process exit code."""
    as_json = bool(getattr(args, "json", False))
    payload, error = _fetch(_client_port(getattr(args, "port", None)))
    if payload is None:
        if as_json:
            print(json.dumps({"error": error}))
        else:
            print(f"Error: {error}", file=sys.stderr)
        return 1
    default = configured_default()
    rows = model_rows(payload, default)
    name = getattr(args, "name", None)
    if name is not None:
        valid, closest = check_name(name, [str(r["name"]) for r in rows])
        if as_json:
            print(json.dumps({"name": name, "valid": valid, "closest": closest}))
        elif valid:
            print(f"{safe_terminal_line(name)} is a valid --model name")
        else:
            print(f"{safe_terminal_line(name)} is not a model this backend offers")
            if closest:
                print("Closest: " + ", ".join(safe_terminal_line(c) for c in closest))
        return 0 if valid else 1
    if as_json:
        print(json.dumps({"default": default, "models": rows}))
        return 0
    if not rows:
        print("No models listed.")
        return 0
    width = max(len(safe_terminal_line(str(r["name"]))) for r in rows)
    for r in rows:
        shown = safe_terminal_line(str(r["name"]))
        mark = "*" if r["default"] else " "
        # The label is shown only when it says more than the name itself.
        label = safe_terminal_line(str(r["label"]))
        extra = safe_terminal_line(str(r["description"]))
        line = f"{mark} {shown.ljust(width)}  {label if label != shown else ''}".rstrip()
        print(f"{line}  ({extra})" if extra else line)
    if not any(r["default"] for r in rows):
        print(f"\nConfigured default {safe_terminal_line(default)} is not in this list.")
    else:
        print("\n* configured default")
    return 0
