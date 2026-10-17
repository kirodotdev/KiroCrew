"""Contract tests for Connections tool-name collision resolution: the resolver.

Every reachable row of the resolver's governing decision table (EXPOSURE x IDENTITY x
DECLARATION LIFECYCLE, see :mod:`kiro_crew.connections.tool_aliases`) has a named test
here, with the registry's own declarations, endpoint identity, determinism, the derived
alias name, the statically visible names a destination reservation reads, and the
registry's validation of ``tool_aliases``.

One topic of the ``test_connections_tool_aliases*.py`` family; the builders and autouse
fixtures the family shares come from ``connections_tool_aliases_helpers.py``.
"""

import json
from unittest.mock import patch

import pytest
from connections_tool_aliases_helpers import _isolated_alias_record  # noqa: F401
from connections_tool_aliases_helpers import _pinned_kiro_cli_version  # noqa: F401
from connections_tool_aliases_helpers import URLS

from kiro_crew.connections import RegistryValidationError, get_all_registry_providers
from kiro_crew.connections.registry import _load_registry
from kiro_crew.connections.tool_aliases import (
    declared_tool_aliases,
    derived_alias,
    exposed_declared_tools,
    exposed_server_keys,
    natural_tool_names,
    normalized_endpoint,
    resolve_tool_aliases,
    statically_visible_tool_names,
)


def _servers(*slugs: str) -> dict:
    return {slug: {"url": URLS[slug]} for slug in slugs}


def _aliases(servers: dict, tools: list) -> dict:
    return resolve_tool_aliases(exposed_declared_tools(servers, tools))


# ── the declarations themselves ──


def test_linear_and_vercel_both_declare_the_tools_they_share():
    """The launch-set collision this slice exists for: without declarations on
    BOTH sides, whichever mounts second wins and the other is unreachable."""
    declared = declared_tool_aliases()
    shared = set(declared["linear"]) & set(declared["vercel"])
    assert shared == {"list_projects", "get_project", "list_teams"}


def test_issue_tools_are_declared_across_every_issue_tracker():
    declared = declared_tool_aliases()
    for slug in ("linear", "github", "gitlab"):
        assert {"list_issues", "get_issue"} <= set(declared[slug]), slug


def test_every_server_with_a_bare_search_or_fetch_declares_the_rename():
    """``search`` and ``fetch`` are the tool pair ChatGPT's connector spec asks a
    server to ship, so several vendors use the bare names. A server that ships one
    must declare it, or whichever of them mounts second shadows the others."""
    declared = declared_tool_aliases()
    bare = {
        "atlassian": {"search", "fetch"},
        "gitlab": {"search"},
        "neon": {"search", "fetch"},
        "todoist": {"search", "fetch"},
    }
    for slug, tools in bare.items():
        assert tools <= set(declared[slug]), slug


def test_todoist_and_atlassian_mounted_together_rename_both_sides():
    assert _aliases(_servers("todoist", "atlassian"), ["@todoist", "@atlassian"]) == {
        "@atlassian/fetch": "atlassian_fetch",
        "@atlassian/search": "atlassian_search",
        "@todoist/fetch": "todoist_fetch",
        "@todoist/search": "todoist_search",
    }


def test_gitlab_and_todoist_share_only_search():
    """GitLab ships ``search`` but no ``fetch``, so Todoist's ``fetch`` keeps its
    name: a rename fires per colliding tool, not per pair of providers."""
    assert _aliases(_servers("todoist", "gitlab"), ["@todoist", "@gitlab"]) == {
        "@gitlab/search": "gitlab_search",
        "@todoist/search": "todoist_search",
    }


def test_todoist_alone_keeps_its_natural_tool_names():
    """Invariant 3: a declaration renames nothing without a real collision."""
    assert _aliases(_servers("todoist"), ["@todoist"]) == {}


def test_every_declared_alias_is_globally_unique():
    aliases = [alias for tools in declared_tool_aliases().values() for alias in tools.values()]
    assert len(aliases) == len(set(aliases))


