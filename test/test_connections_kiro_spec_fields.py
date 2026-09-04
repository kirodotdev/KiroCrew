"""Drift guards for the agent-spec fields kiro-cli actually deserializes.

kiro-cli accepts an unknown key in an MCP entry and drops it silently, so a spec
carrying the wrong name for an OAuth hint is not a parse error -- it is a
successful authorization for the WRONG access, with nothing in any log to say so.
That failure mode is why these are tests and not a comment: the shape is only
verifiable against the binary, and a rename back to the internal spelling would
otherwise be invisible until a user noticed a provider had over-broad access.

The names asserted here were established by differential validation against
kiro-cli 2.16.2 (``kiro-cli agent validate`` on a spec with a WRONG-TYPED value:
a recognised field reports a type error, an ignored one passes silently):

    scopes: 12345            -> accepted   => ignored
    oauthScopes: 12345       -> "expected a sequence"
    clientId: 12345          -> accepted   => ignored
    oauth: {clientId: 12345} -> "expected a string"
"""

from __future__ import annotations

from typing import Any

from kiro_crew.mcp_utils import (
    kiro_entry_client_id,
    kiro_entry_scopes,
    kiro_oauth_wire_entry,
)

# ── FIX 1: the emitted OAuth keys ──


def test_wire_entry_renames_scopes_to_the_field_kiro_cli_parses() -> None:
    out = _emit_owned({"url": "https://x/mcp", "scopes": ["read"]})
    assert out["oauthScopes"] == ["read"]
    # The internal spelling must be GONE, not merely shadowed: kiro-cli ignores
    # it, so leaving it keeps a second, non-load-bearing copy of the request.
    assert "scopes" not in out


def test_wire_entry_nests_client_id_under_oauth() -> None:
    out = _emit_owned({"url": "https://x/mcp", "clientId": "abc"})
    assert out["oauth"] == {"clientId": "abc"}
    assert "clientId" not in out


def test_wire_entry_leaves_other_shapes_alone() -> None:
    for store in ({}, None):
        stdio = {"command": "srv", "args": ["--x"]}
        assert kiro_oauth_wire_entry(stdio, store_entry=store) == stdio
        bare = {"url": "https://x/mcp"}
        assert kiro_oauth_wire_entry(bare, store_entry=store) == bare


def test_wire_entry_preserves_other_oauth_subkeys_and_drops_an_empty_dict() -> None:
    out = _emit_owned(
        {"url": "https://x/mcp", "oauth": {"issuer": "https://i"}, "clientId": "abc"}
    )
    assert out["oauth"] == {"issuer": "https://i", "clientId": "abc"}
    # A clientId-only oauth dict is ours entirely, so a source that says "no
    # clientId" must remove the key rather than leave `oauth: {}` behind.
    gone = _emit_owned({"url": "https://x/mcp", "oauth": {"clientId": "old"}, "clientId": ""})
    assert "oauth" not in gone


def test_wire_entry_drops_an_empty_scope_list() -> None:
    # An empty list is not "no scopes requested" to kiro-cli -- it documents an
    # explicit empty request. Providers with no scopes must emit no key at all,
    # matching what the registry says about them.
    out = _emit_owned({"url": "https://x/mcp", "scopes": []})
    assert "oauthScopes" not in out and "scopes" not in out


def test_entry_readers_accept_both_spellings() -> None:
    assert kiro_entry_scopes({"scopes": ["a"]}) == ["a"]
    assert kiro_entry_scopes({"oauthScopes": ["b"]}) == ["b"]
    assert kiro_entry_scopes({"oauth": {"oauthScopes": ["c"]}}) == ["c"]
    assert kiro_entry_scopes({"scopes": "nope"}) == []
    assert kiro_entry_client_id({"clientId": "a"}) == "a"
    assert kiro_entry_client_id({"oauth": {"clientId": "b"}}) == "b"
    assert kiro_entry_client_id({"clientId": 5}) == ""


