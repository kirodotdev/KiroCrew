"""Tool declarations of the ``control`` domain of ``kirocrew-core``.

The builder lives in :mod:`kiro_crew.mcp_tools.control_parts.schemas`, an owner module of
:mod:`kiro_crew.mcp_tools.control` that imports none of that domain's handlers (see the
package docstring).
"""

from __future__ import annotations

from kiro_crew.mcp_tools.control_parts.schemas import schemas

__all__ = ["schemas"]
