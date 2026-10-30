"""``capabilities.publish`` lists ``regions``, ``audience`` and ``accounts``.

RFC ``docs/request-for-change/rfc-crew-enterprise-edition.md``, appendix rows
"Deploy regions", "Publish audiences" and "Deploy accounts". A policy names the
regions and cloud accounts a deploy may write to and who may read what is
published; each is asked before anything is uploaded. In an enterprise build a
list the remote policy leaves out is off, even when it names ``publish``. The
``oss`` edition with no list behaves as before.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew import publish_governance as pg
from kiro_crew import publish_sync
from kiro_crew.deploy import handlers as deploy_handlers
from kiro_crew.deploy import handlers_sites
from kiro_crew.platform import governance
from kiro_crew.platform.governance import SCOPE_CATALOG, parse_policy, resolve

_SCOPE = "capabilities.publish"
_ACCOUNT = "123456789012"


def _ceiling(publish: dict | None = None, tier: str = "") -> governance.GovernanceCeiling:
    doc: dict = {"version": 1, "boot": {}}
    if publish is not None:
        doc["capabilities"] = {"publish": publish}
    ceiling = parse_policy(doc)
    return dataclasses.replace(ceiling, tier=tier) if tier else ceiling


def _publish_on(**lists: dict) -> dict:
    scopes = {"destinations": {"mode": "allow", "allow": ["deploy-web-aws"]}, **lists}
    return {"enabled": True, "scopes": scopes}


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


# ── The keys ────────────────────────────────────────────────────────────────


def test_the_three_lists_are_catalog_keys() -> None:
    matchers = SCOPE_CATALOG[_SCOPE].scope_matchers
    assert {matchers[k] for k in ("regions", "audience", "accounts")} == {"identifier"}


def test_a_named_list_bounds_its_items() -> None:
    ceiling = _ceiling(
        _publish_on(
            regions={"mode": "allow", "allow": ["us-*"]},
            audience={"mode": "allow", "allow": ["private"]},
            accounts={"mode": "deny", "deny": [_ACCOUNT]},
        )
    )
    assert resolve(ceiling, None, _SCOPE, "regions:us-west-2").permitted
    assert not resolve(ceiling, None, _SCOPE, "regions:eu-west-1").permitted
    assert resolve(ceiling, None, _SCOPE, "audience:private").permitted
    assert not resolve(ceiling, None, _SCOPE, "audience:public").permitted
    assert not resolve(ceiling, None, _SCOPE, f"accounts:{_ACCOUNT}").permitted
    assert resolve(ceiling, None, _SCOPE, "accounts:210987654321").permitted


@pytest.mark.parametrize("publish", [None, _publish_on()])
def test_oss_without_the_lists_permits_as_before(publish: dict | None) -> None:
    for item in ("regions:eu-west-1", "audience:public", f"accounts:{_ACCOUNT}"):
        assert resolve(_ceiling(publish), None, _SCOPE, item).permitted, item


def test_enterprise_named_publish_keeps_unnamed_lists_off(enterprise: None) -> None:
    ceiling = _ceiling(_publish_on(regions={"mode": "allow", "allow": ["us-east-1"]}))
    assert resolve(ceiling, None, _SCOPE, "destinations:deploy-web-aws").permitted
    assert resolve(ceiling, None, _SCOPE, "regions:us-east-1").permitted
    assert not resolve(ceiling, None, _SCOPE, "audience:public").permitted
    assert not resolve(ceiling, None, _SCOPE, f"accounts:{_ACCOUNT}").permitted


def test_enterprise_local_tier_cannot_supply_a_list_the_remote_left_out(
    enterprise: None,
) -> None:
    central = _ceiling(_publish_on(), tier=governance.TIER_CENTRAL)
    home = _ceiling(
        _publish_on(
            regions={"mode": "deny", "deny": []},
            audience={"mode": "deny", "deny": []},
        ),
        tier=governance.TIER_HOME,
    )
    composed = governance.compose_tier_ladder(central, home)
    assert not resolve(composed, None, _SCOPE, "regions:us-east-1").permitted
    assert not resolve(composed, None, _SCOPE, "audience:public").permitted


def test_enterprise_remote_may_open_a_list(enterprise: None) -> None:
    central = _ceiling(
        _publish_on(audience={"mode": "allow", "allow": ["public"]}),
        tier=governance.TIER_CENTRAL,
    )
    composed = governance.compose_tier_ladder(central)
    assert resolve(composed, None, _SCOPE, "audience:public").permitted
    assert not resolve(composed, None, _SCOPE, "regions:us-east-1").permitted


# ── The deploy decision ─────────────────────────────────────────────────────


def _answer(denied_items: set[str]):
    asked: list[str] = []

    def _permits(scope, item, **_kw):
        asked.append(item)
        return SimpleNamespace(
            permitted=item not in denied_items, reason="nope", rule="r", layer="policy"
        )

    return asked, _permits


def _patch(monkeypatch, denied: set[str], governed: bool) -> list[str]:
    """*governed*: the accounts list restricts something (it is not fully open)."""
    asked, permits = _answer(denied)
    monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", permits)
    monkeypatch.setattr(pg, "publish_list_unrestricted", lambda _s, _n: not governed)
    return asked


def test_deploy_asks_audience_then_region(monkeypatch) -> None:
    asked = _patch(monkeypatch, set(), governed=False)
    assert pg.deploy_target_denied_reason("s", "us-east-1") is None
    assert asked == ["audience:public", "regions:us-east-1"]


def test_deploy_denied_region_is_refused(monkeypatch) -> None:
    _patch(monkeypatch, {"regions:eu-west-1"}, governed=False)
    reason = pg.deploy_target_denied_reason("s", "eu-west-1")
    assert reason and "regions" in reason


def test_deploy_refused_while_accounts_restricts(monkeypatch) -> None:
    """Account binding is not supported yet, so a bounded list refuses every deploy."""
    asked = _patch(monkeypatch, set(), governed=True)
    reason = pg.deploy_target_denied_reason("s", "us-east-1")
    assert reason and "accounts" in reason
    assert not any(item.startswith("accounts:") for item in asked)


def test_oss_deploy_without_lists_is_unchanged() -> None:
    """The standalone default context carries no policy, so nothing is refused."""
    assert pg.deploy_target_denied_reason("s", "eu-west-1") is None


def test_governance_error_denies(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", _boom)
    assert pg.publish_list_denied_reason("s", "deploy-web-aws", "regions", "us-east-1")


@pytest.mark.parametrize(
    ("accounts", "open_"),
    [
        (None, True),
        ({"mode": "deny", "deny": []}, True),
        ({"mode": "allow", "allow": ["*"]}, True),
        ({"mode": "deny", "deny": [_ACCOUNT]}, False),
        ({"mode": "allow", "allow": [_ACCOUNT]}, False),
    ],
)
def test_list_probe_asks_what_the_list_allows(monkeypatch, accounts, open_) -> None:
    """A present list is open only if it permits every value, the unknown one too."""
    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.resolve_active_scope", lambda _s: None
    )
    lists = {} if accounts is None else {"accounts": accounts}
    ctx = SimpleNamespace(governance=_ceiling(_publish_on(**lists)))
    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: ctx)
    assert pg.publish_list_unrestricted("s", "accounts") is open_


def test_enterprise_open_lists_let_the_drives_publish(enterprise, monkeypatch) -> None:
    """Presence is not a restriction: every list is present in enterprise."""
    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.resolve_active_scope", lambda _s: None
    )
    open_lists = {n: {"mode": "deny", "deny": []} for n in ("regions", "audience", "accounts")}
    publish = _publish_on(**open_lists)
    publish["scopes"]["destinations"] = {"mode": "deny", "deny": []}
    ctx = SimpleNamespace(governance=_ceiling(publish))
    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: ctx)
    monkeypatch.setattr(pg, "_read_allowed_destinations", lambda: ([], None))
    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.governance_permits",
        lambda *_a, **_k: SimpleNamespace(permitted=True),
    )
    request = SimpleNamespace(headers={"X-Session-Key": "s"})
    for drive in ("personal-drive", "aws-control-drive"):
        assert pg.publish_denied_reason(request, drive) is None, drive
    # A deny list with an entry may name the drive's unknown region: refused.
    publish["scopes"]["regions"] = {"mode": "deny", "deny": ["us-east-1"]}
    ctx.governance = _ceiling(publish)
    assert "regions" in (pg.publish_denied_reason(request, "personal-drive") or "")


def test_do_deploy_refuses_before_staging(monkeypatch) -> None:
    async def _profile(_p):
        return "dev", "eu-west-1"

    seen: dict = {}

    def _deny(session_key, region):
        seen.update(session_key=session_key, region=region)
        return "regions 'eu-west-1' is not permitted"

    monkeypatch.setattr(deploy_handlers, "_resolve_profile", _profile)
    monkeypatch.setattr(deploy_handlers, "deploy_target_denied_reason", _deny)
    monkeypatch.setattr(handlers_sites.os, "name", "posix")
    status, body = asyncio.run(
        deploy_handlers._do_deploy({"site_id": "s", "artifact_slug": "a"}, session_key="k")
    )
    assert status == 403
    assert body["code"] == "publish_destination_disabled"
    assert seen == {"session_key": "k", "region": "eu-west-1"}


# ── Every other destination: refused unless the lists are open ──────────────


def _gate_permits(monkeypatch) -> SimpleNamespace:
    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.governance_permits",
        lambda *_a, **_k: SimpleNamespace(permitted=True),
    )
    monkeypatch.setattr(pg, "_read_allowed_destinations", lambda: ([], None))
    return SimpleNamespace(headers={"X-Session-Key": "s"})


@pytest.mark.parametrize("destination", ["default", "personal-drive", "aws-control-drive"])
@pytest.mark.parametrize("bounded", ["regions", "audience", "accounts"])
def test_other_destinations_refused_while_a_list_restricts(
    monkeypatch, destination, bounded
) -> None:
    request = _gate_permits(monkeypatch)
    monkeypatch.setattr(pg, "publish_list_unrestricted", lambda _s, n: n != bounded)
    assert bounded in (pg.publish_denied_reason(request, destination) or "")
    monkeypatch.setattr(pg, "publish_list_unrestricted", lambda _s, _n: True)
    assert pg.publish_denied_reason(request, destination) is None


def test_enterprise_artifact_publish_is_refused_by_default(enterprise, monkeypatch) -> None:
    """The baseline turns the lists off, so a named destination still cannot publish."""
    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.resolve_active_scope", lambda _s: None
    )
    publish = _publish_on()
    publish["scopes"]["destinations"] = {"mode": "allow", "allow": ["default"]}
    ctx = SimpleNamespace(governance=_ceiling(publish))
    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: ctx)
    request = _gate_permits(monkeypatch)
    assert "only the public web deploy" in (pg.publish_denied_reason(request, "default") or "")


def test_oss_artifact_publish_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.resolve_active_scope", lambda _s: None
    )
    ctx = SimpleNamespace(governance=None)
    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: ctx)
    request = _gate_permits(monkeypatch)
    assert pg.publish_denied_reason(request, "default") is None


@pytest.mark.asyncio
async def test_artifact_gate_runs_off_the_event_loop(tmp_path, monkeypatch) -> None:
    """The gate reads policy and profiles from disk, so it does not run on the loop."""
    import threading

    from test_artifact_sharing_handlers import _request

    from kiro_crew import artifacts as art_mod
    from kiro_crew.artifacts import ArtifactStore
    from kiro_crew.dashboard.handlers import artifacts as art_handlers

    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(art_handlers, "_is_restricted_session", lambda *_a: False)
    loop_thread = threading.current_thread()
    on_loop: list[bool] = []

    def _record(*_a):
        on_loop.append(threading.current_thread() is loop_thread)
        return None

    monkeypatch.setattr(art_handlers, "_publish_governance_denied", _record)
    monkeypatch.setattr(publish_sync, "publish", AsyncMock())
    art = store.create(name="Doc", content="x", kind="html")
    await art_handlers.api_artifact_publish(
        _request(match={"slug": art.slug}, body={"visibility": "PRIVATE"})
    )
    assert on_loop == [False]


def test_list_probe_disabled_gate_is_not_open(monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.resolve_active_scope", lambda _s: None
    )
    ctx = SimpleNamespace(governance=_ceiling({"enabled": False}))
    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: ctx)
    assert not pg.publish_list_unrestricted("s", "accounts")


def test_deploy_web_id_skips_the_list_refusal_only_on_the_core_path(monkeypatch) -> None:
    """An app can declare the same id; only the caller that checks the lists skips."""
    request = _gate_permits(monkeypatch)
    monkeypatch.setattr(pg, "publish_list_unrestricted", lambda _s, n: n != "regions")
    assert "regions" in (pg.publish_denied_reason(request, "deploy-web-aws") or "")
    assert pg.publish_denied_reason(request, "deploy-web-aws", lists_checked_by_caller=True) is None


def test_session_denied_reason_skips_list_refusal_for_deploy_core_path(monkeypatch) -> None:
    """session_denied_reason with lists_checked_by_caller=True skips the list gate."""
    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.governance_permits",
        lambda *_a, **_k: SimpleNamespace(permitted=True),
    )
    monkeypatch.setattr(pg, "_read_allowed_destinations", lambda: ([], None))
    monkeypatch.setattr(pg, "publish_list_unrestricted", lambda _s, n: False)
    assert (
        pg.session_denied_reason("s", pg.DEPLOY_WEB_PROVIDER_ID, lists_checked_by_caller=True)
        is None
    )


def test_session_denied_reason_enforces_list_refusal_without_flag(monkeypatch) -> None:
    """session_denied_reason with lists_checked_by_caller=False (default) still refuses."""
    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.governance_permits",
        lambda *_a, **_k: SimpleNamespace(permitted=True),
    )
    monkeypatch.setattr(pg, "_read_allowed_destinations", lambda: ([], None))
    monkeypatch.setattr(pg, "publish_list_unrestricted", lambda _s, n: False)
    reason = pg.session_denied_reason("s", pg.DEPLOY_WEB_PROVIDER_ID)
    assert reason is not None
    assert "only the public web deploy" in reason