# ═══════════════════════════════════════════════════════════════════════════
# The OWNERSHIP decision table. Every row below must hold SIMULTANEOUSLY --
# each is the failure mode that appears when the discriminator collapses into
# a simpler one, and fixing any row in isolation has historically re-broken
# another. ``managed`` (does the dashboard store own this name?) is the
# discriminator; internal-key presence is NOT.
#
#   managed     + hint valid    -> rebuilt from source     (row 1)
#   managed     + hint emptied  -> wire key deleted        (row 2)
#   managed     + hint absent   -> wire key deleted        (row 3)
#   NOT managed + hint absent   -> wire value preserved    (row 4)
#   any row                     -> oauth.issuer survives   (row 5)
#   any path                    -> one malformed contract  (row 6)
#   any row                     -> second pass is a no-op  (row 7)
# ═══════════════════════════════════════════════════════════════════════════


def _emit_owned(store_entry: dict[str, Any]) -> dict[str, Any]:
    """Emit a store entry that has not been merged with a prior render.

    The store entry is its own source, which is what the store holds before
    ``rebuild_agent_config`` folds the last render in on top of it.
    """
    return kiro_oauth_wire_entry(store_entry, store_entry=store_entry)


def _rendered_then_updated(
    rendered: dict[str, Any], source: dict[str, Any], *, managed: bool = True
) -> dict[str, Any]:
    """The entry shape agent.py emits: last render, ``update``-ed by mcp.json.

    ``source`` doubles as the store entry, because that is exactly what it is at
    the real call site -- the value merged in from ``<data home>/mcp.json``.
    """
    merged = dict(rendered)
    merged.update(source)
    return kiro_oauth_wire_entry(merged, store_entry=source if managed else None)


# ── Row 1: managed + hints present -> rebuilt to the source's values ──


def test_row1_a_changed_scope_list_overwrites_the_previously_rendered_one() -> None:
    out = _rendered_then_updated(
        {"url": "https://x/mcp", "oauthScopes": ["read:old"], "oauth": {"clientId": "old-id"}},
        {"url": "https://x/mcp", "scopes": ["read:new"], "clientId": "new-id"},
    )
    assert out["oauthScopes"] == ["read:new"]
    assert out["oauth"] == {"clientId": "new-id"}


def test_row1_a_widened_then_narrowed_scope_list_is_actually_narrowed() -> None:
    """The failure this guards is over-request, not a crash.

    Keeping the rendered value would pin every future session to the widest
    grant the entry was ever rendered with, so a user who narrows their scopes
    keeps authorizing the ones they removed with nothing to show it.
    """
    out = _rendered_then_updated(
        {"url": "https://x/mcp", "oauthScopes": ["read", "write", "admin"]},
        {"url": "https://x/mcp", "scopes": ["read"]},
    )
    assert out["oauthScopes"] == ["read"]


# ── Row 2: managed + explicitly emptied -> wire keys deleted ──


def test_row2_an_emptied_source_removes_the_rendered_wire_keys() -> None:
    out = _rendered_then_updated(
        {"url": "https://x/mcp", "oauthScopes": ["read"], "oauth": {"clientId": "old-id"}},
        {"url": "https://x/mcp", "scopes": [], "clientId": ""},
    )
    assert "oauthScopes" not in out
    assert "oauth" not in out
    assert out == {"url": "https://x/mcp"}


def test_row2b_an_explicit_clear_beats_a_stale_wire_sibling_in_the_same_store() -> None:
    """Precedence is KEY PRESENCE, not value truthiness.

    A store entry can hold both spellings at once: the scope-toggle preservation
    rule copies a global spec in wire form, then the custom-server API clears the
    scopes in internal form. An empty internal value that fell through to its wire
    sibling would resurrect the very grant the user just cleared -- and silently,
    since the card would still read as scoped.
    """
    store = {
        "url": "https://x/mcp",
        "scopes": [],
        "oauthScopes": ["read", "write"],
        "clientId": "",
        "oauth": {"clientId": "stale-id", "issuer": "https://i"},
    }
    assert kiro_entry_scopes(store) == []
    assert kiro_entry_client_id(store) == ""

    out = _emit_owned(store)
    assert "oauthScopes" not in out
    assert out["oauth"] == {"issuer": "https://i"}, "issuer is the user's"


