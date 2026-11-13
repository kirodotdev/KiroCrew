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
from kiro_crew.platform.spec_denied_gate import url_literal


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


_HOST_ONLY = "(whole-URL rule; KAS checks host only)"
_REGEX_SYNTAX = "(regex syntax KAS cannot express)"


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
