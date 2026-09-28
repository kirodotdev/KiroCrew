"""Dangling ``@ref`` reconcile for the assembled agent config.

``kiro-cli`` mounts what ``mcpServers`` declares, so a ``@ref`` naming a server
absent from that map mounts nothing. The rebuild narrows the map in passes that
never touch ``tools``/``allowedTools``, and these pin the reconcile that keeps
the ref lists consistent with it.
"""

from __future__ import annotations

import logging

from kiro_crew.config.sections import AgentConfig
from kiro_crew.mcp_cleanup import prune_dangling_tool_refs


def _config(**over):
    cfg = {
        "mcpServers": {"live": {"command": "/bin/live"}},
        "tools": ["fs_read", "@live", "@gone"],
        "allowedTools": ["fs_read", "@live", "@gone"],
    }
    cfg.update(over)
    return cfg


def test_a_name_declared_for_mounts_only_keeps_its_mount_and_loses_its_grant():
    # The two lists fail in opposite directions, so a caller unsure whether a name
    # is still owned names it in `declared` and leaves it out of `declared_grants`.
    # Dropping the mount could unmount a server for good -- an existing config
    # never re-adds a template ref -- while keeping the grant would leave an
    # auto-approval on the name, on the one list that never reaches the PreToolUse
    # gate. `@gone` is the other half of the same input: absent from both sets, it
    # goes from both lists, so this is not a blanket keep.
    cfg = _config(
        tools=["fs_read", "@live", "@unsure", "@unsure/run", "@gone"],
        allowedTools=["fs_read", "@live", "@unsure", "@gone"],
    )
    dropped = prune_dangling_tool_refs(cfg, declared=["unsure"], declared_grants=[])
    assert "@unsure" in cfg["tools"]
    assert "@unsure/run" in cfg["tools"]
    assert "@unsure" not in cfg["allowedTools"]
    assert "@gone" not in cfg["tools"]
    assert "@gone" not in cfg["allowedTools"]
    assert "@unsure" in dropped


def test_omitting_declared_grants_lets_both_lists_read_declared():
    # A caller with no doubt passes one set, and the grant list reads it too --
    # otherwise adding the parameter would silently start pruning every existing
    # caller's grants.
    cfg = _config(
        tools=["fs_read", "@live", "@expected", "@gone"],
        allowedTools=["fs_read", "@live", "@expected", "@gone"],
    )
    dropped = prune_dangling_tool_refs(cfg, declared=["expected"])
    assert "@expected" in cfg["tools"]
    assert "@expected" in cfg["allowedTools"]
    assert dropped == ["@gone"]


def test_a_reserved_kiro_namespace_is_not_a_server_ref():
    # `@builtin` addresses a kiro namespace, is listed by kiro's own configuration
    # reference, and never appears in mcpServers. Reading it as a dangling server
    # ref would unmount the whole built-in tool surface plus the tool_search
    # loader on every rebuild, and an existing config never re-adds the entry.
    cfg = _config(
        tools=["fs_read", "@builtin", "@live", "@gone"],
        allowedTools=["@builtin", "@builtin/fs_read", "@gone"],
    )
    assert prune_dangling_tool_refs(cfg) == ["@gone"]
    assert cfg["tools"] == ["fs_read", "@builtin", "@live"]
    assert cfg["allowedTools"] == ["@builtin", "@builtin/fs_read"]


def test_a_reserved_namespace_survives_with_no_server_map_entries_at_all():
    # The exemption cannot depend on some OTHER server being declared: a config
    # whose map is empty is exactly the shape a first rebuild sees.
    cfg = _config(mcpServers={}, tools=["@builtin"], allowedTools=["@builtin"])
    assert prune_dangling_tool_refs(cfg) == []
    assert cfg["tools"] == ["@builtin"]
    assert cfg["allowedTools"] == ["@builtin"]


def test_a_ref_whose_server_left_the_map_is_dropped_from_both_lists():
    cfg = _config()
    assert prune_dangling_tool_refs(cfg) == ["@gone"]
    assert cfg["tools"] == ["fs_read", "@live"]
    assert cfg["allowedTools"] == ["fs_read", "@live"]


def test_a_per_tool_ref_resolves_to_its_server_rather_than_being_kept_whole():
    # The grant form the rebuild's ref sync never re-emits, so it has to be
    # judged on its SERVER -- `@gone/search` is dangling, `@live/search` is not.
    cfg = _config(
        tools=["@live/search", "@gone/search"],
        allowedTools=["@gone/*", "@live/*"],
    )
    assert prune_dangling_tool_refs(cfg) == ["@gone/search", "@gone/*"]
    assert cfg["tools"] == ["@live/search"]
    assert cfg["allowedTools"] == ["@live/*"]


def test_a_declared_name_absent_from_the_map_keeps_its_refs():
    # A server that merely failed to resolve on THIS rebuild. Its per-tool grant
    # cannot be re-derived, so the refs outlive one bad PATH.
    cfg = _config(
        mcpServers={},
        tools=["@unresolvable"],
        allowedTools=["@unresolvable/search"],
    )
    assert prune_dangling_tool_refs(cfg, declared={"unresolvable"}) == []
    assert cfg["tools"] == ["@unresolvable"]
    assert cfg["allowedTools"] == ["@unresolvable/search"]


