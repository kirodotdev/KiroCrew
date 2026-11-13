# SPDX-License-Identifier: Apache-2.0
"""Add abilities leaves out a grant-only tool setting the Bot's worker already grants.

``web_fetch.trusted`` under a bare ``web_fetch`` allow, and ``subagent.trustedAgents``
whose literal names a carried ``subagent`` allow lists, are left out of the copy and
previewed as ``skipped`` ("Already has") instead of refusing the add. Kit time only:
the worker merge still refuses either key in a Bot's file (owner decision,
``docs/decisions``).
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import mate_approval_revocations as revocations
from kiro_crew import mate_capabilities, mate_worker, mate_worker_kit
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.config import paths
from kiro_crew.mate_spec_policy import SpecPolicyUncarried
from kiro_crew.mate_worker_kit import WorkerKitError

TRUSTED = [".*docs\\.example\\.com.*"]
WEB_FETCH_ALLOW = {"capability": "web_fetch", "effect": "allow"}
SUBAGENT_ALLOW = {
    "capability": "subagent",
    "effect": "allow",
    "match": ["reviewer", "builder"],
}

#: The worker base as Crew installs it lists ``web_fetch`` in ``allowedTools``.
WORKER_BASE = {
    "name": "kirocrew-mate-worker",
    "prompt": "base",
    "tools": ["*"],
    "allowedTools": ["fs_read", "web_fetch"],
    "mcpServers": {},
}

#: Atlas's shape: a bare ``web_fetch`` allow beside a ``web_fetch.trusted`` list.
ATLAS = {
    "name": "release-bot",
    "prompt": "Atlas SOP.",
    "tools": ["*"],
    "mcpServers": {},
    "allowedTools": ["fs_read", "web_fetch"],
    "permissions": {"rules": [WEB_FETCH_ALLOW]},
    "toolsSettings": {"web_fetch": {"trusted": TRUSTED}},
}


@pytest.fixture(autouse=True)
def _no_ceiling(_floor_monkeypatch):
    """The governance ceiling is its own module's concern; here it passes grants."""
    from kiro_crew.platform import governance

    _floor_monkeypatch.setattr(
        governance, "sanitize_agent_config_governance", lambda spec, **_k: None
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    user, mates = tmp_path / "agents", tmp_path / "mates" / "agents"
    user.mkdir()
    mates.mkdir(parents=True)
    (user / "release-bot.json").write_text(json.dumps(ATLAS), encoding="utf-8")
    (mates / "pixel.json").write_text(
        json.dumps({"name": "pixel", "prompt": "", "tools": [], "mcpServers": {}}),
        encoding="utf-8",
    )
    (mates / "kirocrew-mate-worker.json").write_text(json.dumps(WORKER_BASE), encoding="utf-8")
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_capabilities, "binding_is_mate", lambda member: member == "pixel")
    monkeypatch.setattr(mate_worker_kit, "_crewmate_backend", lambda member, held: ACP_BACKEND_KAS)
    return user, mates


def _file(mates) -> dict:
    return json.loads((mates / "pixel.json").read_text(encoding="utf-8"))


def _use(user, template: dict) -> None:
    (user / "release-bot.json").write_text(json.dumps(template), encoding="utf-8")


def _base(mates, **changes) -> None:
    (mates / "kirocrew-mate-worker.json").write_text(
        json.dumps({**WORKER_BASE, **changes}), encoding="utf-8"
    )


def _rows(preview: dict) -> set[tuple[str, str, str]]:
    return {(r["section"], r["id"], r["change"]) for r in preview["changes"]}


def _refused(field: str, *, alone: bool = True) -> str:
    """The add's refusal, naming *field* (and nothing else when *alone*)."""
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.preview("pixel", {"template": "release-bot"})
    assert caught.value.code == "template_restrictions_uncarried"
    if alone:
        assert caught.value.detail == field
    else:
        assert field in caught.value.detail
    return caught.value.detail


def _add() -> dict:
    """Preview and apply the add; a refusal fails the test, naming its code and detail."""
    try:
        preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
        body = {"template": "release-bot", "revision": preview["revision"]}
        mate_worker_kit.apply("pixel", body)
    except WorkerKitError as exc:
        pytest.fail(f"the add was refused: {exc.code}: {exc.detail}")
    return preview


