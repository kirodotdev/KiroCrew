"""Untrusted Text SDK -- screen, neutralize and fence text a model must read as data.

An app that puts outside text into an agent's prompt (a calendar invite's title,
an attendee list, a document link) needs the gateway's own controls for it: the
prompt-injection screen, the neutralizer that scrubs every untrusted fence marker
and prompt boundary marker, and the fence markers themselves. Restating them in
the app would leave a control that looks present and drifts from the one the
gateway's own surfaces use, and every fence must neutralize every other one, so
the markers have to come from one table.

Always present on ``ctx.untrusted``, like ``ctx.scrub``: it adds no reach (each
call is a pure function of its argument, or an audit row attributed to this app),
and an app refused it would ship its own patterns. The audit row's surface is
``<app>_<surface>`` and its agent is the app's name, both taken from the context,
so an app cannot attribute a dropped field to another app or to a core surface.
"""

from __future__ import annotations

import re

#: The fences an app may wrap text in, by kind. A closed vocabulary: each kind is a
#: fence the core already neutralizes in every other untrusted block.
UNTRUSTED_FENCE_KINDS: tuple[str, ...] = ("calendar",)

#: ``surface`` is written into the security event log, so it is held to a short
#: identifier, as ``ctx.require_owner``'s operation is.
_SURFACE_RE = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")
#: The session key recorded with a dropped field: a label, capped, never parsed.
_MAX_SESSION_KEY = 256


def _markers(kind: str) -> tuple[str, str]:
    if kind == "calendar":
        from kiro_crew.context import (
            UNTRUSTED_CALENDAR_FENCE_CLOSE,
            UNTRUSTED_CALENDAR_FENCE_OPEN,
        )

        return UNTRUSTED_CALENDAR_FENCE_OPEN, UNTRUSTED_CALENDAR_FENCE_CLOSE
    raise ValueError(f"unknown untrusted fence kind {kind!r}; one of {UNTRUSTED_FENCE_KINDS}")


class UntrustedTextSDK:
    """The gateway's untrusted-text controls, with audit rows attributed to one app."""

    __slots__ = ("_app",)

    def __init__(self, app_name: str) -> None:
        if not isinstance(app_name, str) or not app_name:
            raise ValueError("an untrusted text SDK needs the app's name")
        self._app = app_name

    @property
    def app_name(self) -> str:
        return self._app

    @staticmethod
    def flagged(text: str | None) -> bool:
        """Whether *text* matches the gateway's prompt-injection screen."""
        from kiro_crew.security import contains_injection

        return contains_injection(text)

    @staticmethod
    def neutralize(text: str) -> str:
        """*text* with every untrusted fence marker and prompt boundary marker scrubbed."""
        from kiro_crew.context import neutralize_untrusted_text

        return neutralize_untrusted_text(text)

    @staticmethod
    def fence_markers(kind: str = "calendar") -> tuple[str, str]:
        """The ``(open, close)`` markers of the fence *kind*; :class:`ValueError` if unknown."""
        return _markers(kind)

    def fence(self, body: str, *, kind: str = "calendar") -> str:
        """*body*, neutralized, between the open and close markers of *kind*.

        One line each: ``open``, the body, ``close``. Neutralizing an already
        neutralized body changes nothing, so a caller that screened each field
        first gets the same bytes back; a caller that did not still cannot close
        or forge the fence from inside it.
        """
        open_marker, close_marker = _markers(kind)
        return f"{open_marker}\n{self.neutralize(body)}\n{close_marker}"

    def record_dropped(self, surface: str, *, session_key: str = "", sample: str = "") -> None:
        """Record in the security event log that a screened field was withheld.

        Logged as surface ``<app>_<surface>`` with agent ``<app>``. A *surface*
        that is not a short identifier (lowercase letters, digits and ``_``, at
        most 64) is logged as ``<app>_unnamed`` rather than refused, because the
        field was still withheld. The log keeps a redacted, truncated *sample*.
        Best effort, like every audit write.
        """
        from kiro_crew.security import audit_injection_dropped

        label = (
            surface if isinstance(surface, str) and _SURFACE_RE.fullmatch(surface) else "unnamed"
        )
        audit_injection_dropped(
            surface=f"{self._app}_{label}",
            session_key=str(session_key)[:_MAX_SESSION_KEY],
            agent=self._app,
            sample=str(sample),
        )