def test_row2b_an_explicit_null_also_clears_rather_than_falling_through() -> None:
    """``None`` is a present key, so it is a clear -- not an absent one."""
    store = {"url": "https://x/mcp", "scopes": None, "oauthScopes": ["stale"]}
    assert kiro_entry_scopes(store) == []
    assert "oauthScopes" not in _emit_owned(store)
    store_cid = {"url": "https://x/mcp", "clientId": None, "oauth": {"clientId": "stale"}}
    assert kiro_entry_client_id(store_cid) == ""
    assert "oauth" not in _emit_owned(store_cid)


# ── Row 3: managed + hints ABSENT -> wire keys deleted ──


def test_row3_hints_removed_through_the_api_do_not_survive_the_rebuild() -> None:
    """The custom-update API removes a hint by DELETING the key, not emptying it.

    The dashboard store owns this entry, and ``dict.update()`` cannot remove a
    key, so treating absence as silence would leave the last-rendered grant in
    the spec permanently -- every future session would keep requesting access
    the user removed through the UI, with nothing anywhere to show it.
    """
    out = _rendered_then_updated(
        {"url": "https://x/mcp", "oauthScopes": ["read"], "oauth": {"clientId": "old-id"}},
        {"url": "https://x/mcp"},
    )
    assert "oauthScopes" not in out
    assert "oauth" not in out
    assert out == {"url": "https://x/mcp"}


def test_row3_a_managed_entry_with_no_hints_at_all_emits_none() -> None:
    out = _emit_owned({"url": "https://x/mcp"})
    assert out == {"url": "https://x/mcp"}


# ── Row 4: NOT managed -> wire values preserved verbatim ──


def test_row4_an_unmanaged_wire_only_entry_keeps_its_hints_verbatim() -> None:
    """A file we do not own may be hand-authored directly in wire form.

    There the wire value is the only copy, so deleting on absence destroys
    configuration we never wrote. Ownership is what separates this from row 3 --
    the two entries are INDISTINGUISHABLE by key presence alone.
    """
    wire_only = {
        "url": "https://x/mcp",
        "oauthScopes": ["read:user"],
        "oauth": {"clientId": "hand-authored", "issuer": "https://i"},
    }
    assert kiro_oauth_wire_entry(wire_only, store_entry=None) == wire_only


def test_row4_an_unmanaged_entry_survives_the_rendered_merge_path() -> None:
    out = _rendered_then_updated(
        {"url": "https://x/mcp", "oauthScopes": ["read:user"], "oauth": {"clientId": "hand"}},
        {"url": "https://x/mcp", "headers": {"X-Tenant": "acme"}},
        managed=False,
    )
    assert out["oauthScopes"] == ["read:user"]
    assert out["oauth"] == {"clientId": "hand"}


def test_row4_an_unmanaged_internal_spelling_is_still_translated() -> None:
    """Unmanaged means "absence is silence", not "never translate".

    kiro-cli ignores the internal spelling, so a hand-written ``scopes`` in a
    file we do not own would be silently dropped if we left it alone.
    """
    out = kiro_oauth_wire_entry(
        {"url": "https://x/mcp", "scopes": ["read"], "clientId": "cid"}, store_entry=None
    )
    assert out["oauthScopes"] == ["read"]
    assert out["oauth"] == {"clientId": "cid"}
    assert "scopes" not in out and "clientId" not in out


def test_row3_vs_row4_same_entry_opposite_outcomes() -> None:
    """The discriminator itself: one entry, two regimes, two correct answers.

    The entry is a previous render carrying wire hints. Owned by a store that
    states none, it is a removal; owned by nobody, it is the only copy.
    """
    rendered = {"url": "https://x/mcp", "oauthScopes": ["read"], "oauth": {"clientId": "cid"}}
    owned_says_none = kiro_oauth_wire_entry(dict(rendered), store_entry={"url": "https://x/mcp"})
    assert "oauthScopes" not in owned_says_none
    assert "oauth" not in owned_says_none
    assert kiro_oauth_wire_entry(dict(rendered), store_entry=None) == rendered