# --- web_fetch.trusted -----------------------------------------------------------


@pytest.mark.parametrize("tool", ["web_fetch", "webFetch"])
def test_a_web_fetch_trust_the_worker_already_grants_is_left_out_as_already_has(home, tool):
    user, mates = home
    _use(user, {**ATLAS, "toolsSettings": {tool: {"trusted": TRUSTED}}})
    preview = _add()
    rows = _rows(preview)
    assert ("toolsSettings", f"{tool}.trusted", "skipped") in rows
    assert not any(section == "toolsSettings" and change == "added" for section, _, change in rows)
    written = _file(mates)
    assert "toolsSettings" not in written
    assert written["permissions"] == {"rules": [WEB_FETCH_ALLOW]}
    # The worker starts from the written file, and fetches unasked as the agent does.
    merged = mate_worker.merge_worker_spec(
        WORKER_BASE, mate_worker.worker_additions(WORKER_BASE, written)
    )
    assert "web_fetch" in merged["allowedTools"]


def test_a_block_beside_the_trust_is_still_carried(home):
    user, mates = home
    settings = {"web_fetch": {"blocked": ["evil\\.example\\.com"], "trusted": TRUSTED}}
    _use(user, {**ATLAS, "toolsSettings": settings})
    rows = _rows(_add())
    assert ("toolsSettings", "web_fetch", "added") in rows
    assert ("toolsSettings", "web_fetch.trusted", "skipped") in rows
    assert _file(mates)["toolsSettings"] == {"web_fetch": {"blocked": ["evil\\.example\\.com"]}}


def test_a_web_fetch_trust_is_refused_when_the_worker_would_ask(home):
    """Without ``web_fetch`` in the worker's ``allowedTools`` the key would grant
    something KAS never applies, so it stays refused by name."""
    user, mates = home
    _base(mates, allowedTools=["fs_read"])
    _use(user, {**ATLAS, "permissions": {}})
    before = _file(mates)
    _refused("toolsSettings.web_fetch.trusted")
    assert _file(mates) == before


def test_a_web_fetch_trust_is_refused_when_the_ceiling_withholds_web_fetch(
    home, _floor_monkeypatch
):
    from kiro_crew.platform import governance

    def withhold(spec, **_k):
        if isinstance(spec.get("allowedTools"), list):
            spec["allowedTools"] = [t for t in spec["allowedTools"] if t != "web_fetch"]

    _floor_monkeypatch.setattr(governance, "sanitize_agent_config_governance", withhold)
    _refused("toolsSettings.web_fetch.trusted")


@pytest.mark.parametrize("narrowing", ["revoked", "excluded", "record-unreadable"])
def test_a_web_fetch_trust_is_refused_when_the_bots_own_worker_would_ask(home, narrowing):
    """The proof reads the worker the Bot runs: an approval its owner revoked and a
    tool its own file excludes take ``web_fetch`` away from it, and a revoked
    approvals record that cannot be read proves nothing."""
    user, mates = home
    _use(user, {**ATLAS, "permissions": {}})
    if narrowing == "revoked":
        revocations.record("pixel", ["web_fetch"], [])
    elif narrowing == "excluded":
        own = {**_file(mates), "excludedTools": ["web_fetch"]}
        (mates / "pixel.json").write_text(json.dumps(own), encoding="utf-8")
    else:
        (mates.parent / "revoked-approvals.json").write_text("{not json", encoding="utf-8")
    before = _file(mates)
    _refused("toolsSettings.web_fetch.trusted")
    assert _file(mates) == before


def test_an_approval_revoked_on_another_tool_still_leaves_the_trust_out(home):
    revocations.record("pixel", ["fs_read"], [])
    assert ("toolsSettings", "web_fetch.trusted", "skipped") in _rows(_add())


def test_nothing_is_left_out_when_the_template_is_refused_for_something_else(home):
    """The proof needs a worker that merges; one that does not names every key."""
    user, _ = home
    settings = {
        "web_fetch": {"trusted": TRUSTED},
        "execute_bash": {"deniedCommands": ["git push (origin|upstream) .*"]},
    }
    _use(user, {**ATLAS, "toolsSettings": settings})
    detail = _refused("toolsSettings.web_fetch.trusted", alone=False)
    assert "toolsSettings.execute_bash.deniedCommands" in detail


