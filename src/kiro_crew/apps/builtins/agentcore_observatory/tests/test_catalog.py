"""Catalog integrity: the table is the single source of truth for 27 types.

A wrong cell here makes one whole resource type silently return an empty list,
which the UI would render as "None in this region" — the exact error-vs-empty
confusion the app exists to prevent. These assertions pin the facts that were
read out of `aws bedrock-agentcore-control <verb> help`.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.builtins.agentcore_observatory.backend import catalog

#: Counts established from the live CLI: 28 list verbs minus
#: `list-tags-for-resource` (a tag lookup, not a resource type) = 27 types, of
#: which 10 require a parent identifier. `token-vault` is a 28th ROW but has no
#: list verb, so it is a singleton rather than one of the 27 listable types.
_LISTABLE = 27
_CHILDREN = 10
_ROOT_LISTABLE = 17


def test_ids_are_unique() -> None:
    ids = [t.id for t in catalog.RESOURCE_TYPES]
    assert len(ids) == len(set(ids))


def test_listable_and_child_counts() -> None:
    listable = [t for t in catalog.RESOURCE_TYPES if t.listable]
    children = [t for t in catalog.RESOURCE_TYPES if t.parent]
    roots = [t for t in listable if t.is_root]
    assert len(listable) == _LISTABLE
    assert len(children) == _CHILDREN
    assert len(roots) == _ROOT_LISTABLE


def test_token_vault_is_a_get_only_singleton() -> None:
    """`get-token-vault` takes no arguments and there is no list operation."""
    vault = catalog.by_id("token-vault")
    assert vault is not None
    assert vault.listable is False
    assert vault.get_verb == "get-token-vault"
    assert vault.is_root is True


def test_every_group_is_declared() -> None:
    for rt in catalog.RESOURCE_TYPES:
        assert rt.group in catalog.GROUPS, rt.id


def test_every_group_has_at_least_one_root() -> None:
    """A rail group with no root type would render as an empty heading."""
    for group in catalog.GROUPS:
        roots = [t for t in catalog.root_types() if t.group == group]
        assert roots, group


def test_listable_types_declare_a_response_key() -> None:
    for rt in catalog.RESOURCE_TYPES:
        if rt.listable:
            assert rt.list_key, rt.id


def test_shared_response_keys_are_intentional() -> None:
    """The keys that collide are exactly the ones the API really shares.

    This is the assertion that documents WHY the table cannot be generated from
    type names: three credential families answer under `credentialProviders`,
    and gateways/gateway-targets both answer under `items`.
    """
    by_key: dict[str, set[str]] = {}
    for rt in catalog.RESOURCE_TYPES:
        if rt.listable:
            by_key.setdefault(rt.list_key, set()).add(rt.id)
    assert by_key["credentialProviders"] == {
        "api-key-credential-providers",
        "oauth2-credential-providers",
        "payment-credential-providers",
    }
    assert by_key["items"] == {"gateways", "gateway-targets"}
    assert by_key["agentRuntimes"] == {"agent-runtimes", "agent-runtime-versions"}


def test_child_parent_wiring_is_complete_and_aligned() -> None:
    for rt in catalog.RESOURCE_TYPES:
        if not rt.parent:
            assert not rt.parent_params, rt.id
            assert not rt.parent_fields, rt.id
            continue
        assert catalog.by_id(rt.parent) is not None, rt.id
        assert rt.parent_params, rt.id
        # Positional pairing: each flag must have exactly one supplying field.
        assert len(rt.parent_params) == len(rt.parent_fields), rt.id
        for param in rt.parent_params:
            assert param.startswith("--"), (rt.id, param)


def test_policy_generation_assets_is_the_two_parent_case() -> None:
    """The one type needing two ancestors — the reason the fields are tuples."""
    assets = catalog.by_id("policy-generation-assets")
    assert assets is not None
    assert assets.parent_params == ("--policy-generation-id", "--policy-engine-id")
    assert assets.parent_fields == ("policyGenerationId", "policyEngineId")
    assert assets.parent == "policy-generations"


def test_parent_supplies_every_field_its_children_ask_for() -> None:
    """A child's parent_fields must be produced by an ancestor, not invented.

    Walks up the chain, because `policy-generation-assets` draws one field from
    its parent and one from its grandparent.
    """
    for rt in catalog.RESOURCE_TYPES:
        if not rt.parent:
            continue
        available: set[str] = set()
        node = catalog.by_id(rt.parent)
        while node is not None:
            if node.id_field:
                available.add(node.id_field)
            node = catalog.by_id(node.parent) if node.parent else None
        for wanted in rt.parent_fields:
            assert wanted in available, (rt.id, wanted, sorted(available))


def test_verbs_are_read_only() -> None:
    """No mutating verb can enter the table."""
    for rt in catalog.RESOURCE_TYPES:
        for verb in (rt.list_verb, rt.get_verb):
            if verb:
                assert verb.startswith(("list-", "get-")), (rt.id, verb)


@pytest.mark.parametrize(
    ("type_id", "list_verb", "list_key"),
    [
        ("agent-runtimes", "list-agent-runtimes", "agentRuntimes"),
        ("agent-runtime-endpoints", "list-agent-runtime-endpoints", "runtimeEndpoints"),
        ("browsers", "list-browsers", "browserSummaries"),
        ("browser-profiles", "list-browser-profiles", "profileSummaries"),
        ("code-interpreters", "list-code-interpreters", "codeInterpreterSummaries"),
        ("configuration-bundles", "list-configuration-bundles", "bundles"),
        ("configuration-bundle-versions", "list-configuration-bundle-versions", "versions"),
        ("gateway-rules", "list-gateway-rules", "gatewayRules"),
        ("registry-records", "list-registry-records", "registryRecords"),
        ("policy-generation-assets", "list-policy-generation-assets", "policyGenerationAssets"),
    ],
)
def test_spot_checked_rows_match_the_cli(type_id: str, list_verb: str, list_key: str) -> None:
    """Rows whose key is NOT guessable from the type name, pinned individually."""
    rt = catalog.by_id(type_id)
    assert rt is not None
    assert (rt.list_verb, rt.list_key) == (list_verb, list_key)


def test_lookup_helpers() -> None:
    assert catalog.by_id("nope") is None
    kids = {t.id for t in catalog.children_of("agent-runtimes")}
    assert kids == {"agent-runtime-versions", "agent-runtime-endpoints"}
    assert catalog.children_of("memories") == ()
