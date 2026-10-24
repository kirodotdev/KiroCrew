"""Do the live sessions apply MCP config edits on their own?

kiro-cli reconciles the agent config it was spawned with against the file on
disk: a watcher on ``~/.kiro/agents`` and ``mcp.json`` restarts only the changed
MCP servers, keeps the conversation, and applies the edit at the next turn
boundary. When every running session does that, the dashboard's MCP writers do
NOT need the all-sessions reset they otherwise perform after a config change —
the reset exists only to make kiro-cli re-read a file it already watches.

KAS applies the same change by a second mechanism: Crew re-sends its projected
agent over the wire at the session's next turn boundary
(:data:`ACP_BACKENDS_MCP_WIRE_REINJECT`, performed by
:mod:`kiro_crew.acp.session_provider_mcp_reinject`). A live KAS session is
therefore also left alone -- provided the writer MARKS it, because nothing in
that engine watches a file: :func:`live_sessions_apply_mcp_live` is the gate
over both mechanisms, and the dashboard marks every re-injecting provider in
the same breath as it skips the reset.

Everything here is the gate for skipping that reset, so it fails CLOSED: a
session on a backend outside both sets, one whose handshake reported no
version, or one below the hot-reload floor all answer False and the reset keeps
running. A false positive costs the user a server that
never mounts until they restart by hand, with nothing red to say why; a false
negative costs one restart they were already paying.

The gate is keyed to what each live process reported at ``initialize``
(``agentInfo.version``), never to the binary on disk. The two differ after an
in-place kiro-cli upgrade: every process spawned before it still runs the old
image, and probing the file would answer for a version nothing is running.

What the reconcile covers, and what the dashboard must therefore write:

* an added or removed ``mcpServers`` entry is started or stopped;
* ``disabled: true`` on an entry stops its process, ``disabledTools`` hides a
  tool — both without a restart;
* a ``@server`` ref ADDED to ``tools`` is honoured from the next turn, but a ref
  REMOVED while the server keeps running stays mounted. Disabling a server must
  therefore write ``disabled: true`` onto the agent entry, not merely drop the
  ref — see :func:`kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any, Protocol

from kiro_crew.acp_backends import ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD

logger = logging.getLogger(__name__)


class DeclaresMcpHotReload(Protocol):
    """The one attribute this gate reads off a live provider.

    ``LLMProvider.mcp_config_hot_reload`` is that attribute (default False); the
    gate is typed against this Protocol rather than the provider ABC so it
    depends on the declaration, not on the agent-backend layer that makes it
    (the agent-SDK import boundary keeps application code off ``kiro_crew.acp``
    and ``kiro_crew.providers``).
    """

    @property
    def mcp_config_hot_reload(self) -> bool: ...


#: Lowest kiro-cli release the skip is granted to. kiro-cli's file watcher
#: ("Config Hot-Reload") shipped in 2.10.0, but every semantic the skip and the
#: disable path lean on -- an added entry spawns, ``disabled: true`` stops a
#: running server, a removed entry stops it -- was observed on 2.21.0 only, and
#: the cost of granting the skip to a release that reconciles differently is a
#: server that never mounts with nothing red to say why. So the floor is the
#: probed release; lowering it is a one-line change once an older release is
#: verified, whereas a wrong grant is invisible.
MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION: tuple[int, int, int] = (2, 21, 0)

# The leading dotted-integer run of a version token: ``2.21.0``, and also the
# ``2.21.0-rc.1`` / ``2.21.0+build`` spellings a prerelease build prints. Each
# component is at most nine digits and must end there (the lookahead): the
# token is harness-written (``agentInfo.version`` on the ACP handshake), and
# ``int()`` on a longer run raises past CPython's digit limit, so a component
# past the bound is never handed to ``int()``, for every caller alike.
_VERSION_RE = re.compile(r"(\d{1,9})\.(\d{1,9})(?:\.(\d{1,9}))?(?!\d)")


def parse_kiro_cli_version(text: str) -> tuple[int, int, int] | None:
    """Parse a kiro-cli version string into a comparable tuple.

    Accepts both the bare ``agentInfo.version`` form (``2.21.0``) and the
    ``kiro-cli --version`` line (``kiro-cli 2.21.0``): the LAST
    whitespace-separated token is read, so a wrapper that prefixes its own
    banner still parses. A missing patch component reads as 0. Returns None
    when no version-shaped token is present.
    """
    tokens = text.strip().split()
    if not tokens:
        return None
    match = _VERSION_RE.match(tokens[-1])
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def mcp_hot_reload_supported(backend: str, version: tuple[int, int, int] | None) -> bool:
    """Pure gate: does this backend at this version reconcile MCP edits live?

    Membership first (harness-parity H6 — the capability is opt-in per harness,
    never inferred), then the version floor. An unknown version is not "probably
    new enough": it is False.
    """
    if backend not in ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD:
        return False
    if version is None:
        return False
    return version >= MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION


def provider_hot_reloads(provider: DeclaresMcpHotReload) -> bool:
    """Whether ONE live provider's process reconciles MCP edits on its own.

    Reads the capability the provider DECLARES through the ``LLMProvider``
    contract (``mcp_config_hot_reload``, default False — harness-parity H14);
    the ACP implementation answers from backend membership plus the version its
    process reported at ``initialize``. Only a literal ``True`` counts, so a
    mocked provider's truthy attribute never reads as a skip.
    """
    return provider.mcp_config_hot_reload is True


def live_sessions_hot_reload(providers: Iterable[DeclaresMcpHotReload]) -> bool:
    """Whether EVERY live provider reconciles MCP edits, so no reset is needed.

    ``providers`` is the full set the reset would otherwise touch: registered
    sessions plus the pre-spawned warm pool. Empty means there is nothing a
    reset could reach — the next spawn reads the file fresh — so the answer is
    True. One provider that cannot be shown to reconcile makes the whole answer
    False, because a reset is all-or-nothing and skipping it leaves THAT
    session on the stale config.
    """
    for provider in providers:
        if not provider_hot_reloads(provider):
            return False
    return True


def provider_reinjects_mcp(provider: Any) -> bool:
    """Whether ONE live provider re-sends its MCP projection at its next turn.

    Reads ``LLMProvider.mcp_config_wire_reinject`` (default False, harness-parity
    H14); the ACP implementation answers from :data:`ACP_BACKENDS_MCP_WIRE_REINJECT`
    membership on a session that exists. Only a literal ``True`` counts, as for
    :func:`provider_hot_reloads`, and a provider predating the declaration (no
    attribute at all) answers False.
    """
    return getattr(provider, "mcp_config_wire_reinject", False) is True


def live_sessions_apply_mcp_live(providers: Iterable[Any]) -> bool:
    """Whether EVERY live provider applies an MCP config change without a reset.

    Each may do so by either mechanism: its process reconciles the file itself
    (:func:`provider_hot_reloads`) or Crew re-sends its projection at the next
    turn boundary (:func:`provider_reinjects_mcp`). The all-or-nothing shape and
    the empty-means-True answer are :func:`live_sessions_hot_reload`'s, for the
    same reasons. Deciding is all this does: a caller that skips the reset on a
    True answer MUST then call :func:`mark_reinjecting_providers`, or the
    re-injecting sessions keep their old set with nothing red to say why.
    """
    for provider in providers:
        if not (provider_hot_reloads(provider) or provider_reinjects_mcp(provider)):
            return False
    return True


def mark_reinjecting_providers(providers: Iterable[Any]) -> int:
    """Tell every re-injecting provider that an MCP config change is due.

    Returns how many were marked. A provider that does not re-inject is left
    alone: a hot-reloading one is already watching the file, and the gate that
    led here refused anything else. The mark is a flag the provider applies at
    its own next turn boundary, so this is synchronous and never touches the
    wire.
    """
    marked = 0
    for provider in providers:
        if not provider_reinjects_mcp(provider):
            continue
        mark = getattr(provider, "mark_mcp_config_changed", None)
        if callable(mark):
            mark()
            marked += 1
    return marked