@pytest.mark.parametrize(
    ("settings", "key", "dropped"),
    [
        pytest.param(
            {"web_fetch": {"trusted": TRUSTED}, "webFetch": {"blocked": ["evil\\.example\\.com"]}},
            "toolsSettings.web_fetch.trusted",
            "toolsSettings.webFetch",
            id="web_fetch",
        ),
        pytest.param(
            {
                "subagent": {"trustedAgents": ["reviewer"]},
                "use_subagent": {"trustedAgents": ["builder"]},
            },
            "toolsSettings.subagent.trustedAgents",
            "toolsSettings.use_subagent",
            id="subagent",
        ),
    ],
)
def test_nothing_is_left_out_when_the_tool_is_written_under_two_spellings(
    home, settings, key, dropped
):
    """kiro-cli keeps one spelling of a tool and drops the other; leaving a key out
    must not change which one the worker keeps, so the refusal names both."""
    user, mates = home
    rules = [WEB_FETCH_ALLOW, SUBAGENT_ALLOW]
    _use(user, {**ATLAS, "permissions": {"rules": rules}, "toolsSettings": settings})
    before = _file(mates)
    detail = _refused(dropped, alone=False)
    assert key in detail
    assert _file(mates) == before


# --- subagent.trustedAgents --------------------------------------------------------


@pytest.mark.parametrize("tool", ["subagent", "use_subagent"])
def test_trusted_agents_a_carried_allow_names_are_left_out_as_already_has(home, tool):
    user, mates = home
    template = {
        **ATLAS,
        "permissions": {"rules": [SUBAGENT_ALLOW]},
        "toolsSettings": {tool: {"trustedAgents": ["builder", "reviewer"]}},
    }
    _use(user, template)
    rows = _rows(_add())
    assert ("toolsSettings", f"{tool}.trustedAgents", "skipped") in rows
    written = _file(mates)
    assert "toolsSettings" not in written
    assert written["permissions"] == {"rules": [SUBAGENT_ALLOW]}


@pytest.mark.parametrize(
    ("allow", "trusted"),
    [
        pytest.param(SUBAGENT_ALLOW, ["reviewer", "deployer"], id="a-name-the-allow-lacks"),
        pytest.param({**SUBAGENT_ALLOW, "match": ["gpu-*"]}, ["gpu-*"], id="a-pattern"),
        pytest.param({**SUBAGENT_ALLOW, "exclude": ["builder"]}, ["reviewer"], id="an-exclude"),
        pytest.param({**SUBAGENT_ALLOW, "effect": "ask"}, ["reviewer"], id="an-ask-not-an-allow"),
        pytest.param(SUBAGENT_ALLOW, [], id="an-empty-list"),
    ],
)
def test_trusted_agents_no_carried_allow_covers_stay_refused(home, allow, trusted):
    user, _ = home
    template = {
        **ATLAS,
        "permissions": {"rules": [allow]},
        "toolsSettings": {"subagent": {"trustedAgents": trusted}},
    }
    _use(user, template)
    _refused("toolsSettings.subagent.trustedAgents")


# --- session start is unchanged ----------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "named"),
    [
        ({"web_fetch": {"trusted": TRUSTED}}, "toolsSettings.web_fetch.trusted"),
        ({"subagent": {"trustedAgents": ["reviewer"]}}, "toolsSettings.subagent.trustedAgents"),
    ],
)
def test_a_hand_edited_bot_file_holding_the_key_still_fails_closed(settings, named):
    """The leave-out runs at kit time only: the worker merge refuses either key."""
    file_spec = {
        "name": "pixel",
        "prompt": "",
        "permissions": {"rules": [WEB_FETCH_ALLOW, SUBAGENT_ALLOW]},
        "toolsSettings": settings,
    }
    with pytest.raises(SpecPolicyUncarried) as caught:
        mate_worker.merge_worker_spec(
            WORKER_BASE, mate_worker.worker_additions(WORKER_BASE, file_spec)
        )
    assert named in caught.value.fields
