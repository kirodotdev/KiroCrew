"""A KAS spec's plain-URL ``toolsSettings.web_fetch.trusted`` entry runs a fetch
without a prompt, as kiro-cli runs it, where KAS carries no allow for it.

kiro-cli anchors each entry and matches it against the whole URL: a blocked match
refuses, then a trusted match runs without a prompt (``evaluate_url_permission``).
Crew's gate grants only a trusted entry that is a plain URL or a plain URL plus
``.*``, read with string work (no regex), and only when every blocked entry has
that shape and none matches. It adds no refusal: KAS's host deny stays as on main,
and anything else keeps main's prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_permissions import CREW_SPEC_DENIES_KEY
from kiro_crew.acp.kas_spec_denies import projected_spec_denies
from kiro_crew.acp.types import AcpEvent
from kiro_crew.hooks import (
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    HookManager,
    HooksConfig,
    hook_gate_kwargs,
)
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.spec_denied_gate import canonical_url, url_literal


@pytest.fixture(autouse=True)
def _no_profiles(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _project(**spec: Any) -> tuple[dict[str, Any], Any]:
    out = kas_agents.to_client_custom_agent("a", {"name": "a", "prompt": "p", **spec}, "p")
    return out, projected_spec_denies([out], "a", "")


def _fetch(url: object) -> AcpEvent:
    return _kas_builtin_flow(
        "web_fetch", title="Fetching web content", kind="fetch", raw_input={"url": url}
    )


def _judge(url: object, grant: Any) -> str:
    event = _fetch(url)
    event.spec_denied_grant = grant
    return HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event)).action


_TRUSTED = ["https://docs\\.example\\.com/.*"]


# ── the reader ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("https://docs\\.example\\.com/.*", ("https://docs.example.com/", True)),
        ("^https://x\\.com/a$", ("https://x.com/a", False)),
        ("\\Ahttps://x\\.com/a\\z", ("https://x.com/a", False)),
        ("https://x\\.com:8443/.*", ("https://x.com:8443/", True)),
        (".*", ("", True)),
        # Regex syntax beyond a final `.*` is not read.
        ("https://x\\.com/\\d+", None),
        ("https://a\\.com/.*|https://b\\.com/.*", None),
        ("https://x.com/.*", None),
        ("(?i)https://x\\.com/.*", None),
        ("https://x\\.com/.*/a", None),
        # Rust word boundaries, and a trailing `\$` kiro-cli leaves unanchored.
        ("https://x\\.com/\\<a\\>", None),
        ("https://x\\.com/a\\$", None),
        ("", None),
    ],
)
def test_url_literal_reads_only_plain_urls(entry: str, expected: object) -> None:
    assert url_literal(entry) == expected


# ── the gate ──────────────────────────────────────────────────────────────────


def test_trusted_runs_unprompted_unless_blocked() -> None:
    _out, grant = _project(
        toolsSettings={
            "web_fetch": {
                "blocked": ["https://docs\\.example\\.com/private/.*"],
                "trusted": _TRUSTED,
            }
        }
    )
    assert _judge("https://docs.example.com/guide", grant) == TOOL_AUTO_APPROVE
    # Matched case-sensitively and whole, as kiro-cli anchors it.
    assert _judge("https://DOCS.example.com/guide", grant) == TOOL_ALLOW
    assert _judge("https://evil.com/https://docs.example.com/", grant) == TOOL_ALLOW
    # A blocked match withholds the grant: main's prompt, not a refusal.
    assert _judge("https://docs.example.com/private/x", grant) == TOOL_ALLOW
    assert _judge("https://docs.example.com/\n", grant) == TOOL_ALLOW


@pytest.mark.parametrize(
    "url",
    [
        # Spellings a fetch resolves under the blocked prefix.
        "https://docs.example.com/%70rivate/x",
        "https://docs.example.com/%2e/private/x",
        "https://docs.example.com/guide/../private/x",
        "https://docs.example.com//private/x",
        "https://docs.example.com:443/private/x",
        # A blocked entry is read in its canonical form too.
        "https://docs.example.com/public/x",
        "https://docs.example.com/public/x#a",
        # Spellings that leave the trusted host, or that no fetch reads one way.
        "https://docs.example.com@evil.com/",
        "https://docs.example.com\\@evil.com/",
        "https://docs.example.com:99999/",
        "https://docs.example.com/%zz",
        "https://docs.example.com/a%20b",
    ],
)
def test_a_spelling_with_another_canonical_form_is_not_granted(url: str) -> None:
    _out, grant = _project(
        toolsSettings={
            "web_fetch": {
                "blocked": [
                    "https://docs\\.example\\.com/private/.*",
                    "https://DOCS\\.example\\.com/public/x",
                ],
                "trusted": _TRUSTED,
            }
        }
    )
    assert _judge("https://docs.example.com/guide", grant) == TOOL_AUTO_APPROVE
    assert _judge(url, grant) == TOOL_ALLOW


@pytest.mark.parametrize(
    ("text", "prefix", "expected"),
    [
        (
            "HTTPS://Docs.Example.COM.:443/%7euser/./a/../b",
            False,
            "https://docs.example.com/~user/b",
        ),
        ("https://x.com", False, "https://x.com/"),
        ("https://x.com?q", False, "https://x.com/?q"),
        ("https://x.com/%2f", False, "https://x.com/%2F"),
        ("https://x.com", True, "https://x.com"),
        ("https://x.com:8443/a/", True, "https://x.com:8443/a/"),
        ("https://x.com/a/..", True, None),
        ("https://x.com/%7", True, None),
        ("https://u@x.com/", False, None),
        ("http://2130706433/", False, None),
        ("http://0x7f.0.0.1/", False, None),
        ("http://127.0.0.1/", False, "http://127.0.0.1/"),
        ("http://127.1", True, None),
        ("http://0x7f.0.0.1", True, None),
        ("http://10.0.0.1", True, "http://10.0.0.1"),
        ("http://[::1]/", False, None),
        ("http://[::1]/", True, "http://[::1]/"),
        ("https://exa\u0301mple.com/", False, None),
        ("ftp://x.com/", False, None),
    ],
)
def test_canonical_url(text: str, prefix: bool, expected: str | None) -> None:
    assert canonical_url(text, prefix=prefix) == expected


def test_entries_are_read_in_canonical_form_too() -> None:
    # Another scheme's blocked entry withholds nothing; a trusted prefix with an
    # upper-case host or default port still grants its own URLs.
    _out, grant = _project(
        toolsSettings={
            "web_fetch": {
                "blocked": ["ftp://.*"],
                "trusted": ["https://Docs\\.Example\\.com:443/.*"],
            }
        }
    )
    assert _judge("https://Docs.Example.com:443/x", grant) == TOOL_AUTO_APPROVE
    # A blocked numeric host in another spelling withholds the grant.
    _out, grant = _project(
        toolsSettings={"web_fetch": {"blocked": ["http://0x7f\\.0\\.0\\.1/.*"], "trusted": [".*"]}}
    )
    assert _judge("http://127.0.0.1/x", grant) == TOOL_ALLOW


def test_gate_adds_no_refusal() -> None:
    _out, grant = _project(toolsSettings={"web_fetch": {"blocked": ["https://evil\\.com/.*"]}})
    # No trusted list: nothing to grant, so no record and main's behaviour.
    assert grant is None
    assert _judge("https://evil.com/x", grant) == TOOL_ALLOW


def test_kas_policy_is_left_as_on_main() -> None:
    out, _grant = _project(
        allowedTools=["web_fetch"],
        toolsSettings={"web_fetch": {"blocked": [".*bad\\.com.*"], "trusted": _TRUSTED}},
    )
    rules = out["permissions"]["rules"]
    assert {"capability": "web_fetch", "effect": "allow"} in rules
    assert {"capability": "web_fetch", "match": ["*bad.com*"], "effect": "deny"} in rules
    assert not any(r.get("effect") == "ask" for r in rules)
    assert "web_fetch" not in out.get(CREW_SPEC_DENIES_KEY, {}).get("builtins", [])


@pytest.mark.parametrize(
    "settings",
    [
        # A blocked entry the reader does not take: no grant past it.
        {"blocked": ["https://x\\.com/\\d+"], "trusted": _TRUSTED},
        # A trusted entry the reader does not take grants nothing.
        {"trusted": ["https://docs\\.example\\.com/\\w+"]},
    ],
)
def test_an_entry_it_cannot_read_keeps_mains_prompt(settings: dict[str, Any]) -> None:
    _out, grant = _project(toolsSettings={"web_fetch": settings})
    assert _judge("https://docs.example.com/guide", grant) == TOOL_ALLOW


@pytest.mark.parametrize(
    "permissions",
    [
        {"rules": [{"capability": "web_fetch", "match": ["*.example.com"], "effect": "ask"}]},
        {"rules": [{"capability": "builtin", "effect": "ask"}]},
        {"rules": [{"capability": "web_fetch", "match": ["evil.com"], "effect": "deny"}]},
        # A deny the merge refuses whole (past the 64-pattern bound) still counts.
        {
            "rules": [
                {
                    "capability": "web_fetch",
                    "match": ["*"],
                    "exclude": [f"h{i}.example" for i in range(65)],
                    "effect": "deny",
                }
            ]
        },
    ],
)
def test_an_authored_restriction_keeps_the_grant_off(permissions: dict[str, Any]) -> None:
    _out, grant = _project(
        permissions=permissions, toolsSettings={"web_fetch": {"trusted": _TRUSTED}}
    )
    assert _judge("https://docs.example.com/guide", grant) == TOOL_ALLOW


def test_the_notice_drops_only_a_trusted_list_the_gate_applies() -> None:
    def lost(settings: dict[str, Any]) -> list[str]:
        return kas_agents.spec_keys_without_carrier(
            {"toolsSettings": settings}, translates_tools_settings=True
        )

    assert lost({"web_fetch": {"blocked": ["https://evil\\.com/.*"], "trusted": _TRUSTED}}) == [
        f"toolsSettings.web_fetch.blocked: https://evil\\.com/.* {_HOST_ONLY}"
    ]
    assert lost({"webFetch": {"trusted": [*_TRUSTED, "https://x\\.com/\\d+"]}}) == [
        "toolsSettings.webFetch.trusted"
    ]
    assert lost({"web_fetch": {"blocked": ["(?i)x"], "trusted": _TRUSTED}}) == [
        f"toolsSettings.web_fetch.blocked: (?i)x {_REGEX_SYNTAX}",
        "toolsSettings.web_fetch.trusted",
    ]


def test_a_trusted_list_past_the_bound_names_only_the_entries_cut() -> None:
    """The gate grants the first 64 trusted entries, so the notice names each later
    one by position and text and says the first 64 apply, never the key whole."""
    from kiro_crew.acp.kas_spec_denies import web_fetch_gate
    from kiro_crew.agent_sdk.spec_hooks import spec_keys_notice

    hosts = [f"https://h{i}\\.example\\.com/x" for i in range(76)]

    def lost(trusted: list[str], key: str = "web_fetch") -> list[str]:
        return kas_agents.spec_keys_without_carrier(
            {"toolsSettings": {key: {"trusted": trusted}}}, translates_tools_settings=True
        )

    assert len(web_fetch_gate({"web_fetch": {"trusted": hosts[:65]}})["webFetchTrusted"]) == 64
    assert lost(hosts[:65]) == [
        f"toolsSettings.web_fetch.trusted past the limit: entry 65: {hosts[64]}"
    ]
    many = lost([*hosts, hosts[0]], key="webFetch")
    assert many == [
        "toolsSettings.webFetch.trusted past the limit: "
        + "; ".join(f"entry {i + 1}: {hosts[i]}" for i in range(64, 74))
        + "; and 2 more after entry 74"
    ]
    notice = spec_keys_notice("a", lost(hosts[:65]))
    assert "no effect" not in notice
    assert f"Its first 64 entries apply. These do not: entry 65: {hosts[64]}." in notice
    # An entry the gate cannot read still names the key whole.
    assert lost([*hosts[:65], "https://x\\.com/\\d+"]) == ["toolsSettings.web_fetch.trusted"]


_REFUSED = "; every web fetch is refused until this entry is fixed"
_HOST_ONLY = "(whole-URL rule; KAS checks host only)"
_REGEX_SYNTAX = "(regex syntax KAS cannot express)"


def test_opted_in_the_notice_says_every_fetch_is_refused(monkeypatch) -> None:
    """``agent.kas_fail_closed_web_fetch_blocked`` on: each uncarried entry says so."""
    from kiro_crew.acp import kas_permissions, kas_unread_commands

    monkeypatch.setattr(kas_permissions, "fails_closed_on_uncarried_blocked", lambda: True)
    monkeypatch.setattr(kas_unread_commands, "fails_closed_on_uncarried_blocked", lambda: True)
    lost = kas_agents.spec_keys_without_carrier(
        {"toolsSettings": {"web_fetch": {"blocked": ["https://evil\\.com/.*"]}}},
        translates_tools_settings=True,
    )
    assert lost == [
        f"toolsSettings.web_fetch.blocked: https://evil\\.com/.* {_HOST_ONLY}{_REFUSED}"
    ]


def test_the_notice_names_each_blocked_entry_kas_cannot_carry() -> None:
    """Each dropped ``blocked`` entry is named on its own, under the key the spec
    wrote, with the reason it is dropped; one KAS carries as a host deny is not
    named."""
    long_host = "a" * 1025
    blocked = [
        "evil\\.com",
        "evil\\.(com|net)",
        long_host,
        "https://a\\.com/x",
        "https://b\\.com/.*",
        "x\x7f/",
    ]
    lost = kas_agents.spec_keys_without_carrier(
        {"toolsSettings": {"webFetch": {"blocked": blocked}}},
        translates_tools_settings=True,
    )
    assert lost == [
        f'toolsSettings.webFetch.blocked: "x\\u007f/" {_HOST_ONLY}',
        f"toolsSettings.webFetch.blocked: {long_host} (longer than 1024 characters)",
        f"toolsSettings.webFetch.blocked: evil\\.(com|net) {_REGEX_SYNTAX}",
        f"toolsSettings.webFetch.blocked: https://a\\.com/x {_HOST_ONLY}",
        f"toolsSettings.webFetch.blocked: https://b\\.com/.* {_HOST_ONLY}",
    ]
    carried = {"toolsSettings": {"web_fetch": {"blocked": ["evil\\.com"]}}}
    assert kas_agents.spec_keys_without_carrier(carried, translates_tools_settings=True) == []


def test_another_tool_is_never_granted_by_the_web_fetch_record() -> None:
    _out, grant = _project(toolsSettings={"web_fetch": {"trusted": [".*"]}})
    event = _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": "make build"}
    )
    event.spec_denied_grant = grant
    action = HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event)).action
    assert action != TOOL_AUTO_APPROVE


# ── trusted URLs a canonical form must not lose ──────────────────────────────


_PRIVATE_BLOCKED = ["https://docs\\.example\\.com/private/.*"]


@pytest.mark.parametrize(
    "url",
    [
        # An escape in the query is the normal shape of a search or API URL.
        "https://docs.example.com/search?q=a%20b",
        "https://docs.example.com/api?filter=name%3Dfoo",
        # An empty path segment no blocked entry reaches either way.
        "https://docs.example.com/web/2020/https://x.com/",
    ],
)
def test_a_trusted_url_with_a_query_escape_or_empty_segment_is_granted(
    url: str,
) -> None:
    _out, grant = _project(
        toolsSettings={"web_fetch": {"blocked": _PRIVATE_BLOCKED, "trusted": _TRUSTED}}
    )
    assert _judge(url, grant) == TOOL_AUTO_APPROVE


@pytest.mark.parametrize(
    "url",
    [
        "https://docs.example.com/%70rivate/x",
        "https://docs.example.com/%2e/private/x",
        "https://docs.example.com//private/x",
        # Collapsed before its dot segment, `//..` climbs out of `public`.
        "https://docs.example.com/public//../private/x",
        # Dot segments removed first keep `//private/x`, which a server collapses.
        "https://docs.example.com//private//../x",
        "https://docs.example.com///private//..",
        # Any escape in the path, and non-ASCII anywhere, written or escaped
        # (it has Unicode-equivalent spellings): no grant, as on main.
        "https://docs.example.com/a%2Fb",
        "https://docs.example.com/wiki/Zürich",
        "https://docs.example.com/wiki/Z%C3%BCrich",
        "https://docs.example.com/public/%EF%BC%8E%EF%BC%8E/private/x",
        "https://docs.example.com/s?q=Zürich",
        "https://docs.example.com/s?q=%EF%BD%85",
        "https://docs.example.com/s?q=%FF",
        # An escaped `%` reads as another escape when decoded twice.
        "https://docs.example.com/s?q=%2573ecret",
        # A homoglyph host (Cyrillic а) is not the trusted host.
        "https://docs.ex\u0430mple.com/guide",
    ],
)
def test_a_spelling_that_reaches_blocked_or_leaves_trusted_stays_withheld(
    url: str,
) -> None:
    _out, grant = _project(
        toolsSettings={"web_fetch": {"blocked": _PRIVATE_BLOCKED, "trusted": _TRUSTED}}
    )
    assert _judge("https://docs.example.com/guide", grant) == TOOL_AUTO_APPROVE
    assert _judge(url, grant) == TOOL_ALLOW


def test_a_blocked_query_value_is_read_unescaped_too() -> None:
    _out, grant = _project(
        toolsSettings={
            "web_fetch": {
                "blocked": ["https://docs\\.example\\.com/api\\?filter=name=foo.*"],
                "trusted": _TRUSTED,
            }
        }
    )
    assert _judge("https://docs.example.com/api?filter=name%3Dbar", grant) == TOOL_AUTO_APPROVE
    assert _judge("https://docs.example.com/api?filter=name%3Dfoo", grant) == TOOL_ALLOW
    # An entry written with escapes is read unescaped too, so a mixed spelling
    # of the same value is still withheld.
    _out, grant = _project(
        toolsSettings={
            "web_fetch": {
                "blocked": ["https://docs\\.example\\.com/api\\?redirect=https%3A%2F%2Fevil.*"],
                "trusted": _TRUSTED,
            }
        }
    )
    assert _judge("https://docs.example.com/api?redirect=https%3A%2F%2Fgood.com", grant) == (
        TOOL_AUTO_APPROVE
    )
    for url in (
        "https://docs.example.com/api?redirect=https%3A%2F%2Fevil.com",
        "https://docs.example.com/api?redirect=https:%2F%2Fevil.com",
        "https://docs.example.com/api?redirect=https://evil.com",
    ):
        assert _judge(url, grant) == TOOL_ALLOW, url
    # A form parser reads `+` as a space.
    _out, grant = _project(
        toolsSettings={
            "web_fetch": {
                "blocked": ["https://docs\\.example\\.com/s\\?q=a%20b.*"],
                "trusted": _TRUSTED,
            }
        }
    )
    assert _judge("https://docs.example.com/s?q=a%2Bb", grant) == TOOL_AUTO_APPROVE
    for url in ("https://docs.example.com/s?q=a+b", "https://docs.example.com/s?q=a%20b"):
        assert _judge(url, grant) == TOOL_ALLOW, url


def test_a_blocked_private_range_prefix_withholds_only_its_range() -> None:
    _out, grant = _project(
        toolsSettings={
            "web_fetch": {
                "blocked": [
                    "http://10\\..*",
                    "http://127\\..*",
                    "http://192\\.168\\..*",
                ],
                "trusted": [".*"],
            }
        }
    )
    assert _judge("https://docs.example.com/guide", grant) == TOOL_AUTO_APPROVE
    assert _judge("http://11.0.0.1/", grant) == TOOL_AUTO_APPROVE
    for url in (
        "http://10.0.0.1/",
        "http://127.0.0.1/",
        "http://127.1/",
        "http://0x7f.1/",
        "http://0x7f.0.0.1/",
        "http://2130706433/",
        "http://192.168.1.1/",
        "HTTP://10.0.0.1./x",
    ):
        assert _judge(url, grant) == TOOL_ALLOW, url


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("http://10.", "http://10."),
        ("http://192.168.", "http://192.168."),
        ("http://127.1", None),
        ("http://010.", None),
        ("http://0x7f.", None),
        ("http://10.:80", None),
    ],
)
def test_a_cut_decimal_host_prefix_is_kept_as_written(text: str, expected: str | None) -> None:
    assert canonical_url(text, prefix=True) == expected


def test_empty_segments_are_read_three_ways_and_non_ascii_is_refused() -> None:
    assert canonical_url("https://x.com/wiki/Zürich") is None
    assert canonical_url("https://x.com/a//b/../c") == "https://x.com/a//c"
    assert canonical_url("https://x.com/a//b/../c", collapse="before") == "https://x.com/a/c"
    assert canonical_url("https://x.com//a//../b", collapse="after") == "https://x.com/a/b"
    assert canonical_url("https://ex\u0430mple.com/") is None


def test_a_non_ascii_blocked_entry_withholds_every_grant() -> None:
    # It has no canonical form, as on main.
    _out, grant = _project(
        toolsSettings={
            "web_fetch": {
                "blocked": ["https://docs\\.example\\.com/cafe\u0301/.*"],
                "trusted": _TRUSTED,
            }
        }
    )
    assert _judge("https://docs.example.com/caf\u00e9/x", grant) == TOOL_ALLOW
    assert _judge("https://docs.example.com/guide", grant) == TOOL_ALLOW


def test_a_trusted_cut_decimal_host_prefix_grants_nothing() -> None:
    _out, grant = _project(toolsSettings={"web_fetch": {"trusted": ["http://10\\..*"]}})
    assert _judge("http://10.0.0.1.evil.com/", grant) == TOOL_ALLOW
    assert _judge("http://10.0.0.1/", grant) == TOOL_ALLOW
