"""Thin, leak-safe resolver for KAS's ``_kiro/auth/getAccessToken`` callback.

When KAS is launched with ``--auth=acp-callback`` (see :mod:`kas_assets`), it
keeps NO refresh token of its own: whenever it needs an access token it calls
back to this host over ACP. kiro-cli already owns the OIDC refresh token and
exposes a hidden subcommand -- ``kiro-cli chat _ get-kas-token`` -- that
resolves-and-refreshes the token and prints it as one JSON line. So this host's
entire auth job is a passthrough: shell out to that command and hand the result
back to KAS. No OIDC, no refresh logic, no token custody lives here.

Security (no credential in logs, no information disclosure via errors):

* The access token exists only as a transient local for the duration of one
  callback -- it is never cached, persisted, or written to a log.
* Failure paths raise :class:`KasAuthCallbackError` whose message is a fixed
  human string, NEVER the subprocess output -- a malformed line could itself be
  a live token with trailing junk, so it must not travel in an exception or a
  traceback.
* The one place upstream text is surfaced (kiro-cli's own ``kind: "error"``
  message, which carries no token) is still run through ``redact_credentials``
  defensively before it leaves this module.
* The command is spawned with an argv LIST and no shell, so there is no command
  injection surface, and a timeout guarantees a hung kiro-cli cannot wedge the
  auth callback (and with it every prompt on the connection).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from kiro_crew.kiro_cli import resolve_kiro_cli

#: Subcommand that resolves + refreshes the KAS access token and prints one
#: JSON line. Hidden kiro-cli IPC surface; may change without notice.
_GET_KAS_TOKEN_ARGV_TAIL = ("chat", "_", "get-kas-token")

#: Hard cap on the callback subprocess. A refresh is a network round-trip, so
#: allow room, but never let a hung process block the connection forever.
_CALLBACK_TIMEOUT_SECS = 20.0

#: The only keys forwarded to KAS, matching ``GetAccessTokenResponse`` in the
#: ACP type covenant. Filtering (rather than forwarding ``data`` wholesale)
#: keeps any future kiro-cli field from leaking onto the wire unreviewed.
_COVENANT_KEYS = ("accessToken", "expiresAt", "profileArn", "authMethod", "provider")


class KasAuthCallbackError(RuntimeError):
    """A KAS auth callback could not be fulfilled.

    Its ``str`` is always a fixed, token-free description -- safe to log and to
    send back as a JSON-RPC error message.
    """


async def resolve_kas_access_token(
    *,
    timeout: float = _CALLBACK_TIMEOUT_SECS,
) -> dict[str, Any]:
    """Resolve a fresh KAS access token via kiro-cli, as the ACP response dict.

    Returns the ``GetAccessTokenResponse`` body KAS expects
    (``accessToken`` + ``expiresAt`` always, ``profileArn`` / ``authMethod`` /
    ``provider`` when kiro-cli supplies them). Raises :class:`KasAuthCallbackError`
    on any failure, with a message that never contains token bytes.
    """
    binary = await asyncio.to_thread(resolve_kiro_cli)
    if not binary:
        raise KasAuthCallbackError("kiro-cli not found; cannot obtain a KAS token")

    argv = [binary, *_GET_KAS_TOKEN_ARGV_TAIL]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        # Spawn failure carries no token; the OSError text is a path/errno.
        raise KasAuthCallbackError(f"could not launch kiro-cli: {exc}") from None

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(ProcessLookupError, asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        raise KasAuthCallbackError("kiro-cli token callback timed out") from None
    except asyncio.CancelledError:
        # Runtime/loop teardown cancelled this callback task mid-flight. SIGKILL
        # the credential subprocess synchronously so it cannot linger detached;
        # do NOT await here (we are unwinding a cancellation) — the loop's child
        # watcher reaps the killed process.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise

    return _parse_token_output(stdout_b, stderr_b)


def _parse_token_output(stdout_b: bytes, stderr_b: bytes) -> dict[str, Any]:
    """Parse the subprocess output into the ACP response, leaking nothing.

    Split out from the spawn so it can be unit-tested without a real process.
    """
    stdout = stdout_b.decode("utf-8", "replace").strip()
    if not stdout:
        # No JSON at all. Deliberately do NOT echo stderr: it is untrusted
        # subprocess output and credential redaction is best-effort, so the only
        # way to GUARANTEE no token leaks here is to never surface it. The raw
        # stderr is dropped rather than logged for the same reason.
        raise KasAuthCallbackError("kiro-cli returned no token output")

    # Use only the last line: kiro-cli emits exactly one JSON line, but a stray
    # leading log line must not derail the parse.
    last = stdout.splitlines()[-1]
    try:
        obj = json.loads(last)
    except (ValueError, TypeError):
        # DO NOT put `last`/`stdout` in the message -- it may be a live token.
        raise KasAuthCallbackError("could not parse kiro-cli token output") from None

    if not isinstance(obj, dict):
        raise KasAuthCallbackError("kiro-cli token output was not a JSON object")

    kind = obj.get("kind")
    data = obj.get("data")
    if not isinstance(data, dict):
        raise KasAuthCallbackError("kiro-cli token output had no data object")

    if kind == "error":
        # Do NOT surface the subprocess-provided message: it is untrusted and
        # could in principle carry sensitive text, and this module is not an
        # egress boundary (so it must not call a redactor either — the
        # test_security_posture contract). Raise a fixed, generic, actionable
        # message; the specific cause stays in kiro-cli's own logs.
        raise KasAuthCallbackError("kiro-cli authentication failed; run `kiro-cli login`")

    if kind != "getKasToken":
        raise KasAuthCallbackError(f"unexpected kiro-cli token output kind: {kind!r}")

    if not data.get("accessToken"):
        raise KasAuthCallbackError("kiro-cli token output missing accessToken")

    # Forward ONLY the covenant keys that are present. Never log this dict.
    return {k: data[k] for k in _COVENANT_KEYS if k in data}
