"""Whether a resumed session's saved cwd can still be opened, and the notice when not.

``get_or_create`` reads these on every start that reloads a saved session and
re-exports them for the callers that import them from there.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from kiro_crew.agent_sdk.backends import ACP_BACKENDS_CWD_BOUND_SESSIONS


def _resume_would_move_the_session(provider: Any, stored_cwd: str) -> bool:
    """Whether loading *provider*'s saved session would restore it outside its own folder.

    Only for a harness in ``ACP_BACKENDS_CWD_BOUND_SESSIONS``, which serves a session
    only in the directory it was created in. A saved session with no recorded folder
    (its project was cleared) counts as moved: a fresh session with the conversation
    replayed is always correct there, and a load in the wrong folder is not.
    """
    backend = getattr(getattr(provider, "client", None), "backend", None)
    if not (isinstance(backend, str) and backend in ACP_BACKENDS_CWD_BOUND_SESSIONS):
        return False
    cwd = getattr(provider, "cwd", "")
    if not stored_cwd or not isinstance(cwd, str) or not cwd:
        return True
    return os.path.normpath(stored_cwd) != os.path.normpath(cwd)


def _stored_cwd_is_hidden_scratch(
    stored_cwd: str, shared_scratch: object, session_key: str, backend_override: str | None
) -> bool:
    """Whether a resumed session's recorded cwd is a scratch tree its new process cannot read.

    Each process gets its own directory under the scratch root, and the sandbox hides
    every other one. A conversation that worked in its ``$KIROCREW_SCRATCH`` records
    that directory as its cwd, but its next process gets a fresh tree, so the old one
    is hidden from it and the child cannot read its own working directory (kiro-cli
    exits at start-up with ``EPERM ... uv_cwd`` on macOS). The tree the spawn shares
    (``shared_scratch``) is re-exposed to it, so a cwd inside that one is kept. A session
    that starts unsandboxed hides nothing, so its cwd is kept too: its tier is
    ``agent.sandbox`` after the governance floor, raised to the ``agent.harness_sandbox``
    floor of the backend the provider factory selects for *session_key* and
    *backend_override*.
    Native Windows applies no scratch-root mask at all: a Kiro spawn is delegated to
    Kiro's own sandbox and Crew has no Windows sandbox backend, so the cwd is kept there.
    On every platform a cwd that is not a directory (the scratch sweep removed or
    quarantined its tree) is unreadable too, so it is never kept.
    """
    from kiro_crew import platform_compat
    from kiro_crew.agent_scratch import scratch_root
    from kiro_crew.agent_sdk.tool_gate import harness_sandbox_mode
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.crewmate_harness import crewmate_backend_pin
    from kiro_crew.members import select_provider_backend
    from kiro_crew.sandbox_tiers import _clamp_sandbox_mode, configured_sandbox_mode

    path = Path(os.path.realpath(stored_cwd))
    if not path.is_relative_to(os.path.realpath(scratch_root())):
        return False
    if not os.path.isdir(path):
        return True
    if platform_compat.IS_WINDOWS:
        return False
    try:
        mode = _clamp_sandbox_mode(configured_sandbox_mode())
        # A floor only raises a tier, so only an "off" tier needs the session's backend.
        if mode == "off":
            cfg = KiroCrewConfig.load()
            backend = select_provider_backend(
                session_key,
                cfg.agent.member_acp_backend,
                cfg.agent.acp_backend,
                backend_override=backend_override,
                crew_backend=(
                    crewmate_backend_pin(cfg, session_key) if backend_override is None else None
                ),
            )
            if harness_sandbox_mode(backend, mode, cfg.agent.harness_sandbox) == "off":
                return False
    except Exception:
        # An unreadable config or backend counts as masked: the session then starts
        # in its default directory, which it can always read.
        pass
    if shared_scratch is not None and path.is_relative_to(os.path.realpath(str(shared_scratch))):
        return False
    return True


#: ``startup_notice`` token for a start that left a hidden scratch cwd behind.
HIDDEN_SCRATCH_CWD_NOTICE = "hidden_scratch_cwd_skipped"


def _note_skipped_hidden_cwd(provider: Any, skipped_cwd: str) -> None:
    """Tell the chat its folder could not be reopened, once, on the provider's start."""
    if not skipped_cwd or getattr(provider, "startup_notice", None) is not None:
        # A notice the start itself left (a cloud reopen) is not overwritten.
        return
    provider.startup_notice = (
        HIDDEN_SCRATCH_CWD_NOTICE,
        f"This chat's folder {skipped_cwd} was a temporary folder from an earlier "
        "run and can't be opened anymore. The chat now works in the default folder.",
    )
