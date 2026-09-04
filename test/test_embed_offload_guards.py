"""Source-assertion tests for embedding offload contracts.

These tests verify that specific call sites route blocking embedding work
through the ``mc-embed`` bulkhead pool (``run_in_embed_pool``) rather than
the shared default executor.

The pattern mirrors ``TestWindowsTeardownOffLoop`` in ``test_mcp_discovery.py``:
assert against the shipped source rather than simulating a platform-specific
run, because the branches may be unreachable on CI's platform.
"""

from __future__ import annotations

import inspect


class TestPersonalShopperEmbedOffload:
    """personal_shopper routes must use run_in_embed_pool for embed-heavy ops.

    ``store.add``, ``store.search``, ``store.update`` and ``store.reembed_all``
    call the synchronous embedder and block for 60-90s per invocation. These
    MUST route through ``run_in_embed_pool`` (the bounded mc-embed bulkhead
    pool) instead of ``asyncio.to_thread`` (the shared default executor) so
    that embedding work cannot starve fast I/O offloads that share the default
    pool.
    """

    def test_store_add_uses_embed_pool(self) -> None:
        from kiro_crew.apps.builtins.personal_shopper.backend import routes

        src = inspect.getsource(routes._handle_add_preference)
        assert (
            "run_in_embed_pool" in src
        ), "store.add must be offloaded via run_in_embed_pool, not asyncio.to_thread"
        assert "store.add" in src, "handler must call store.add"

    def test_store_search_uses_embed_pool(self) -> None:
        from kiro_crew.apps.builtins.personal_shopper.backend import routes

        src = inspect.getsource(routes._handle_search_preferences)
        assert (
            "run_in_embed_pool" in src
        ), "store.search must be offloaded via run_in_embed_pool, not asyncio.to_thread"
        assert "store.search" in src, "handler must call store.search"

    def test_store_reembed_all_uses_embed_pool(self) -> None:
        from kiro_crew.apps.builtins.personal_shopper.backend import routes

        src = inspect.getsource(routes._handle_reembed_preferences)
        assert (
            "run_in_embed_pool" in src
        ), "store.reembed_all must be offloaded via run_in_embed_pool, not asyncio.to_thread"
        assert "store.reembed_all" in src, "handler must call store.reembed_all"

    def test_store_update_uses_embed_pool(self) -> None:
        """``store.update`` re-embeds on a text change, so it is embed-heavy too.

        ``PreferenceStore.update`` writes ``_embed(new_text)`` whenever ``text``
        is supplied, which makes it a sibling of add/search/reembed rather than
        a fast metadata write. Pinning it here stops the offload contract from
        being a point patch that leaves one embedding path on the shared pool.
        """
        from kiro_crew.apps.builtins.personal_shopper.backend import routes, store

        src = inspect.getsource(routes._handle_update_preference)
        assert (
            "run_in_embed_pool" in src
        ), "store.update must be offloaded via run_in_embed_pool, not asyncio.to_thread"
        assert "store.update" in src, "handler must call store.update"
        # The premise above must stay true: update embeds on a text change.
        assert "_embed(new_text)" in inspect.getsource(
            store.PreferenceStore.update
        ), "premise broken: store.update no longer embeds, so this pin is stale"

    def test_non_embed_ops_still_use_to_thread(self) -> None:
        """list_all and delete never embed, so they stay on asyncio.to_thread."""
        from kiro_crew.apps.builtins.personal_shopper.backend import routes

        list_src = inspect.getsource(routes._handle_list_preferences)
        assert "asyncio.to_thread" in list_src, "list_all should use asyncio.to_thread"

        delete_src = inspect.getsource(routes._handle_delete_preference)
        assert "asyncio.to_thread" in delete_src, "delete should use asyncio.to_thread"
