"""Enterprise-build values for the controls governance cannot reach.

``docs/request-for-change/rfc-crew-enterprise-edition.md`` (appendix, "Red")
lists controls an enterprise build hardcodes instead of leaving to a policy. Each
feature's own on/off read asks :func:`locked_off`, so the rule lives in one place
and an ``oss`` build (``edition.is_enterprise()`` false) is unchanged.

Read at call time so ``monkeypatch.setattr("kiro_crew.edition.EDITION",
"enterprise")`` reaches every site.
"""

from __future__ import annotations

from typing import Any

from kiro_crew import edition

#: Said by a refused action, so the reason names the build rather than a setting.
LOCKED_REASON = "turned off in the enterprise edition"


def locked_off() -> bool:
    """True when this build hardcodes the Red control off."""
    return edition.is_enterprise()


def sandbox_mode_floor(mode: Any, default: Any) -> Any:
    """``agent.sandbox`` as read, except an enterprise build cannot read ``off``.

    There ``off`` reads as the shipped *default*, so local config cannot go below
    Crew's own sandbox; ``strict`` still narrows (Red "Minimum sandbox level"). An
    ``oss`` build returns *mode* unchanged. Every reader of the setting calls this:
    the config builder and the knowledge pool's raw read.
    """
    if mode == "off" and locked_off():
        return default
    return mode


def unsandboxed_exec_floor(allow: bool, platform_default: bool) -> bool:
    """``sandbox_allow_unsandboxed_exec`` as read; an enterprise build cannot widen it.

    There a declared ``true`` reads as *platform_default* (fail-closed where a
    backend can exist), for the same reason as :func:`sandbox_mode_floor`; a
    declared ``false`` still narrows. An ``oss`` build returns *allow* unchanged.
    """
    if allow and locked_off():
        return platform_default
    return allow