def test_no_declared_alias_lands_on_a_declared_natural_tool_name():
    declared = declared_tool_aliases()
    naturals = {tool for tools in declared.values() for tool in tools}
    destinations = {alias for tools in declared.values() for alias in tools.values()}
    assert not naturals & destinations


def test_every_declared_alias_equals_its_derivation():
    """The emission pass recognises its own prior output by re-deriving this name;
    an alias that is not its own derivation would never be recognised."""
    for slug, tools in declared_tool_aliases().items():
        for tool, alias in tools.items():
            assert alias == f"{slug}_{tool}", f"{slug}/{tool} -> {alias}"


def test_providers_without_collisions_declare_nothing():
    assert not {"notion", "stripe"} & set(declared_tool_aliases())


# ── table rows 1-3: whole-server exposure, identity matched ──


def test_row1_whole_server_both_sides_aliases_the_shared_tools():
    aliases = _aliases(_servers("linear", "vercel"), ["@linear", "@vercel"])
    assert aliases["@linear/list_projects"] == "linear_list_projects"
    assert aliases["@vercel/list_projects"] == "vercel_list_projects"
    assert set(aliases) == {
        "@linear/get_project",
        "@linear/list_projects",
        "@linear/list_teams",
        "@vercel/get_project",
        "@vercel/list_projects",
        "@vercel/list_teams",
    }


def test_row1_whole_server_alone_keeps_natural_names():
    assert _aliases(_servers("linear"), ["@linear"]) == {}
    assert _aliases(_servers("vercel"), ["@vercel"]) == {}


def test_row1_whole_server_pair_with_no_shared_declaration_aliases_nothing():
    """Linear declares issue tools, Vercel declares none, so mounting the pair
    must not rename Linear's issue tools."""
    aliases = _aliases(_servers("linear", "vercel"), ["@linear", "@vercel"])
    assert "@linear/list_issues" not in aliases


def test_row2_a_renamed_declaration_replaces_the_old_alias():
    from kiro_crew.connections import tool_aliases as ta

    renamed = {
        "linear": {"list_projects": "linear_projects"},
        "vercel": {"list_projects": "vercel_projects"},
    }
    with patch.object(ta, "declared_tool_aliases", return_value=renamed):
        aliases = _aliases(_servers("linear", "vercel"), ["@linear", "@vercel"])

    assert aliases == {
        "@linear/list_projects": "linear_projects",
        "@vercel/list_projects": "vercel_projects",
    }


def test_row3_a_withdrawn_declaration_yields_no_alias():
    from kiro_crew.connections import tool_aliases as ta

    with patch.object(ta, "declared_tool_aliases", return_value={"vercel": {"x": "vercel_x"}}):
        assert _aliases(_servers("linear", "vercel"), ["@linear", "@vercel"]) == {}


# ── table rows 4-6: per-tool exposure ──


def test_row4_per_tool_refs_on_disjoint_tools_alias_nothing():
    """Provider-level eligibility renamed both sides here even though the EXPOSED
    tools cannot collide -- mounting one tool is not mounting the server."""
    aliases = _aliases(_servers("linear", "github"), ["@linear/list_issues", "@github/get_issue"])
    assert aliases == {}


def test_row4_per_tool_refs_on_the_same_tool_alias_both():
    aliases = _aliases(_servers("linear", "github"), ["@linear/list_issues", "@github/list_issues"])
    assert aliases == {
        "@github/list_issues": "github_list_issues",
        "@linear/list_issues": "linear_list_issues",
    }


def test_row4_whole_server_against_per_tool_aliases_only_the_overlap():
    aliases = _aliases(_servers("linear", "vercel"), ["@linear", "@vercel/list_projects"])
    assert set(aliases) == {"@linear/list_projects", "@vercel/list_projects"}


def test_row4_whole_server_against_a_non_overlapping_per_tool_aliases_nothing():
    assert _aliases(_servers("linear", "vercel"), ["@linear", "@vercel/get_deployment"]) == {}


