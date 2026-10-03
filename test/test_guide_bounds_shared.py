"""Every structure bounding a guide's actions or its id reads the catalog's constants.

The MCP input schema, the tool-argument validator and the gateway store each
bound the same two populations; a bare literal in any of them would let the
three drift apart, so the values must match and no literal may restate them.
"""

from __future__ import annotations

from kiro_crew import guide_catalog


def _tool(defs: list[dict], name: str) -> dict:
    return next(d for d in defs if d["name"] == name)


def test_every_bound_matches_the_catalog_constants() -> None:
    from kiro_crew import mcp_guide, validation
    from kiro_crew.dashboard import guide_runs

    defs = mcp_guide._tool_definitions()
    start = _tool(defs, "guide_start")["inputSchema"]["properties"]["actions"]
    assert start["maxItems"] == guide_catalog.MAX_ACTIONS
    for name in ("guide_status", "guide_cancel"):
        prop = _tool(defs, name)["inputSchema"]["properties"]["guide_id"]
        assert prop["maxLength"] == guide_catalog.GUIDE_ID_MAX_LEN

    (actions,) = validation.GUIDE_START_SCHEMA.fields
    assert actions.max_items == guide_catalog.MAX_ACTIONS
    for schema in (validation.GUIDE_STATUS_SCHEMA, validation.GUIDE_CANCEL_SCHEMA):
        (gid,) = schema.fields
        assert gid.max_len == guide_catalog.GUIDE_ID_MAX_LEN
        assert gid.pattern.pattern.endswith(f"{{1,{guide_catalog.GUIDE_ID_MAX_LEN}}}$")

    assert guide_runs._GUIDE_ID_MAX == guide_catalog.GUIDE_ID_MAX_LEN


def test_no_bound_is_restated_as_a_literal() -> None:
    import inspect

    from kiro_crew import mcp_guide, validation

    guide_src = inspect.getsource(mcp_guide._tool_definitions)
    assert '"maxItems": ' + str(guide_catalog.MAX_ACTIONS) not in guide_src
    assert '"maxLength": ' + str(guide_catalog.GUIDE_ID_MAX_LEN) not in guide_src
    val_src = inspect.getsource(validation)
    start = val_src.index("GUIDE_LIST_ACTIONS_SCHEMA")
    block = val_src[start : val_src.index("MCP_GUIDE_SCHEMAS", start)]
    assert "max_items=" + str(guide_catalog.MAX_ACTIONS) not in block
    assert "max_len=" + str(guide_catalog.GUIDE_ID_MAX_LEN) not in block
