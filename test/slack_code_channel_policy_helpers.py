"""A real ``channels.posture.slack.allowed_channel_ids`` policy for code-channel tests.

:func:`allow_only` installs a parsed policy whose per-channel ceiling names explicit
channel ids, so a test drives the same policy seam (``channel_policy.channel_refusal``)
every inbound path and write applies, rather than a patched verdict.
"""

from __future__ import annotations

from typing import Any

import pytest


def allow_only(monkeypatch: pytest.MonkeyPatch, *channel_ids: str) -> list[dict[str, Any]]:
    """Install a policy admitting only *channel_ids*; return the SEL rows it writes."""
    from kiro_crew.platform import context as pc
    from kiro_crew.platform.governance import parse_policy

    ceiling = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "channels": {
                "members": {"mode": "allow", "allow": ["slack"]},
                "posture": {
                    "slack": {"allowed_channel_ids": {"mode": "allow", "allow": list(channel_ids)}}
                },
            },
        }
    )
    real = pc.current_context

    class _Context:
        governance = ceiling

        def __getattr__(self, name: str) -> Any:
            return getattr(real(), name)

    context = _Context()
    monkeypatch.setattr(pc, "current_context", lambda: context)
    rows: list[dict[str, Any]] = []

    class _Sel:
        def log_api_access(self, **kw: Any) -> None:
            rows.append(kw)

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    return rows


def policy_refusals(rows: list[dict[str, Any]], operation: str) -> list[dict[str, Any]]:
    """The policy seam's denied rows for *operation*."""
    return [
        r
        for r in rows
        if r.get("operation") == operation
        and r.get("outcome") == "denied"
        and r.get("error") == "channel_not_allowed_by_policy"
    ]
