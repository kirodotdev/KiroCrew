"""Who may Edit & Resend or rewind to a row: the shared table, on the real identities.

``test/fixtures/shared_crew_edit_rule.json`` is also read by the dashboard's
``website/src/api/editRule.test.ts``. Here every editor and author is built from
the real identity functions, and the owner's ids from the real
``owner.owner_principal_ids`` (with a Tailscale login set for the owner).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard.kiro_relay.caller import RelayCaller, RelayCallerKind
from kiro_crew.shared_crew import access_set, owner
from kiro_crew.shared_crew.attribution import author_stamp, stamp_principal_author
from kiro_crew.shared_crew.contracts import MESSAGE_AUTHOR_META_KEY, Principal, Provenance
from kiro_crew.shared_crew.edit_rule import Editor, may_edit
from kiro_crew.shared_crew.identity import (
    app_principal,
    local_operator_principal,
    relay_principal,
    tailnet_principal,
)

TABLE = json.loads(
    (Path(__file__).parent / "fixtures" / "shared_crew_edit_rule.json").read_text(encoding="utf-8")
)
OWNER_LOGIN = "owner@example.test"
TEAMMATE = "kiro-user.teammate-ana-0002"


def _principals() -> dict[str, Principal]:
    return {
        "owner_local": local_operator_principal("local-app"),
        "owner_startup": local_operator_principal("local-startup"),
        "owner_tailnet": tailnet_principal(OWNER_LOGIN, "n1", Provenance.TAILSCALE_SERVE),
        "owner_relay": relay_principal(
            RelayCaller(kiro_user_id="kiro-user.owner-0001", kind=RelayCallerKind.OWNER),
            "local-app",
        ),
        "teammate_relay": relay_principal(
            RelayCaller(kiro_user_id=TEAMMATE, kind=RelayCallerKind.TEAMMATE), "local-app"
        ),
        "teammate_tailnet": tailnet_principal(
            "alice@example.test", "n2", Provenance.TAILSCALE_LISTENER
        ),
        "unknown": Principal.unknown(),
        "app": app_principal("notes"),
        "other_app": app_principal("other"),
        "automation": Principal.system("cron"),
    }


def _meta(author: str, people: dict[str, Principal]) -> dict[str, Any]:
    if author == "absent":
        return {}
    if author == "damaged":
        return {"author": {"id": "not a principal id", "kind": "human"}}
    if author == "unknown_stamp":
        return {"author": author_stamp(Principal.unknown())}
    if author == "teammate_relay_legacy":
        return {"author": people["teammate_relay"].to_public_dict()}
    return {"author": author_stamp(people[author])}


@pytest.fixture
def people(monkeypatch) -> dict[str, Principal]:
    owner.reset_for_tests()
    monkeypatch.setattr(access_set, "owner_login", lambda: OWNER_LOGIN)
    return _principals()


@pytest.mark.parametrize("case", TABLE["cases"], ids=lambda c: f"{c['editor']}-{c['author']}")
def test_the_shared_table(case: dict[str, Any], people: dict[str, Principal]) -> None:
    principal = people[case["editor"]]
    editor = Editor(
        ids=owner.writes_as_ids(principal),
        is_owner=owner.is_owner_principal(principal),
        relay_teammate=case["editor"] == "teammate_relay",
    )
    meta = _meta(case["author"], people)
    assert may_edit(editor, meta, preview_on=case["preview"]) is case["expect"], case["why"]


def test_the_stamp_carries_the_provider_and_no_issuer_or_subject(people) -> None:
    stamp = author_stamp(people["teammate_relay"])
    assert stamp["provider"] == "kiro"
    assert "issuer" not in stamp and "subject" not in stamp
    channel = stamp_principal_author({}, Principal.unknown())[MESSAGE_AUTHOR_META_KEY]
    assert channel == author_stamp(Principal.unknown())


def test_an_unknown_caller_writes_only_as_unknown(people) -> None:
    assert owner.writes_as_ids(Principal.unknown()) == frozenset({Principal.unknown().id})
    assert people["owner_tailnet"].id in owner.writes_as_ids(people["owner_relay"])