def test_row5_a_renamed_declaration_applies_to_the_exposed_tool_only():
    from kiro_crew.connections import tool_aliases as ta

    renamed = {
        "linear": {"list_issues": "linear_issues", "get_issue": "linear_issue"},
        "github": {"list_issues": "github_issues", "get_issue": "github_issue"},
    }
    with patch.object(ta, "declared_tool_aliases", return_value=renamed):
        aliases = _aliases(
            _servers("linear", "github"), ["@linear/list_issues", "@github/list_issues"]
        )

    assert aliases == {
        "@github/list_issues": "github_issues",
        "@linear/list_issues": "linear_issues",
    }


def test_row6_a_ref_naming_an_undeclared_tool_exposes_nothing():
    exposed = exposed_declared_tools(_servers("linear", "vercel"), ["@linear/nope", "@vercel"])
    assert "linear" not in exposed
    assert _aliases(_servers("linear", "vercel"), ["@linear/nope", "@vercel"]) == {}


# ── table rows 7-12: wildcards ──


def test_row7_the_global_wildcard_exposes_every_declared_source():
    wildcard = _aliases(_servers("linear", "vercel"), ["*"])
    whole = _aliases(_servers("linear", "vercel"), ["@linear", "@vercel"])
    assert wildcard == whole


def test_row10_a_per_server_wildcard_reads_as_whole_server():
    """Over-reading renames an unmounted tool, which is inert; under-reading
    leaves a real collision shadowed."""
    starred = _aliases(_servers("linear", "vercel"), ["@linear/*", "@vercel/*"])
    whole = _aliases(_servers("linear", "vercel"), ["@linear", "@vercel"])
    assert starred == whole


def test_a_whole_server_ref_wins_over_a_sibling_per_tool_ref():
    exposed = exposed_declared_tools(
        _servers("linear"), ["@linear/list_issues", "@linear", "@linear/get_issue"]
    )
    assert exposed["linear"] == frozenset(declared_tool_aliases()["linear"])


# ── table rows 13-18: not mounted ──


def test_row13_an_allowed_tools_only_ref_exposes_nothing():
    """``allowedTools`` auto-approves; ``tools`` is the closed allowlist that
    MOUNTS, so a ref absent from it cannot collide with anything."""
    servers = _servers("linear", "vercel")
    assert exposed_declared_tools(servers, []) == {}
    assert _aliases(servers, []) == {}


def test_row16_an_absent_provider_exposes_nothing():
    assert _aliases(_servers("linear", "vercel"), ["@linear"]) == {}


def test_builtin_entries_are_not_server_refs():
    assert exposed_server_keys(["fs_read", "code", "@linear"]) == {"linear"}
    assert exposed_server_keys(["*", "@linear"]) is None


# ── table rows 19-54: identity ──


def test_a_url_mismatched_server_gets_no_aliases():
    """Rows 19-36 collapse: identity gates before exposure, so a server that is
    not the provider has no claim on its declarations whatever ``tools`` says."""
    servers = {"linear": {"url": "https://evil.example.com/mcp"}, "vercel": {"url": URLS["vercel"]}}
    assert set(exposed_declared_tools(servers, ["@linear", "@vercel"])) == {"vercel"}
    assert _aliases(servers, ["@linear", "@vercel"]) == {}


def test_a_custom_server_carrying_a_registry_name_gets_no_aliases():
    """Rows 37-54. A registry slug is not proof of identity -- anyone can add a
    server named ``linear``."""
    servers = {"linear": {"command": "npx", "args": ["x"]}, "vercel": {"url": URLS["vercel"]}}
    assert set(exposed_declared_tools(servers, ["@linear", "@vercel"])) == {"vercel"}


def test_endpoint_matching_tolerates_a_trailing_slash_and_case():
    servers = {
        "github": {"url": "https://API.GithubCopilot.com/mcp"},
        "gitlab": {"url": URLS["gitlab"]},
    }
    assert set(exposed_declared_tools(servers, ["@github", "@gitlab"])) == {"github", "gitlab"}