# ── Row 5: oauth.issuer survives every row ──


def test_row5_an_unrelated_oauth_subkey_survives_every_regime() -> None:
    """Only ``clientId`` is ours; ``issuer`` is the user's in all four rows."""
    base = {"url": "https://x/mcp", "oauth": {"issuer": "https://i", "clientId": "old"}}
    # row 1 -- rebuilt
    row1 = _rendered_then_updated(base, {"clientId": "new"})
    assert row1["oauth"] == {"issuer": "https://i", "clientId": "new"}
    # row 2 -- explicitly emptied
    row2 = _rendered_then_updated(base, {"clientId": ""})
    assert row2["oauth"] == {"issuer": "https://i"}
    # row 3 -- an owned store that states nothing at all
    row3 = kiro_oauth_wire_entry(dict(base), store_entry={"url": "https://x/mcp"})
    assert row3["oauth"] == {"issuer": "https://i"}
    # row 4 -- unmanaged, untouched
    row4 = kiro_oauth_wire_entry(dict(base), store_entry=None)
    assert row4["oauth"] == {"issuer": "https://i", "clientId": "old"}


# ── Row 6: ONE malformed-scope contract on every path ──


def test_row6_a_malformed_scope_member_omits_the_field_entirely() -> None:
    """kiro-cli rejects the WHOLE spec on a bad oauthScopes, dropping every tool.

    Forwarding the well-formed subset is also refused: it would silently request
    something other than what the file asks for.
    """
    for bad in (["read", 7], ["read", ""], ["read", "   "], ["read", None], "read", 7, {}):
        out = _emit_owned({"url": "https://x/mcp", "scopes": bad})
        assert "oauthScopes" not in out, bad
        assert "scopes" not in out, bad


def test_row6_emit_and_readback_agree_on_every_malformed_shape() -> None:
    """The emit path and the discovery/sync readback share one contract.

    A readback that kept the well-formed subset while emit omitted the field
    would report an access level no file asks for and no session receives, and a
    sync acting on that difference would propagate the truncated grant as if it
    were the complete one.
    """
    for bad in (["read", 7], ["read", ""], ["read", "   "], ["read", None], "read", 7, {}, None):
        emitted = _emit_owned({"url": "https://x/mcp", "scopes": bad})
        assert "oauthScopes" not in emitted, bad
        assert kiro_entry_scopes({"scopes": bad}) == [], bad
        assert kiro_entry_scopes({"oauthScopes": bad}) == [], bad
        assert kiro_entry_scopes({"oauth": {"oauthScopes": bad}}) == [], bad


def test_row6_a_clean_scope_list_agrees_on_every_path() -> None:
    clean = ["read:user", "read:org"]
    out = _emit_owned({"url": "https://x/mcp", "scopes": clean})
    assert out["oauthScopes"] == clean
    assert kiro_entry_scopes(out) == clean
    assert kiro_entry_scopes({"scopes": clean}) == clean


def test_row6_a_malformed_member_does_not_leave_a_stale_rendered_list_standing() -> None:
    out = _rendered_then_updated(
        {"url": "https://x/mcp", "oauthScopes": ["read", "write"]},
        {"url": "https://x/mcp", "scopes": ["read", 7]},
    )
    assert "oauthScopes" not in out


def test_row6_a_blank_client_id_is_treated_as_absent() -> None:
    for bad in ("", "   ", 5, None, []):
        out = _emit_owned({"url": "https://x/mcp", "clientId": bad})
        assert "oauth" not in out, bad
        assert "clientId" not in out, bad
        assert kiro_entry_client_id({"clientId": bad}) == "", bad


# ── Row 7: a second pass is a no-op (the render converges) ──


