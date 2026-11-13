"""Startup signatures of a Kiro CLI child that cannot start, and their curated messages.

Split out of :mod:`kiro_crew.acp.transport_errors` (which re-exports every name
here) so that module stays under the file-line cap. Pure: regexes and strings,
no imports from the transports, so both ACP transports and the error type can
read the one classification without an import cycle.
"""

from __future__ import annotations

import re

# The POSIX ``kiro-cli`` launcher execs a SIBLING ``kiro-cli-chat`` for ``acp``
# and ``chat``; when that sibling is absent, empty or version-skewed (a stale
# symlink pointing at a removed binary is the field case) the launcher
# itself answers ``--version``/``whoami`` but the exec fails with this line and
# the child never starts. Classified for the same reason as the sandbox-init and
# auth signatures: the condition is DETERMINISTIC, so the reconnect ladder's
# respawn reproduces it exactly, and without this the operator gets the generic
# "process exited (rc=None/rc=1)" card -- which points at retry or model-switch,
# neither of which can fix an install problem -- instead of the one line that
# names the fix (reinstall/repair Kiro CLI).
#
# Anchored on the ``kiro-cli-chat`` launch token specifically. A bare
# ``failed to launch`` or a generic spawn error is ordinary output for an
# unrelated death a respawn legitimately fixes, so matching either alone would
# latch a permanent wrong verdict; requiring the delegate name costs no coverage
# because the launcher always names the sibling it could not exec.
_RE_DELEGATE_LAUNCH_FAILURE = re.compile(
    r"failed\s+to\s+launch\s+kiro-cli-chat",
    re.IGNORECASE,
)


# A Kiro CLI older than the relay argv Kiro Crew builds rejects the
# ``--auth-method`` option (``acp.kas_transport.KAS_RELAY_AUTH_FLAG``) at argument
# parse, before the child prints anything else:
# ``error: unexpected argument '--auth-method' found``. Like the delegate-launch
# line it is DETERMINISTIC -- every respawn passes the same argv to the same
# binary -- so the retry ladder only repeats the generic "process exited
# (rc=None)" card. Its remedy differs (update, not reinstall), so it carries its
# own message through the same non-retryable error.
#
# Anchored on the option name: clap prints ``unexpected argument`` for any
# unknown option, and only this one is the too-old signature Kiro Crew can name a
# fix for. The literal is spelled here rather than imported because
# ``kas_transport`` sits above this module in the import graph.
_KIRO_CLI_TOO_OLD_TOKEN = "unexpected argument '--auth-method'"


def _is_kiro_cli_too_old(haystack: str) -> bool:
    """Whether *haystack* holds clap's rejection of ``--auth-method``, any spacing or case."""
    return _KIRO_CLI_TOO_OLD_TOKEN in " ".join(haystack.split()).lower()


def delegate_launch_failure_message(haystack: str) -> str | None:
    """The curated startup-failure message for *haystack* (a child's stderr), or ``None``.

    The ONE classification point both ACP transports read, so the retry verdict
    and the wording a user sees cannot drift apart: a Kiro CLI too old for the
    ``--auth-method`` option gets :data:`KIRO_CLI_TOO_OLD_MESSAGE`, a
    ``kiro-cli-chat`` delegate that would not launch gets
    :data:`DELEGATE_LAUNCH_FAILURE_MESSAGE`, anything else ``None``. The
    too-old check runs first because it is the more specific remedy.
    """
    if _is_kiro_cli_too_old(haystack):
        return KIRO_CLI_TOO_OLD_MESSAGE
    if _RE_DELEGATE_LAUNCH_FAILURE.search(haystack):
        return DELEGATE_LAUNCH_FAILURE_MESSAGE
    return None


def is_delegate_launch_failure_output(haystack: str) -> bool:
    """True when *haystack* (a child's stderr) shows the Kiro CLI child cannot start.

    Either the ``kiro-cli-chat`` delegate failing to launch, or a Kiro CLI too old
    for the ``--auth-method`` option. Shared by both ACP transports for the same
    anti-drift reason :func:`is_sandbox_init_failure_output` and
    :func:`is_auth_failure_output` are each a single function: the two
    transports must not come to disagree about what the signature IS.
    """
    return delegate_launch_failure_message(haystack) is not None


#: Actionable, non-retryable message for a ``kiro-cli-chat`` delegate that would
#: not launch. Fixed recovery wording (``user_worded``), naming the install
#: repair rather than a retry or model switch.
DELEGATE_LAUNCH_FAILURE_MESSAGE = (
    "The Kiro CLI chat delegate (kiro-cli-chat) failed to launch. This usually "
    "means a stale or partial Kiro CLI install (for example a stale symlink "
    "pointing at a removed binary). Reinstall or repair Kiro CLI and try again."
)

#: Actionable, non-retryable message for a Kiro CLI that rejected the
#: ``--auth-method`` option because it is too old. Names the dashboard path,
#: because most users have no terminal. Used only when the Kiro CLI check was
#: latched (``mark_registered_cli_too_old`` returned True), so the setup screen's
#: Update Kiro CLI button (``POST /api/kiro-prerequisite/update-cli``) appears;
#: otherwise the build site uses :data:`KIRO_CLI_TOO_OLD_TERMINAL_MESSAGE`.
KIRO_CLI_TOO_OLD_MESSAGE = (
    "Kiro CLI is too old for this version of Kiro Crew: it does not accept the "
    "--auth-method option that Kiro Crew passes to start a KAS session, so retrying "
    "cannot help. "
    "The owner of this Kiro Crew can update it with the Update Kiro CLI button on "
    "the Kiro CLI setup screen. On the desktop app, update the "
    "app instead. Then start a new session."
)

#: The same failure when no dashboard latched the Kiro CLI check (``kirocrew
#: chat``, or a gateway whose configured backend is not KAS), so no Update Kiro
#: CLI button appears: names the command instead.
KIRO_CLI_TOO_OLD_TERMINAL_MESSAGE = (
    "Kiro CLI is too old for this version of Kiro Crew: it does not accept the "
    "--auth-method option that Kiro Crew passes to start a KAS session, so retrying "
    "cannot help. "
    "Run `kiro-cli update` in a terminal (on the desktop app, update the app "
    "instead), then start a new session."
)