def test_endpoint_matching_rejects_a_different_path_on_the_right_host():
    """Linear ships a read-only endpoint AND a read-write one; only the pinned one
    carries the tool set the declarations describe."""
    servers = {"linear": {"url": "https://mcp.linear.app/mcp"}, "vercel": {"url": URLS["vercel"]}}
    assert set(exposed_declared_tools(servers, ["@linear", "@vercel"])) == {"vercel"}


@pytest.mark.parametrize(
    "written,canonical",
    [
        ("https://mcp.linear.app:443/mcp/readonly", "https://mcp.linear.app/mcp/readonly"),
        ("http://example.test:80/mcp", "http://example.test/mcp"),
        ("HTTPS://MCP.Linear.App:443/mcp/readonly", "https://mcp.linear.app/mcp/readonly"),
    ],
)
def test_an_explicit_scheme_default_port_names_the_same_endpoint(written, canonical):
    """An explicitly written default port must not fail the identity check: the
    provider would be treated as unverified and get NO aliases, leaving exactly the
    shadowing this pass exists to remove."""
    assert normalized_endpoint(written) == normalized_endpoint(canonical)


@pytest.mark.parametrize(
    "written", ["https://mcp.linear.app:8443/mcp/readonly", "http://example.test:8080/mcp"]
)
def test_a_non_default_port_stays_significant(written):
    """A non-default port can select a different server, so it is part of identity."""
    assert normalized_endpoint(written) != normalized_endpoint(
        written.replace(":8443", "").replace(":8080", "")
    )


def test_a_provider_mounted_on_an_explicit_default_port_still_gets_aliases():
    """The consequence, end to end: the collision is resolved rather than silently
    left in place because the URL spelled the port out."""
    servers = {
        "linear": {"url": "https://mcp.linear.app:443/mcp/readonly"},
        "vercel": {"url": URLS["vercel"]},
    }
    assert set(exposed_declared_tools(servers, ["@linear", "@vercel"])) == {"linear", "vercel"}


def test_a_disabled_tool_does_not_force_an_alias_on_the_provider_that_keeps_it():
    """A tool a server DISABLES is not callable, so it cannot collide.

    Counting it renames the only reachable holder of that name for nothing: with
    Linear and GitHub both mounted whole and GitHub's ``list_issues`` disabled,
    Linear's ``list_issues`` is unambiguous and must keep its natural name.
    """
    servers = {
        "linear": {"url": URLS["linear"]},
        "github": {"url": URLS["github"], "disabledTools": ["list_issues", "get_issue"]},
    }
    exposed = exposed_declared_tools(servers, ["@linear", "@github"])

    assert "list_issues" not in exposed.get("github", frozenset())
    assert "list_issues" in exposed["linear"]
    assert "@linear/list_issues" not in resolve_tool_aliases(exposed)


def test_a_disabled_tool_still_leaves_a_real_collision_aliased():
    """The guard subtracts only what is disabled: a name both providers still expose
    collides exactly as before."""
    servers = {
        "linear": {"url": URLS["linear"]},
        "github": {"url": URLS["github"], "disabledTools": ["get_issue"]},
    }
    exposed = exposed_declared_tools(servers, ["@linear", "@github"])

    assert "list_issues" in exposed["github"] and "list_issues" in exposed["linear"]
    aliases = resolve_tool_aliases(exposed)
    assert aliases["@linear/list_issues"] == "linear_list_issues"
    assert aliases["@github/list_issues"] == "github_list_issues"


@pytest.mark.parametrize("disabled", ["list_issues", {"list_issues": True}, 42, None])
def test_a_non_list_disabled_tools_value_is_ignored(disabled):
    """A hand-edited non-list value must not silently empty the exposed set."""
    servers = {
        "linear": {"url": URLS["linear"]},
        "github": {"url": URLS["github"], "disabledTools": disabled},
    }
    exposed = exposed_declared_tools(servers, ["@linear", "@github"])

    assert "list_issues" in exposed["github"]