def test_row7_a_second_rebuild_is_a_no_op_for_every_row() -> None:
    """Emit is a fixed point: render again over the same store and nothing moves.

    A rule that is correct once but not stable would oscillate the emitted spec
    on every gateway start, which is how a wrong grant becomes intermittent
    rather than reproducible.
    """
    cases = [
        # (label, source store entry, managed)
        ("row1", {"url": "https://x/mcp", "scopes": ["read"], "clientId": "cid"}, True),
        ("row2", {"url": "https://x/mcp", "scopes": [], "clientId": ""}, True),
        ("row3", {"url": "https://x/mcp"}, True),
        ("row4", {"url": "https://x/mcp"}, False),
    ]
    for label, source, managed in cases:
        # First render starts from a previously-rendered entry carrying wire keys.
        rendered = {
            "url": "https://x/mcp",
            "oauthScopes": ["stale"],
            "oauth": {"clientId": "stale-id", "issuer": "https://i"},
        }
        first = _rendered_then_updated(rendered, source, managed=managed)
        second = _rendered_then_updated(first, source, managed=managed)
        assert first == second, label
        # And the issuer is still there in all four (row 5 under iteration).
        assert second["oauth"]["issuer"] == "https://i", label


def test_row7_readback_of_an_emitted_entry_matches_what_was_requested() -> None:
    """Convergence on the sync side: the readback of a render is a fixed point.

    ``discover_servers_to_sync`` compares the readback of the agent entry with
    the readback of mcp.json, so a render whose readback did not equal its own
    source would re-flag for sync on every single pass.
    """
    for scopes, client_id in ((["read"], "cid"), ([], ""), (["a", "b"], "x")):
        source = {"url": "https://x/mcp", "scopes": scopes, "clientId": client_id}
        emitted = _emit_owned(source)
        assert kiro_entry_scopes(emitted) == scopes
        assert kiro_entry_client_id(emitted) == client_id


# ── Row 8: an OWNED store entry states its hints in EITHER spelling ──
#
# The scope-toggle preservation rule copies a global server's spec into the store
# verbatim so toggling its globals off does not lose the config. That copy is a
# dict in the store -- so it is ours -- but it carries whatever spelling the
# global used, which for a hand-authored entry is the WIRE one. Reading only the
# internal spelling would see "no hints" on an owned entry and delete exactly the
# configuration the copy exists to preserve.


def test_row8_an_owned_store_entry_in_wire_form_keeps_its_hints() -> None:
    preserved_copy = {
        "url": "https://x/mcp",
        "oauthScopes": ["read:user"],
        "oauth": {"clientId": "hand-authored", "issuer": "https://i"},
    }
    out = _emit_owned(preserved_copy)
    assert out["oauthScopes"] == ["read:user"]
    assert out["oauth"] == {"clientId": "hand-authored", "issuer": "https://i"}


def test_row8_a_wire_form_store_entry_survives_the_rendered_merge_path() -> None:
    """The shape agent.py actually emits, with the copy as the store entry."""
    store = {
        "url": "https://x/mcp",
        "oauthScopes": ["read:user"],
        "oauth": {"clientId": "hand-authored"},
    }
    out = _rendered_then_updated(
        {"url": "https://x/mcp", "oauthScopes": ["stale"], "oauth": {"clientId": "stale-id"}},
        store,
    )
    assert out["oauthScopes"] == ["read:user"]
    assert out["oauth"] == {"clientId": "hand-authored"}


def test_row8_does_not_resurrect_row3_the_store_must_be_the_one_speaking() -> None:
    """A stale RENDER in wire form still loses to an owned store that says none.

    Row 8 reads wire spellings off the STORE only. Reading them off the merged
    entry would let the previous render outvote the store and undo row 3.
    """
    out = _rendered_then_updated(
        {"url": "https://x/mcp", "oauthScopes": ["stale"], "oauth": {"clientId": "stale-id"}},
        {"url": "https://x/mcp"},
    )
    assert "oauthScopes" not in out
    assert "oauth" not in out


def test_row8_an_internal_store_spelling_still_wins_over_a_wire_render() -> None:
    out = _rendered_then_updated(
        {"url": "https://x/mcp", "oauthScopes": ["stale"]},
        {"url": "https://x/mcp", "scopes": ["fresh"]},
    )
    assert out["oauthScopes"] == ["fresh"]
