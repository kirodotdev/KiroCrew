"""Extras an update adds to the shadow tree, beyond the ones it carries.

:func:`kiro_crew.platform.wheel_engine._carried_extras` keeps every extra the
replaced tree already has, so an extra a release starts recommending would never
reach anyone who updates. The memory index's extra is added here, best effort.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from kiro_crew.platform import wheel_engine_build_child as _build_child
from kiro_crew.platform.wheel_engine_contract import (
    _PIP_INSTALL_TIMEOUT_SECS,
    WheelUpdateCancelled,
    WheelUpdateError,
    _BuildContext,
)

# The update's own logger: its log lines and tests filter on this name.
logger = logging.getLogger("kiro_crew.platform.wheel_engine")

#: The extra an update adds when the tree it replaces does not carry it yet.
MEMORY_INDEX_EXTRA: Final = "memory-index"


def add_memory_index_extra(
    shadow_pip: list[str],
    policy: list[str],
    wheel_path: Path,
    extras: tuple[str, ...],
    ctx: _BuildContext,
) -> None:
    """Add the memory index's extra to the shadow tree, best effort.

    Its usearch wheels need glibc 2.28+ on Linux, so it is no core dependency.
    This adds it from wheels only (*policy*), with the new wheel's own pins, where
    the host has them. Without a wheel, or on any other pip failure, the update
    goes on: recall then reads every memory, as it does with the index off. A
    cancel still cancels. A tree that carries the extra already is left alone.
    """
    if MEMORY_INDEX_EXTRA in extras:
        return
    try:
        # Looked up on the module, so a test that patches the build step sees it.
        _build_child._run(
            shadow_pip + ["install", "--quiet"] + policy + [f"{wheel_path}[{MEMORY_INDEX_EXTRA}]"],
            _PIP_INSTALL_TIMEOUT_SECS,
            "pip install of the memory-index extra",
            ctx=ctx,
        )
    except WheelUpdateCancelled:
        raise
    except WheelUpdateError as exc:
        logger.warning(
            "Update: the memory-index extra did not install (%s); recall keeps reading "
            "every memory. The update continues.",
            exc,
        )
