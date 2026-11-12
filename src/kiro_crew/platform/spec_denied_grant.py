"""A KAS spec grant that holds only while Crew's gate checks the spec's denies.

kiro-cli runs a command an ``allowedCommands`` entry (or an ``allowedTools`` shell
grant) covers unless a ``deniedCommands`` entry names it, and a write
``allowedPaths`` or ``allowedTools`` covers unless a ``deniedPaths`` entry does.
On KAS the projection reads any such deny as the author's restriction and asks
for every call, because a deny it carries as a KAS glob may not read the way
kiro-cli's regex does.

When Crew can read every deny itself (:mod:`kiro_crew.platform.allowed_commands`,
:mod:`kiro_crew.platform.allowed_paths`), the projection records the grants those
denies held back here, WITH the denies. The two travel as one value, so a surface
that drops it drops the grant too and the call keeps asking. The gate refuses a
call a deny names (``spec-denies`` row) and grants the rest only when the deny
check answers a clear no (``spec-builtin-grant`` row). A leaf: stdlib and the
two readers only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SpecDeniedGrant:
    """The grants a KAS session's own agent gets only after Crew checks its denies.

    *builtins* are kiro-cli built-in names (``execute_bash``, ``fs_write``),
    *commands* ``allowedCommands`` entries, *denied_commands* the spec's
    ``deniedCommands`` as written, *denied_write_paths* its write ``deniedPaths``
    anchored to absolute globs at the session's directory *cwd*.

    *web_fetch_blocked* are the spec's ``web_fetch.blocked`` entries that are plain
    URLs (``spec_denied_gate.url_literal``), read only to withhold the grant,
    *web_fetch_blocked_complete* whether that is every entry, and
    *web_fetch_trusted* the plain-URL ``web_fetch.trusted`` entries it grants by.
    """

    builtins: frozenset[str] = frozenset()
    commands: tuple[str, ...] = ()
    denied_commands: tuple[str, ...] = ()
    denied_write_paths: tuple[str, ...] = ()
    cwd: str = ""
    web_fetch_blocked: tuple[str, ...] = ()
    web_fetch_blocked_complete: bool = True
    web_fetch_trusted: tuple[str, ...] = ()


def as_spec_denied_grant(value: Any) -> SpecDeniedGrant | None:
    """*value* when it is a :class:`SpecDeniedGrant`, else ``None`` (no grant)."""
    return value if isinstance(value, SpecDeniedGrant) else None