def test_a_provider_whose_every_declared_tool_is_disabled_is_omitted():
    """With nothing callable left it can neither collide nor be collided with."""
    servers = {
        "linear": {"url": URLS["linear"]},
        "vercel": {
            "url": URLS["vercel"],
            "disabledTools": ["list_projects", "get_project", "list_teams"],
        },
    }
    exposed = exposed_declared_tools(servers, ["@linear", "@vercel"])

    assert "vercel" not in exposed
    assert resolve_tool_aliases(exposed) == {}


@pytest.mark.parametrize("bad", [None, "", "   ", 42, "not-a-url", "://broken", ["u"]])
def test_unusable_endpoints_normalize_to_none(bad):
    assert normalized_endpoint(bad) is None


def test_a_non_dict_server_entry_is_not_eligible():
    servers = {"linear": "nope", "vercel": {"url": URLS["vercel"]}}
    assert set(exposed_declared_tools(servers, ["*"])) == {"vercel"}


# ── determinism ──


def test_resolution_is_independent_of_order_and_is_sorted():
    forward = _aliases(_servers("linear", "vercel", "github"), ["@linear", "@vercel", "@github"])
    reverse = _aliases(_servers("github", "vercel", "linear"), ["@github", "@vercel", "@linear"])
    assert forward == reverse
    assert list(forward) == sorted(forward)


def test_three_way_collision_aliases_every_claimant():
    aliases = _aliases(_servers("linear", "github", "gitlab"), ["@linear", "@github", "@gitlab"])
    assert {aliases[f"@{slug}/list_issues"] for slug in ("linear", "github", "gitlab")} == {
        "linear_list_issues",
        "github_list_issues",
        "gitlab_list_issues",
    }


def test_a_duplicated_slug_is_one_provider_not_a_collision():
    assert resolve_tool_aliases({"linear": ["list_projects", "list_projects"]}) == {}


def test_natural_tool_names_are_the_exposed_pre_alias_names():
    assert natural_tool_names({"vercel": {"list_projects"}}) == {"list_projects"}
    assert natural_tool_names({}) == set()


# ── the generated-shape domain (name generation only) ──


@pytest.mark.parametrize(
    "slug,tool,expected",
    [
        ("linear", "list_projects", "linear_list_projects"),
        ("vercel", "get_project", "vercel_get_project"),
    ],
)
def test_the_derived_name_pins_slug_and_tool(slug, tool, expected):
    assert derived_alias(slug, tool) == expected


def test_the_derived_name_is_the_one_the_registry_declares():
    """``derived_alias`` is the NAME GENERATOR: the validator pins every
    declaration to it, so a reader sees the exact name that will be emitted. It is
    not an ownership test -- see the record tests below."""
    for slug, tools in declared_tool_aliases().items():
        for tool, alias in tools.items():
            assert alias == derived_alias(slug, tool)


# ── statically visible names (destination reservation input) ──


def test_per_tool_refs_of_any_server_are_statically_visible():
    visible = statically_visible_tool_names(
        ["fs_read", "@mycustom/linear_list_projects", "@linear", "@vercel/list_projects"]
    )
    assert visible == {"linear_list_projects", "list_projects"}


def test_a_whole_server_mount_publishes_no_statically_visible_names():
    """The OUT OF SCOPE row: a custom server mounted whole names its tools only
    at runtime, so nothing static can see them."""
    assert statically_visible_tool_names(["@mycustom", "@mycustom/*", "*"]) == set()


def test_a_per_tool_ref_survives_a_whole_server_ref_on_the_same_server():
    """Reservation is precedence-INDEPENDENT. Exposure lets ``@mycustom``
    supersede ``@mycustom/x`` because the wider ref already mounts it, but the
    explicit name is still occupied and a generated alias must not land on it."""
    visible = statically_visible_tool_names(
        ["@linear", "@vercel", "@mycustom", "@mycustom/vercel_list_projects"]
    )
    assert "vercel_list_projects" in visible