def test_the_same_name_is_dropped_once_it_is_no_longer_declared():
    # The other side of the test above, measured on the same input: without the
    # declaration the very same config loses the refs. One-sided verification
    # could not tell the guard from a blanket keep.
    cfg = _config(
        mcpServers={},
        tools=["@unresolvable"],
        allowedTools=["@unresolvable/search"],
    )
    assert prune_dangling_tool_refs(cfg) == ["@unresolvable", "@unresolvable/search"]
    assert cfg["tools"] == []
    assert cfg["allowedTools"] == []


def test_an_entry_without_the_sigil_is_left_to_the_passes_that_own_it():
    cfg = _config(tools=["fs_read", "code", "@gone"], allowedTools=["execute_bash"])
    prune_dangling_tool_refs(cfg)
    assert cfg["tools"] == ["fs_read", "code"]
    assert cfg["allowedTools"] == ["execute_bash"]


def test_a_non_string_entry_is_not_dropped_by_this_pass():
    # `allowedTools: [1]` is a hand-edit the final governance pass already drops;
    # judging it here would make two passes own the same removal.
    cfg = _config(allowedTools=[1, "@gone"])
    assert prune_dangling_tool_refs(cfg) == ["@gone"]
    assert cfg["allowedTools"] == [1]


def test_a_server_map_that_is_not_a_dict_drops_nothing():
    # No readable verdict: every ref would look dangling, so a malformed or
    # missing map must not strip the lists.
    for bad in (None, [], "mcpServers", 7):
        cfg = _config(mcpServers=bad)
        assert prune_dangling_tool_refs(cfg) == []
        assert cfg["tools"] == ["fs_read", "@live", "@gone"]
        assert cfg["allowedTools"] == ["fs_read", "@live", "@gone"]


def test_an_absent_server_map_key_drops_nothing():
    cfg = {"tools": ["@gone"], "allowedTools": ["@gone"]}
    assert prune_dangling_tool_refs(cfg) == []
    assert cfg["tools"] == ["@gone"]


def test_a_list_that_is_not_a_list_is_skipped_rather_than_replaced():
    cfg = _config(tools="@gone")
    assert prune_dangling_tool_refs(cfg) == ["@gone"]
    assert cfg["tools"] == "@gone"


def test_the_reconcile_is_idempotent():
    cfg = _config()
    first = prune_dangling_tool_refs(cfg)
    after_first = (list(cfg["tools"]), list(cfg["allowedTools"]))
    assert prune_dangling_tool_refs(cfg) == []
    assert (cfg["tools"], cfg["allowedTools"]) == after_first
    assert first == ["@gone"]


def test_a_ref_reported_once_even_when_it_dangles_in_both_lists():
    cfg = _config()
    assert prune_dangling_tool_refs(cfg).count("@gone") == 1


def test_repeated_occurrences_of_one_dangling_ref_are_all_removed():
    cfg = _config(tools=["@gone", "@live", "@gone"], allowedTools=[])
    assert prune_dangling_tool_refs(cfg) == ["@gone"]
    assert cfg["tools"] == ["@live"]


def test_order_of_surviving_refs_is_preserved():
    cfg = _config(
        mcpServers={"a": {}, "b": {}, "c": {}},
        tools=["@c", "@gone", "@a", "fs_read", "@b"],
        allowedTools=[],
    )
    prune_dangling_tool_refs(cfg)
    assert cfg["tools"] == ["@c", "@a", "fs_read", "@b"]


def test_a_bare_sigil_is_dropped_as_naming_no_server():
    cfg = _config(tools=["@", "@live"], allowedTools=[])
    assert prune_dangling_tool_refs(cfg) == ["@"]
    assert cfg["tools"] == ["@live"]


def test_an_unmounted_ref_is_recorded_where_the_shipped_log_level_shows_it(caplog):
    # A `tools` removal takes a tool OUT of the agent's surface, and this log line
    # is its only trail: SEL carries the grant side alone. So the record has to
    # clear the level the gateway actually runs at. That level is read from the
    # shipped default rather than spelled WARNING here, because the assumption is
    # what matters -- if the default is ever raised, this fails instead of the
    # record going quietly invisible to the operator who has to debug it.
    shipped = getattr(logging, AgentConfig().log_level.upper())
    cfg = _config()
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.mcp_cleanup"):
        prune_dangling_tool_refs(cfg)
    records = [r for r in caplog.records if "@gone" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno >= shipped


def test_a_grant_only_removal_stays_at_info_because_sel_already_carries_it(caplog):
    # The mount is exempt, so nothing left the agent's surface and the only change
    # is the revoked grant -- which `rebuild_agent_config` already audits as
    # `mcp_auto_approve_revoked`. Raising this record too would put one decision
    # in front of the operator twice, so the level tracks the mount list and not
    # merely whether anything was dropped.
    cfg = _config(
        tools=["fs_read", "@live", "@unsure"],
        allowedTools=["fs_read", "@live", "@unsure"],
    )
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.mcp_cleanup"):
        prune_dangling_tool_refs(cfg, declared=["unsure"], declared_grants=[])
    records = [r for r in caplog.records if "@unsure" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
