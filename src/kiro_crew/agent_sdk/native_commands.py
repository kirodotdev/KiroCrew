"""Promptless native command batches through the agent-backend boundary.

Application code receives plain JSON-like dicts and a stable outcome code. The
ACP driver owns process construction, exception translation, timeout, and
teardown; no ACP class crosses this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from kiro_crew.agent_sdk.drivers import acp as acp_driver


@dataclass(frozen=True)
class NativeCommandBatch:
    """A bounded command batch result with no backend-specific types."""

    code: str
    results: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return self.code == "ok"


async def run_kiro_native_commands(
    commands: Sequence[str],
    *,
    work_dir: Path,
    agent: str,
    session_key: str,
    timeout_seconds: float,
) -> NativeCommandBatch:
    """Run native commands on one promptless kiro-cli session.

    Commands execute in order under one total timeout. The driver returns only
    structured command results; callers must reduce them before external use.
    """
    code, results = await acp_driver.run_kiro_native_commands(
        tuple(commands),
        work_dir=work_dir,
        agent=agent,
        session_key=session_key,
        timeout_seconds=timeout_seconds,
    )
    return NativeCommandBatch(code=code, results=tuple(results))


__all__ = ["NativeCommandBatch", "run_kiro_native_commands"]