def test_reservation_does_not_change_exposure_precedence():
    """The companion half: exposure keeps whole-server-wins, so the narrower ref
    does not shrink what a provider exposes."""
    exposed = exposed_declared_tools(_servers("linear"), ["@linear", "@linear/list_issues"])
    assert exposed["linear"] == frozenset(declared_tool_aliases()["linear"])


# ── registry validation ──


def _registry(tmp_path, mutate):
    payload = get_all_registry_providers()
    mutate(payload)
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"list_issues": ""},
        {"list_issues": "has space"},
        {"list_issues": "has/slash"},
        {"list_issues": "@server"},
        {"list_issues": 42},
        {"": "alias_name"},
        {" list_issues": "alias_name"},
        {"has space": "alias_name"},
        {"has/slash": "alias_name"},
        {"@server/tool": "alias_name"},
        ["list_issues"],
        "list_issues",
    ],
)
def test_malformed_tool_aliases_are_rejected_at_load(tmp_path, bad):
    """A bad alias reaches the emitted agent spec, where kiro-cli rejects the
    WHOLE spec and the agent loses every tool. A malformed KEY is worse than
    loud: it silently never matches a real tool."""
    path = _registry(tmp_path, lambda p: p[0].update(tool_aliases=bad))
    with pytest.raises(RegistryValidationError, match="tool_aliases"):
        _load_registry(path)


@pytest.mark.parametrize(
    "alias",
    ["list_issues", "shared_name", "linearlist", "linea_list", "linear_issues", "linear_"],
)
def test_an_alias_that_is_not_its_own_derivation_is_rejected(tmp_path, alias):
    """Ownership is decided by re-deriving ``<slug>_<tool>`` from the ref, so an
    alias that is not that name would be emitted and then never recognised as
    ours -- it would outlive its own declaration. ``linear_issues`` is in the list
    because it is exactly the prefix-shaped name a user might hand-write, and the
    registry must not be able to mint one that collides with that space."""

    def mutate(payload):
        linear = next(p for p in payload if p["slug"] == "linear")
        linear["tool_aliases"] = {"list_issues": alias}

    with pytest.raises(RegistryValidationError, match="must be exactly"):
        _load_registry(_registry(tmp_path, mutate))


def test_the_derived_alias_is_accepted(tmp_path):
    def mutate(payload):
        linear = next(p for p in payload if p["slug"] == "linear")
        linear["tool_aliases"] = {"list_issues": "linear_list_issues"}

    loaded = {p["slug"]: p for p in _load_registry(_registry(tmp_path, mutate))}
    assert loaded["linear"]["tool_aliases"] == {"list_issues": "linear_list_issues"}


def test_two_tools_cannot_share_one_alias(tmp_path):
    """Derivation subsumes the uniqueness check: an alias shared by two tools is
    not its own derivation for at least one of them, so it is rejected."""

    def mutate(payload):
        linear = next(p for p in payload if p["slug"] == "linear")
        linear["tool_aliases"] = {"list_issues": "linear_same", "get_issue": "linear_same"}

    with pytest.raises(RegistryValidationError, match="must be exactly"):
        _load_registry(_registry(tmp_path, mutate))


def test_tool_aliases_does_not_widen_the_schema(tmp_path):
    path = _registry(tmp_path, lambda p: p[0].update(tool_nicknames={"list_issues": "x"}))
    with pytest.raises(RegistryValidationError, match="unknown fields: tool_nicknames"):
        _load_registry(path)


def test_a_malformed_slug_is_still_rejected(tmp_path):
    """The slug check moved ahead of the alias block that depends on it."""
    path = _registry(tmp_path, lambda p: p[0].update(slug="Bad_Slug"))
    with pytest.raises(RegistryValidationError, match="slug must contain"):
        _load_registry(path)
