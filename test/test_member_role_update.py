"""Role update (design: Crew Member = Custom Agent + Wrapper, rollout step 4).

A member hired from a store template can take the template's newer version
through a per-field three-way merge -- BASE the pristine copy the hire recorded,
MINE the member's own agent file plus its card fields, THEIRS the template as
the app ships it now -- and can detach from the template for good. The step-4
gate: update a template one member customized; the customization survives.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import test_member_hire as _hire
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import agent_state, member_templates
from kiro_crew.config.loader import KiroCrewConfig

APP = _hire.APP
TEMPLATE_AGENT = _hire.TEMPLATE_AGENT
_store_hire = _hire._store_hire
# The hire suite's fixtures, registered here by assignment: an installed, enabled
# app offering one template, and an agents directory with its materialized copy.
_owner_caller = _hire._owner_caller
agents_dir = _hire.agents_dir
store_app = _hire.store_app

MEMBER = "Pager-triage"


def FID(field: str) -> str:
    """The opaque id a plan field is resolved by."""
    return member_templates.field_id(field)


def _app() -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_member_detach,
        api_member_hire,
        api_member_role_update_apply,
        api_member_role_update_get,
        api_members,
    )

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = MagicMock(sessions=None)
    app.router.add_post("/api/members", api_member_hire)
    app.router.add_get("/api/members", api_members)
    app.router.add_get("/api/members/{member}/role-update", api_member_role_update_get)
    app.router.add_post("/api/members/{member}/role-update", api_member_role_update_apply)
    app.router.add_post("/api/members/{member}/detach", api_member_detach)
    return app


async def _hire_pager(client) -> None:
    resp = await client.post("/api/members", json=_store_hire("Pager triage"))
    assert resp.status == 200, await resp.text()


async def _apply(client, body: dict, *, fingerprint: str | None = None, theirs: str | None = None):
    """POST an apply the way the panel does: carrying the fingerprints of the
    member and the template as the plan just saw them (or explicit ones)."""
    if fingerprint is None or theirs is None:
        plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
        fingerprint = fingerprint or plan["member_fingerprint"]
        theirs = theirs or plan["template_fingerprint"]
    return await client.post(
        f"/api/members/{MEMBER}/role-update",
        json={"member_fingerprint": fingerprint, "template_fingerprint": theirs, **body},
    )


def _member_spec(agents_dir: Path) -> dict:
    return json.loads((agents_dir / f"{MEMBER}.json").read_text(encoding="utf-8"))


def _write_member_spec(agents_dir: Path, spec: dict) -> None:
    (agents_dir / f"{MEMBER}.json").write_text(json.dumps(spec), encoding="utf-8")


def _publish(store_app: Path, agents_dir: Path, version: str, *, spec=None, card=None) -> None:
    """Ship a new version of the app: manifest version, shipped spec, card,
    and the materialized copy the bridge would rewrite on update."""
    from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, InstalledApp, _write_installed

    manifest = json.loads((store_app / APP_MANIFEST_FILENAME).read_text())
    manifest["version"] = version
    if card:
        manifest["crew"]["templates"][0].update(card)
    (store_app / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    if spec is not None:
        (store_app / TEMPLATE_AGENT).write_text(json.dumps(spec), encoding="utf-8")
        (agents_dir / f"{APP}--triage.json").write_text(json.dumps(spec), encoding="utf-8")
    _write_installed(APP, InstalledApp(name=APP, version=version, enabled=True))


def _shipped(store_app: Path) -> dict:
    return json.loads((store_app / TEMPLATE_AGENT).read_text())


class TestPlanAndMerge:
    """The pure merge, no filesystem."""

    def _theirs(self, spec: dict, role="Oncall Triage Engineer", triggers="incident"):
        from kiro_crew.apps.manifest import CrewTemplate

        return member_templates.StoreTemplate(
            app=APP,
            version="2.0.0",
            card=CrewTemplate(agent=TEMPLATE_AGENT, role=role, triggers=triggers),
            agent_name="triage",
            materialized=f"{APP}--triage",
            spec=spec,
            materialized_spec=spec,
        )

    def test_each_field_lands_in_exactly_one_state(self):
        base = {"agent": {"name": "triage", "prompt": "p0", "tools": ["A"], "model": "m0"}}
        base["card"] = {"role": "Oncall Triage Engineer", "triggers": "incident"}
        mine = {"name": "Pager-triage", "prompt": "p0", "tools": ["A", "B"], "model": "mine"}
        theirs = self._theirs(
            {"name": "triage", "prompt": "p1", "tools": ["A"], "model": "theirs", "hooks": {}},
            triggers="incident, outage",
        )
        deltas = {
            d.field: d
            for d in member_templates.plan_role_update(
                base, mine, {"role": "Oncall Triage Engineer", "triggers": "incident"}, theirs
            )
        }
        assert deltas["spec.prompt"].state == member_templates.APPLY
        assert deltas["spec.tools"].state == member_templates.KEEP
        assert deltas["spec.model"].state == member_templates.CONFLICT
        assert deltas["spec.hooks"].state == member_templates.APPLY
        assert deltas["spec.hooks"].base is member_templates.MISSING
        assert deltas["card.role"].state == member_templates.UNCHANGED
        assert deltas["card.triggers"].state == member_templates.APPLY
        # `name` is the id on both sides and never a field.
        assert "spec.name" not in deltas
        assert member_templates.needs_update(list(deltas.values()))

    def test_a_type_change_is_a_change_json_equality_not_pythons(self):
        """``True == 1``, ``1 == 1.0`` and ``0 == False`` in Python. Read with
        ``==``, a member that changed ``strict: true`` to ``strict: 1`` looks
        UNCHANGED, and a template flipping the flag to ``false`` then APPLIES
        over the member's edit with no conflict. The merge compares JSON
        values: a different type is a different value, recursively."""
        eq = member_templates.json_equal
        assert not eq(True, 1) and not eq(1, True) and not eq(0, False)
        assert not eq(1, 1.0) and not eq(1.0, 1)
        assert not eq({"a": [1, {"b": True}]}, {"a": [1, {"b": 1}]})
        assert eq({"a": [1, {"b": True}]}, {"a": [1, {"b": True}]})
        assert not eq("1", 1) and not eq(None, False) and not eq([], {})
        assert eq(member_templates.MISSING, member_templates.MISSING)
        assert not eq(member_templates.MISSING, None)
        base = {
            "agent": {"strict": True, "retries": 1, "flags": [0, 1], "opts": {"on": False}},
            "card": {"role": "R", "triggers": ""},
        }
        # MINE changed every value's TYPE only; THEIRS changed the values.
        mine = {
            "name": "me",
            "strict": 1,
            "retries": 1.0,
            "flags": [False, True],
            "opts": {"on": 0},
        }
        theirs = self._theirs(
            {"strict": False, "retries": 2, "flags": [0, 1], "opts": {"on": False}},
            role="R",
            triggers="",
        )
        states = {
            d.field: d.state
            for d in member_templates.plan_role_update(base, mine, {"role": "R"}, theirs)
        }
        assert states["spec.strict"] == member_templates.CONFLICT
        assert states["spec.retries"] == member_templates.CONFLICT
        assert states["spec.flags"] == member_templates.KEEP
        assert states["spec.opts"] == member_templates.KEEP
        # And the apply honours it: the member's ``1`` is not overwritten by
        # the template's ``false`` without a decision.
        deltas = member_templates.plan_role_update(base, mine, {"role": "R"}, theirs)
        with pytest.raises(member_templates.UnresolvedConflicts) as info:
            member_templates.merge_role_update(dict(mine), {"role": "R"}, deltas, {})
        assert set(info.value.fields) == {FID("spec.strict"), FID("spec.retries")}
        assert set(info.value.labels) == {"spec.strict", "spec.retries"}

    def test_both_sides_agreeing_needs_no_decision(self):
        base = {"agent": {"prompt": "p0"}, "card": {"role": "R", "triggers": ""}}
        theirs = self._theirs({"prompt": "p1"}, role="R", triggers="")
        deltas = member_templates.plan_role_update(base, {"prompt": "p1"}, {"role": "R"}, theirs)
        states = {d.field: d.state for d in deltas}
        assert states["spec.prompt"] == member_templates.AGREE
        assert not member_templates.needs_update(deltas)

    def test_merge_refuses_an_unresolved_conflict_before_deciding_anything(self):
        base = {"agent": {"prompt": "p0", "model": "m0"}, "card": {"role": "R", "triggers": ""}}
        mine = {"name": "me", "prompt": "p0", "model": "mine"}
        theirs = self._theirs({"prompt": "p1", "model": "theirs"}, role="R", triggers="")
        deltas = member_templates.plan_role_update(base, mine, {"role": "R"}, theirs)
        with pytest.raises(member_templates.UnresolvedConflicts) as info:
            member_templates.merge_role_update(mine, {"role": "R"}, deltas, {})
        assert info.value.fields == [FID("spec.model")]
        assert info.value.labels == ["spec.model"]
        # A resolution for a field NOT in conflict is ignored: the plan decides.
        spec, card = member_templates.merge_role_update(
            mine, {"role": "R"}, deltas, {FID("spec.model"): "mine", FID("spec.prompt"): "mine"}
        )
        assert spec == {"name": "me", "prompt": "p1", "model": "mine"}
        assert card == {"role": "R", "triggers": ""}
        spec, _ = member_templates.merge_role_update(
            mine, {"role": "R"}, deltas, {FID("spec.model"): "theirs"}
        )
        assert spec["model"] == "theirs"
        # The field NAME is not a handle: a resolution keyed by it resolves nothing.
        with pytest.raises(member_templates.UnresolvedConflicts):
            member_templates.merge_role_update(mine, {"role": "R"}, deltas, {"spec.model": "mine"})

    def test_deeply_nested_values_compare_without_recursing(self):
        """``json.loads`` accepts nesting far past the interpreter's recursion
        limit; a recursive compare would raise ``RecursionError`` out of the
        plan and turn a legal template into a 500. The compare is iterative."""
        import sys

        depth = sys.getrecursionlimit() + 500
        deep_a: Any = 1
        deep_b: Any = 1
        for _ in range(depth):
            deep_a = [deep_a]
            deep_b = [deep_b]
        assert member_templates.json_equal(deep_a, deep_b) is True
        deep_b_inner = deep_b
        for _ in range(depth - 1):
            deep_b_inner = deep_b_inner[0]
        deep_b_inner[0] = 2
        assert member_templates.json_equal(deep_a, deep_b) is False
        nested = {"k": deep_a}
        assert member_templates.json_equal(nested, {"k": deep_a}) is True
        base = {"agent": {"prompt": "p", "tree": deep_a}, "card": {"role": "R", "triggers": ""}}
        theirs = self._theirs({"prompt": "p", "tree": deep_b}, role="R", triggers="")
        deltas = member_templates.plan_role_update(
            base, {"prompt": "p", "tree": deep_a}, {"role": "R"}, theirs
        )
        assert {d.field: d.state for d in deltas}["spec.tree"] == member_templates.APPLY

    def test_the_plan_redactor_rebuilds_deep_values_without_recursing(self):
        """The wire redaction walks the same values the compare does, so it has
        the same obligation: a legal file nested past the recursion limit must
        not turn the plan GET into a 500. Leaves and string KEYS are redacted,
        shapes and order are kept, and the input is left untouched."""
        import sys

        from kiro_crew.dashboard.handlers.members import _redact_plan_leaves

        depth = sys.getrecursionlimit() + 500
        deep: Any = "AKIAIOSFODNN7EXAMPLE"
        for _ in range(depth):
            deep = [deep]
        out = _redact_plan_leaves({"tree": deep, "n": 1, "flag": True})
        assert isinstance(out, dict) and out["n"] == 1 and out["flag"] is True
        inner = out["tree"]
        for _ in range(depth):
            assert isinstance(inner, list) and len(inner) == 1
            inner = inner[0]
        assert isinstance(inner, str) and "AKIAIOSFODNN7EXAMPLE" not in inner
        # Order and keys are preserved; a credential-shaped KEY is redacted too.
        src = {"b": [1, {"AKIAIOSFODNN7EXAMPLE": "x"}], "a": {"z": "s", "y": None}}
        out2 = _redact_plan_leaves(src)
        assert isinstance(out2, dict) and list(out2) == ["b", "a"]
        assert list(out2["a"]) == ["z", "y"] and out2["a"]["y"] is None
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(out2)
        # The input is not mutated.
        assert "AKIAIOSFODNN7EXAMPLE" in json.dumps(src)

    def test_a_key_the_template_removed_is_removed_when_it_applies(self):
        base = {"agent": {"prompt": "p0", "legacy": 1}, "card": {"role": "R", "triggers": ""}}
        mine = {"name": "me", "prompt": "p0", "legacy": 1}
        theirs = self._theirs({"prompt": "p0"}, role="R", triggers="")
        deltas = member_templates.plan_role_update(base, mine, {"role": "R"}, theirs)
        spec, _ = member_templates.merge_role_update(mine, {"role": "R"}, deltas, {})
        assert "legacy" not in spec
        # A key set to null is a VALUE, distinct from a removed key.
        base2 = {"agent": {"x": None}, "card": {"role": "R", "triggers": ""}}
        deltas2 = member_templates.plan_role_update(
            base2, {"x": None}, {"role": "R"}, self._theirs({}, role="R", triggers="")
        )
        assert {d.field: d.state for d in deltas2}["spec.x"] == member_templates.APPLY


class TestRoleUpdateRoutes:
    @pytest.mark.asyncio
    async def test_a_lone_surrogate_in_the_member_spec_still_plans(
        self, agents_dir: Path, store_app: Path
    ):
        """The fingerprints are over an ASCII-escaped serialization: a member
        spec carrying a lone surrogate (``\\ud800`` -- json.loads accepts it,
        ``str.encode`` does not) fingerprints instead of turning the plan into
        a 500, and the apply pinned to that fingerprint goes through."""
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            mine = _member_spec(agents_dir)
            mine["description"] = "triage \ud800 pager"
            _write_member_spec(agents_dir, mine)
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 200, await resp.text()
            plan = await resp.json()
            assert plan["member_fingerprint"] and plan["template_fingerprint"]
            resp = await _apply(
                client,
                {
                    "expected_version": "1.2.0",
                    "member_fingerprint": plan["member_fingerprint"],
                    "template_fingerprint": plan["template_fingerprint"],
                },
            )
            assert resp.status == 200, await resp.text()
        assert _member_spec(agents_dir)["description"] == "triage \ud800 pager"
        # The same escape in the TEMPLATE: the apply's pristine-copy publication
        # serializes it back to its escape instead of failing the write after
        # the row committed (a 500 with the base left behind for good).
        from kiro_crew import member_templates

        theirs = _shipped(store_app)
        theirs["description"] = "shipped \ud800 triage"
        _publish(store_app, agents_dir, "1.3.0", spec=theirs)
        async with TestClient(TestServer(_app())) as client:
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["update_available"] is True
            resp = await _apply(
                client,
                {
                    "expected_version": "1.3.0",
                    "resolutions": {FID("spec.description"): "theirs"},
                    "member_fingerprint": plan["member_fingerprint"],
                    "template_fingerprint": plan["template_fingerprint"],
                },
            )
            assert resp.status == 200, await resp.text()
        row = KiroCrewConfig.load().agents[MEMBER]
        pristine = member_templates.read_pristine_copy(MEMBER, generation=row.memory_store)
        assert pristine["version"] == "1.3.0"
        assert pristine["agent"]["description"] == "shipped \ud800 triage"

    @pytest.mark.asyncio
    async def test_gate_update_a_template_one_member_customized_and_the_customization_survives(
        self, agents_dir: Path, store_app: Path
    ):
        """The step-4 gate. The member customized its prompt and added a tool;
        the template's new version changes the prompt too, adds triggers and a
        hook. The member's tool survives, the hook and triggers apply, the
        prompt conflict is resolved the member's way; version and pristine
        copy advance; lived state is untouched."""
        from kiro_crew import members

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            # Up to date right after the hire.
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["update_available"] is False
            assert plan["installed_version"] == plan["member_version"] == "1.2.0"
            assert all(f["state"] == "unchanged" for f in plan["fields"])

            # MINE: the member customized its prompt and added a tool.
            mine = _member_spec(agents_dir)
            mine["prompt"] = "You triage incidents for the PAGER team."
            mine["tools"] = ["ReadFile", "Grep"]
            _write_member_spec(agents_dir, mine)
            slug = members.slug_for_name(MEMBER)
            briefing = members.member_briefing_path(slug)
            briefing_before = briefing.read_text() if briefing.exists() else None

            # THEIRS: v1.3.0 changes the prompt, adds a hook, widens the triggers.
            theirs = _shipped(store_app)
            theirs["prompt"] = "You triage incidents. Escalate after 15 minutes."
            theirs["hooks"] = {"agentSpawn": [{"command": "echo hi"}]}
            _publish(
                store_app,
                agents_dir,
                "1.3.0",
                spec=theirs,
                card={"triggers": "incident, prod outage, sev2"},
            )

            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["update_available"] is True
            assert plan["installed_version"] == "1.3.0" and plan["member_version"] == "1.2.0"
            states = {f["field"]: f["state"] for f in plan["fields"]}
            assert states["spec.prompt"] == "conflict"
            assert states["spec.tools"] == "keep"
            assert states["spec.hooks"] == "apply"
            assert states["card.triggers"] == "apply"
            assert states["card.role"] == "unchanged"
            prompt = next(f for f in plan["fields"] if f["field"] == "spec.prompt")
            assert prompt["base"] == "You triage incidents."
            assert prompt["mine"] == "You triage incidents for the PAGER team."
            assert prompt["theirs"] == "You triage incidents. Escalate after 15 minutes."

            # Without a resolution for the conflict nothing is applied.
            resp = await _apply(client, {"expected_version": "1.3.0"})
            assert resp.status == 409
            assert (await resp.json()) == {
                "error": "every conflicting field needs a resolution",
                "code": "unresolved_conflicts",
                "fields": [FID("spec.prompt")],
                "labels": ["spec.prompt"],
            }
            assert _member_spec(agents_dir)["prompt"] == "You triage incidents for the PAGER team."

            resp = await _apply(
                client, {"resolutions": {FID("spec.prompt"): "mine"}, "expected_version": "1.3.0"}
            )
            assert resp.status == 200, await resp.text()
            assert (await resp.json()) == {"ok": True}

            roster = {
                r["name"]: r for r in (await (await client.get("/api/members")).json())["members"]
            }
        after = _member_spec(agents_dir)
        # The customization survived; the template's additions landed.
        assert after["prompt"] == "You triage incidents for the PAGER team."
        assert after["tools"] == ["ReadFile", "Grep"]
        assert after["hooks"] == {"agentSpawn": [{"command": "echo hi"}]}
        assert after["name"] == MEMBER
        row = KiroCrewConfig.load().agents[MEMBER]
        assert row.template_version == "1.3.0"
        assert row.triggers == "incident, prod outage, sev2"
        assert row.role == "Oncall Triage Engineer"
        assert roster[MEMBER]["template_version"] == "1.3.0"
        # The pristine copy advanced to the new BASE.
        pristine = member_templates.read_pristine_copy(MEMBER, generation=row.memory_store)
        assert pristine["version"] == "1.3.0"
        assert pristine["agent"]["prompt"] == "You triage incidents. Escalate after 15 minutes."
        assert pristine["card"]["triggers"] == "incident, prod outage, sev2"
        # Lived state untouched; the app's own files untouched.
        assert (briefing.read_text() if briefing.exists() else None) == briefing_before
        assert _shipped(store_app)["prompt"] == "You triage incidents. Escalate after 15 minutes."
        # And the member is now up to date; a second apply is a no-op.
        async with TestClient(TestServer(_app())) as client:
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["update_available"] is False
            assert {f["field"]: f["state"] for f in plan["fields"]}["spec.prompt"] == "keep"
            resp = await _apply(client, {"expected_version": "1.3.0"})
            assert resp.status == 200
        assert _member_spec(agents_dir) == after

    @pytest.mark.asyncio
    async def test_the_plan_echoes_no_credential_from_either_side(
        self, agents_dir: Path, store_app: Path
    ):
        """The plan carries each side's VALUE for the review dialog. Agent-file
        fields can hold a token (an ``mcpServers`` env, a URL with a secret in
        its query) -- the file is hand-editable and agent-writable -- and the
        template side is a third party's text. Every string leaf and key is
        run through the credential / exfiltration-URL redactors before it
        leaves; the fingerprints stay over the real values, so the redaction
        never changes what an apply is checked against."""
        secret = "AKIAIOSFODNN7EXAMPLE"
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            mine = _member_spec(agents_dir)
            mine["mcpServers"] = {
                "pager": {"command": "pager-mcp", "env": {"PAGER_TOKEN": secret}},
                secret: {"command": "x"},
            }
            mine["tools"] = ["ReadFile", f"https://evil.example/collect?token={secret}"]
            _write_member_spec(agents_dir, mine)
            theirs = _shipped(store_app)
            theirs["prompt"] = f"You triage incidents. Report to https://x.example/?key={secret}"
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)

            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            fields = {f["field"]: f for f in plan["fields"]}
            assert fields["spec.mcpServers"]["state"] == "keep"
            assert fields["spec.prompt"]["state"] == "apply"
            assert secret not in json.dumps(plan)
            # Shapes survive the scrub: the dialog renders by type.
            mcp = fields["spec.mcpServers"]["mine"]
            assert isinstance(mcp, dict) and "pager" in mcp
            assert mcp["pager"]["command"] == "pager-mcp"
            assert isinstance(fields["spec.tools"]["mine"], list)
            assert fields["spec.tools"]["mine"][0] == "ReadFile"
            # The fingerprint is over the REAL member file: the apply, which
            # re-reads the file and recomputes it, still matches this plan.
            resp = await _apply(client, {"expected_version": "1.3.0"})
            assert resp.status == 200, await resp.text()
        assert _member_spec(agents_dir)["mcpServers"]["pager"]["env"]["PAGER_TOKEN"] == secret

    @pytest.mark.asyncio
    async def test_the_bridges_plumbing_is_not_read_as_the_members_customization(
        self, agents_dir: Path, store_app: Path
    ):
        """The hire copies the MATERIALIZED file -- shipped spec plus the app
        bridge's own MCP servers and managed refs. BASE and THEIRS are that
        form too, so the plumbing is `unchanged`, never `keep`."""

        shipped = _shipped(store_app)
        plumbed = dict(shipped, mcpServers={"kirocrew-core": {"command": "kirocrew", "args": []}})
        (agents_dir / f"{APP}--triage.json").write_text(json.dumps(plumbed), encoding="utf-8")
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            generation = KiroCrewConfig.load().agents[MEMBER].memory_store
            pristine = member_templates.read_pristine_copy(MEMBER, generation=generation)
            assert pristine["agent"]["mcpServers"] == plumbed["mcpServers"]
            assert _member_spec(agents_dir)["mcpServers"] == plumbed["mcpServers"]
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            states = {f["field"]: f["state"] for f in plan["fields"]}
            assert states["spec.mcpServers"] == "unchanged"
            assert plan["update_available"] is False
            # The bridge re-plumbs on an app update: that reads as the template's change.
            replumbed = dict(
                plumbed, mcpServers={"kirocrew-core": {"command": "kirocrew", "args": ["mcp-core"]}}
            )
            _publish(store_app, agents_dir, "1.3.0", spec=shipped)
            (agents_dir / f"{APP}--triage.json").write_text(json.dumps(replumbed), encoding="utf-8")
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert {f["field"]: f["state"] for f in plan["fields"]}["spec.mcpServers"] == "apply"

    @pytest.mark.asyncio
    async def test_a_template_that_moved_since_the_plan_is_refused(
        self, agents_dir: Path, store_app: Path
    ):
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _publish(store_app, agents_dir, "1.3.0", card={"triggers": "sev1"})
            resp = await _apply(client, {"expected_version": "1.2.9"})
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "template_changed"
            assert body["installed_version"] == "1.3.0"
        assert KiroCrewConfig.load().agents[MEMBER].triggers == "incident, prod outage"

    @pytest.mark.asyncio
    async def test_bad_bodies_and_unlinked_members_are_refused(
        self, agents_dir: Path, store_app: Path
    ):
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            for body, code in (
                ({"resolutions": ["spec.prompt"]}, "invalid_resolutions"),
                ({"resolutions": {FID("spec.prompt"): "yours"}}, "invalid_resolutions"),
                ({"expected_version": 1}, "invalid_expected_version"),
                # Required: an apply names the version its plan was made against.
                ({"resolutions": {}}, "invalid_expected_version"),
                # And the digest of the member the plan was made about.
                ({"expected_version": "1.2.0"}, "invalid_member_fingerprint"),
                (
                    {"expected_version": "1.2.0", "member_fingerprint": 7},
                    "invalid_member_fingerprint",
                ),
                # And the digest of the template body the plan was made against.
                (
                    {"expected_version": "1.2.0", "member_fingerprint": "x"},
                    "invalid_template_fingerprint",
                ),
                (
                    {
                        "expected_version": "1.2.0",
                        "member_fingerprint": "x",
                        "template_fingerprint": "",
                    },
                    "invalid_template_fingerprint",
                ),
                ([], "body_not_object"),
            ):
                resp = await client.post(f"/api/members/{MEMBER}/role-update", json=body)
                assert resp.status == 400, (body, await resp.text())
                assert (await resp.json())["code"] == code
            resp = await client.get("/api/members/default/role-update")
            assert resp.status == 409
            assert (await resp.json())["code"] == "not_linked"
            resp = await client.get("/api/members/nobody/role-update")
            assert resp.status == 404
            resp = await client.post("/api/members/default/detach")
            assert resp.status == 409
            assert (await resp.json())["code"] == "not_linked"

    @pytest.mark.asyncio
    async def test_an_unavailable_template_is_reported_not_merged(
        self, agents_dir: Path, store_app: Path
    ):
        from kiro_crew.apps.manager import InstalledApp, _write_installed

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _write_installed(APP, InstalledApp(name=APP, version="1.2.0", enabled=False))
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 409
            assert (await resp.json())["code"] == "app_disabled"
            import shutil

            shutil.rmtree(store_app)
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 404
            assert (await resp.json())["code"] == "app_not_installed"

    @pytest.mark.asyncio
    async def test_two_cards_whose_agents_share_a_name_are_ambiguous_not_first_wins(
        self, agents_dir: Path, store_app: Path
    ):
        """The stored ref is ``<app>/<agent name>``. An app rewritten after
        install so two cards' agents register under that name is refused
        (409 ``template_ambiguous``) rather than resolved by card order; a
        banned app surfaces its own verdict, never ``template_not_offered``."""
        from kiro_crew.apps.manager import APP_MANIFEST_FILENAME

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            m = json.loads((store_app / APP_MANIFEST_FILENAME).read_text())
            (store_app / "agents" / "scribe.json").write_text(
                json.dumps(dict(_shipped(store_app), name="triage", prompt="the other one"))
            )
            m["agents"].append("agents/scribe.json")
            m["crew"]["templates"].append(
                {"agent": "agents/scribe.json", "role": "Scribe", "triggers": ""}
            )
            (store_app / APP_MANIFEST_FILENAME).write_text(json.dumps(m))
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "template_ambiguous"
            resp = await _apply(client, {"expected_version": "1.2.0"}, fingerprint="x", theirs="y")
            assert resp.status == 409
            assert (await resp.json())["code"] == "template_ambiguous"
        assert _member_spec(agents_dir)["prompt"] == "You triage incidents."
        with patch(
            "kiro_crew.member_templates.resolve_store_template",
            side_effect=member_templates.TemplateUnavailable(
                "app_admission_denied", "banned", status=409
            ),
        ):
            with pytest.raises(member_templates.TemplateUnavailable) as exc:
                member_templates.resolve_template_ref(f"{APP}/triage")
            assert exc.value.code == "app_admission_denied"

    @pytest.mark.asyncio
    async def test_the_named_cards_own_refusal_is_the_answer_not_template_not_offered(
        self, agents_dir: Path, store_app: Path
    ):
        """A ref names an agent the app DOES offer; when that card's own resolve
        refuses (its materialized file gone, its spec unreadable, its bytes
        tampered) the refusal is the verdict -- reported as such, never hidden
        behind ``template_not_offered`` because the search moved on to other
        cards. Another card's trouble is still skipped."""
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            # The named card's materialized file vanishes: its own code surfaces.
            (agents_dir / f"{APP}--triage.json").unlink()
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "template_not_materialized"
        # A refusal from a card that is NOT the named one is skipped; a truly
        # unnamed agent is still `template_not_offered`.
        real = member_templates.resolve_store_template

        def other_card_broken(app, agent_path):
            if agent_path.endswith("scribe.json"):
                raise member_templates.TemplateUnavailable(
                    "template_spec_unreadable", "x", status=409
                )
            return real(app, agent_path)

        with patch("kiro_crew.member_templates.resolve_store_template", other_card_broken):
            with pytest.raises(member_templates.TemplateUnavailable) as exc:
                member_templates.resolve_template_ref(f"{APP}/nobody")
            assert exc.value.code == "template_not_offered"

    @pytest.mark.asyncio
    async def test_a_missing_or_foreign_pristine_copy_has_no_base(
        self, agents_dir: Path, store_app: Path
    ):
        """The BASE lives in a gateway-only leaf of the data home, keyed by the
        immutable member id (never the lossy slug), and names the member and
        its store generation: a copy for another template, another member, or
        an earlier same-id member is not this member's base."""
        from kiro_crew import members
        from kiro_crew.config.paths import data_home

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            path = member_templates.pristine_copy_path(MEMBER)
            assert path.parent == member_templates.pristine_copies_root().resolve()
            assert path.parent.parent == data_home().resolve()
            assert path.parent.name == member_templates.PRISTINE_COPIES_DIR_NAME
            # Not in the agent-writable member directory, and not slug-keyed.
            assert not (
                members.member_dir(members.slug_for_name(MEMBER)) / "template.json"
            ).exists()
            good = json.loads(path.read_text())
            generation = KiroCrewConfig.load().agents[MEMBER].memory_store
            assert good["member"] == MEMBER and good["generation"] == generation
            path.unlink()
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 409
            assert (await resp.json())["code"] == "pristine_copy_missing"
            # A pristine copy naming ANOTHER template is not this member's base.
            path.write_text(json.dumps(dict(good, template="other/agent")))
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 409
            assert (await resp.json())["code"] == "pristine_copy_missing"
            # Nor one written for a same-id member of an EARLIER generation
            # (deleted and re-hired), nor one stamped with another member's id.
            path.write_text(json.dumps(dict(good, generation="member-pager-triage-old")))
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert (await resp.json())["code"] == "pristine_copy_missing"
            path.write_text(json.dumps(dict(good, member="pager-triage")))
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert (await resp.json())["code"] == "pristine_copy_missing"
            # The real one reads again.
            path.write_text(json.dumps(good))
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_a_pristine_copy_of_a_spec_near_the_cap_still_reads(
        self, agents_dir: Path, store_app: Path
    ):
        """The pristine copy wraps the materialized spec in an envelope and is
        written pretty-printed with ``ensure_ascii``, so a spec that passed
        ``TEMPLATE_SPEC_MAX_BYTES`` compactly comes back several times larger.
        The read cap is the copy's own (``PRISTINE_COPY_MAX_BYTES``), not the
        spec's: a member whose template was near the cap still has its base."""
        theirs = _shipped(store_app)
        # A compact spec just under the cap: deeply nested small tokens, the
        # shape that expands the most under indentation.
        filler = {"k%d" % i: [{"a": 1, "b": [2, 3]}] * 8 for i in range(1)}
        while (
            len(json.dumps(dict(theirs, filler=filler)))
            < member_templates.TEMPLATE_SPEC_MAX_BYTES - 4096
        ):
            filler["k%d" % len(filler)] = [{"a": 1, "b": [2, 3]}] * 8
        theirs["filler"] = filler
        assert len(json.dumps(theirs)) < member_templates.TEMPLATE_SPEC_MAX_BYTES
        _publish(store_app, agents_dir, "1.2.0", spec=theirs)
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            path = member_templates.pristine_copy_path(MEMBER)
            written = path.stat().st_size
            assert written > member_templates.TEMPLATE_SPEC_MAX_BYTES  # the expansion is real
            assert written <= member_templates.PRISTINE_COPY_MAX_BYTES
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["update_available"] is False

    def test_the_base_is_hidden_from_sandboxed_processes_and_agent_file_tools(self):
        """A forged BASE makes the merge skip a template change or overwrite the
        member's customizations silently, so nothing an agent runs may write
        one. Three fences, pinned by the SAME constant the writer roots at so a
        rename cannot silently unmask it: the sandbox bind-mask (every leaf
        under the data home is writable in-sandbox unless
        ``_CREW_HIDDEN_LEAVES`` names it -- ``trust/``, where the base once
        lived, is sandbox-VISIBLE), pre-creation before each spawn (the mask
        loop is guarded on ``isdir`` and the root is built by the first store
        hire), and the agent file-tool gate, which is what holds on Windows and
        macOS where there is no bind-mask."""
        from kiro_crew import sandbox
        from kiro_crew.security import paths as security_paths

        leaf = member_templates.PRISTINE_COPIES_DIR_NAME
        assert "/" not in leaf
        assert leaf in sandbox._CREW_HIDDEN_LEAVES
        assert leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert leaf in security_paths._CREW_SECRET_LEAVES
        assert leaf not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES
        assert member_templates.pristine_copies_root().name == leaf
        assert member_templates.pristine_copies_root().parent.name != "trust"

    @pytest.mark.asyncio
    async def test_the_base_moves_only_after_the_rows_commit_landed(
        self, agents_dir: Path, store_app: Path
    ):
        """The pristine copy is the row's provenance made concrete, so it
        advances (apply), is removed (detach) and is first written (hire)
        only AFTER the config rename succeeded -- inside the same lock hold,
        so a same-id rehire cannot slip between the row and its base. A
        rename that fails leaves the base exactly where the row still is:
        no BASE ahead of a row that never advanced, no linked row without a
        base, no orphaned base after a detach the row never saw."""
        from kiro_crew.config import loader
        from kiro_crew.dashboard.handlers.members import _detach_member

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            path = member_templates.pristine_copy_path(MEMBER)
            before = json.loads(path.read_text())
            _publish(store_app, agents_dir, "1.3.0", card={"triggers": "sev1"})
            # Apply: the config rename fails after the mutation ran.
            with patch.object(loader, "write_config_atomically", side_effect=OSError("disk full")):
                resp = await _apply(client, {"expected_version": "1.3.0"})
                assert resp.status == 500
            row = KiroCrewConfig.load().agents[MEMBER]
            assert row.template_version == "1.2.0"
            assert json.loads(path.read_text()) == before, "the base advanced without its row"
            # The plan still offers the update, and applying for real lands both.
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["update_available"] is True
            resp = await _apply(client, {"expected_version": "1.3.0"})
            assert resp.status == 200, await resp.text()
            assert KiroCrewConfig.load().agents[MEMBER].template_version == "1.3.0"
            assert json.loads(path.read_text())["version"] == "1.3.0"
        # Detach: the rename fails; the row stays linked and keeps its base.
        row = KiroCrewConfig.load().agents[MEMBER]
        with patch.object(loader, "write_config_atomically", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                _detach_member(MEMBER, row.memory_store, row.template)
        assert KiroCrewConfig.load().agents[MEMBER].template == row.template
        assert path.exists()
        resp = _detach_member(MEMBER, row.memory_store, row.template)
        assert resp.status == 200
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_a_hire_whose_link_cannot_commit_writes_no_base(
        self, agents_dir: Path, store_app: Path
    ):
        """Step 4 of the store hire records the link and its base in that
        order: a rename that fails rolls the hire back, and no base is left
        claiming a provenance no row ever got."""
        from kiro_crew.config import loader

        real = loader.write_config_atomically
        calls: list[int] = []

        def _fail_the_link(path, data, **kwargs):
            # The create's own publish goes through; the link's commit fails.
            agents = data.get("agents", {}) if isinstance(data, dict) else {}
            if isinstance(agents.get(MEMBER), dict) and agents[MEMBER].get("template"):
                calls.append(1)
                raise OSError("disk full")
            return real(path, data, **kwargs)

        async with TestClient(TestServer(_app())) as client:
            with patch.object(loader, "write_config_atomically", side_effect=_fail_the_link):
                resp = await client.post("/api/members", json=_store_hire("Pager triage"))
            assert calls, "the link's commit did not run"
            assert resp.status != 200
            assert MEMBER not in KiroCrewConfig.load().agents
            assert not member_templates.pristine_copy_path(MEMBER).exists()

    def test_the_pristine_copy_path_is_keyed_by_a_validated_member_id(self, agents_dir: Path):
        from kiro_crew import members

        for bad in ("../evil", "a/b", "", ".", "x" * 70):
            with pytest.raises(members.MemberSlugError):
                member_templates.pristine_copy_path(bad)
        # Two ids that share one slug get two files.
        assert member_templates.pristine_copy_path(
            "Pager-triage"
        ) != member_templates.pristine_copy_path("pager-triage")

    @pytest.mark.asyncio
    async def test_a_member_bound_to_a_shared_file_is_never_rewritten(
        self, agents_dir: Path, store_app: Path
    ):
        """The update rewrites the member's agent file, so it must be the copy
        the hire made for this member -- a hand-edited row pointing a member at
        the app's materialized file would otherwise have the update rewrite a
        file every other member of that template shares."""
        from kiro_crew.config.loader import update_config_locked

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)

            def rebind(doc):
                doc["agents"][MEMBER]["kiro_agent"] = f"{APP}--triage"
                return doc

            update_config_locked(mutate=rebind)
            _publish(store_app, agents_dir, "1.3.0", card={"triggers": "sev1"})
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 409
            assert (await resp.json())["code"] == "not_private_copy"
        assert json.loads((agents_dir / f"{APP}--triage.json").read_text())["name"] == "triage"

    @pytest.mark.asyncio
    async def test_a_malformed_binding_is_not_private_copy_not_a_500(
        self, agents_dir: Path, store_app: Path
    ):
        """The binding is free text in a hand-editable, agent-writable config. A
        non-string (``kiro_agent: []``) or a name outside the agent grammar is
        answered as "not bound to its own copy" by the plan and the apply alike
        -- never handed to the sidecar lookup, where an unhashable key is a
        500 (detach never reads the binding)."""
        from kiro_crew.config.loader import update_config_locked

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            for bad in ([], {"x": 1}, 7, "", "../evil", "has space"):

                def rebind(doc, bad=bad):
                    doc["agents"][MEMBER]["kiro_agent"] = bad
                    return doc

                update_config_locked(mutate=rebind)
                resp = await client.get(f"/api/members/{MEMBER}/role-update")
                assert resp.status == 409, (bad, await resp.text())
                assert (await resp.json())["code"] == "not_private_copy"
                resp = await _apply(
                    client, {"expected_version": "1.2.0"}, fingerprint="x", theirs="y"
                )
                assert resp.status == 409, (bad, await resp.text())
                assert (await resp.json())["code"] == "not_private_copy"

    @pytest.mark.asyncio
    async def test_a_governance_withheld_grant_in_the_template_does_not_reach_the_member(
        self, agents_dir: Path, store_app: Path
    ):
        """The merged definition goes through the same whole-config governance
        funnel every spec writer uses before it is persisted."""
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            theirs = _shipped(store_app)
            theirs["allowedTools"] = ["execute_bash"]
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)
            with patch(
                "kiro_crew.agent.sanitize_agent_config_governance",
                side_effect=lambda cfg: cfg.pop("allowedTools", None),
            ) as funnel:
                resp = await _apply(client, {"expected_version": "1.3.0"})
                assert resp.status == 200, await resp.text()
            assert funnel.called
        assert "allowedTools" not in _member_spec(agents_dir)

    @pytest.mark.asyncio
    async def test_the_members_definition_is_written_through_the_one_member_writer(
        self, agents_dir: Path, store_app: Path
    ):
        """``agent.write_agent_definition`` is the one writer of a WHOLE agent
        definition a merge or a patch produced -- the editor's PATCH overwrite
        lands through it too -- so the apply must reach the disk through it and
        nowhere else."""
        from kiro_crew.dashboard.handlers import members as members_handlers

        written: list[tuple[Path, dict]] = []
        real = members_handlers.write_agent_definition

        def observing(path: Path, spec: dict) -> None:
            written.append((path, dict(spec)))
            real(path, spec)

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            theirs = _shipped(store_app)
            theirs["prompt"] = "theirs"
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)
            with patch.object(members_handlers, "write_agent_definition", observing):
                resp = await _apply(client, {"expected_version": "1.3.0"})
                assert resp.status == 200, await resp.text()
        assert len(written) == 1
        path, spec = written[0]
        assert path == agents_dir / f"{MEMBER}.json"
        assert spec["prompt"] == "theirs"
        assert _member_spec(agents_dir) == json.loads(path.read_text(encoding="utf-8"))

    @pytest.mark.asyncio
    async def test_a_conflict_resolved_the_templates_way_takes_theirs(
        self, agents_dir: Path, store_app: Path
    ):
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            mine = _member_spec(agents_dir)
            mine["prompt"] = "mine"
            _write_member_spec(agents_dir, mine)
            theirs = _shipped(store_app)
            theirs["prompt"] = "theirs"
            _publish(store_app, agents_dir, "1.3.0", spec=theirs, card={"role": "Incident Lead"})
            resp = await _apply(
                client, {"resolutions": {FID("spec.prompt"): "theirs"}, "expected_version": "1.3.0"}
            )
            assert resp.status == 200, await resp.text()
        assert _member_spec(agents_dir)["prompt"] == "theirs"
        assert KiroCrewConfig.load().agents[MEMBER].role == "Incident Lead"

    @pytest.mark.asyncio
    async def test_a_member_whose_role_was_renamed_keeps_it_when_the_card_did_not_move(
        self, agents_dir: Path, store_app: Path
    ):
        """Card fields merge like definition fields: a role the user changed is
        MINE; the template's unchanged role does not overwrite it."""
        from kiro_crew.config.loader import update_config_locked

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)

            def rename(doc):
                doc["agents"][MEMBER]["role"] = "Pager captain"
                return doc

            update_config_locked(mutate=rename)
            _publish(store_app, agents_dir, "1.3.0", card={"triggers": "sev1"})
            resp = await _apply(client, {"expected_version": "1.3.0"})
            assert resp.status == 200, await resp.text()
        row = KiroCrewConfig.load().agents[MEMBER]
        assert row.role == "Pager captain" and row.triggers == "sev1"

    @pytest.mark.asyncio
    async def test_a_member_edited_since_the_plan_is_refused(
        self, agents_dir: Path, store_app: Path
    ):
        """A plan is a decision about the member AS REVIEWED. The apply carries
        the plan's digest of MINE back; a member whose definition or card moved
        in between (the crew editor, a rename) is refused, so a stale
        ``theirs`` choice never overwrites a customization nobody saw."""
        from kiro_crew.config.loader import update_config_locked

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            theirs = _shipped(store_app)
            theirs["prompt"] = "theirs"
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            stale = plan["member_fingerprint"]
            # The user rewrites the prompt after reviewing the plan.
            mine = _member_spec(agents_dir)
            mine["prompt"] = "rewritten after the plan"
            _write_member_spec(agents_dir, mine)
            resp = await _apply(
                client,
                {"resolutions": {FID("spec.prompt"): "theirs"}, "expected_version": "1.3.0"},
                fingerprint=stale,
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "member_changed_since_plan"
            assert _member_spec(agents_dir)["prompt"] == "rewritten after the plan"
            assert KiroCrewConfig.load().agents[MEMBER].template_version == "1.2.0"
            # A card change counts too: the row is part of MINE.
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            stale = plan["member_fingerprint"]

            def rename(doc):
                doc["agents"][MEMBER]["role"] = "Pager captain"
                return doc

            update_config_locked(mutate=rename)
            resp = await _apply(
                client,
                {"resolutions": {FID("spec.prompt"): "theirs"}, "expected_version": "1.3.0"},
                fingerprint=stale,
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "member_changed_since_plan"
            # The digest is stable across re-reads of the same member, and a
            # fresh plan applies.
            fresh = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            again = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert fresh["member_fingerprint"] == again["member_fingerprint"] != stale
            resp = await _apply(
                client, {"resolutions": {FID("spec.prompt"): "theirs"}, "expected_version": "1.3.0"}
            )
            assert resp.status == 200, await resp.text()
        assert _member_spec(agents_dir)["prompt"] == "theirs"
        assert KiroCrewConfig.load().agents[MEMBER].role == "Pager captain"

    @pytest.mark.asyncio
    async def test_a_file_that_moved_between_the_plan_read_and_the_lock_is_not_overwritten(
        self, agents_dir: Path, store_app: Path
    ):
        """The plan reads the member's file before the spec lock; the write
        happens under it. A writer that lands in between -- the fork refresh
        re-materializing an MCP command, the agent editor -- must not have its
        write reverted by a whole-file write of the older merge: the file is
        re-read and re-digested under the lock, and a moved file refuses."""
        from kiro_crew.dashboard.handlers import members as members_handlers

        real_lock = members_handlers.agents_spec_lock
        landed: list[str] = []

        class _LockThenMove:
            def __init__(self, agents_dir_):
                self._inner = real_lock(agents_dir_)

            def __enter__(self):
                out = self._inner.__enter__()
                # The refresh's write, landing just as the apply takes the lock.
                if not landed:
                    mine = _member_spec(agents_dir)
                    mine["mcpServers"] = {"kirocrew-core": {"command": "/new/path/kirocrew"}}
                    _write_member_spec(agents_dir, mine)
                    landed.append("refresh")
                return out

            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            theirs = _shipped(store_app)
            theirs["prompt"] = "theirs"
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)
            with patch.object(members_handlers, "agents_spec_lock", _LockThenMove):
                resp = await _apply(
                    client,
                    {"resolutions": {FID("spec.prompt"): "theirs"}, "expected_version": "1.3.0"},
                )
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "member_changed_since_plan"
            # The refresh's write survived; nothing of the plan landed.
            spec = _member_spec(agents_dir)
            assert spec["mcpServers"]["kirocrew-core"]["command"] == "/new/path/kirocrew"
            assert spec["prompt"] != "theirs"
            assert KiroCrewConfig.load().agents[MEMBER].template_version == "1.2.0"
            # A fresh plan digests the moved file and applies.
            resp = await _apply(
                client, {"resolutions": {FID("spec.prompt"): "theirs"}, "expected_version": "1.3.0"}
            )
            assert resp.status == 200, await resp.text()
        assert _member_spec(agents_dir)["prompt"] == "theirs"

    @pytest.mark.asyncio
    async def test_a_card_edit_landing_inside_the_apply_is_not_overwritten(
        self, agents_dir: Path, store_app: Path
    ):
        """The row's role/triggers are the card half of MINE. An edit to them
        that lands after the plan's read but before the apply's locked mutation
        -- the crew editor in another process -- is re-read under the
        cross-process lock and refuses the apply; the binding and generation
        alone would not have seen it, and the merged card would have erased
        it. The agent FILE is written only after that re-check passed, inside
        the same hold, so the refusal leaves the member's own spec exactly as
        it was -- written first, a stale card's 409 would have left the
        template's prompt over the member's customization with nothing for the
        next plan to re-apply. The pristine copy stays on the old version."""
        from kiro_crew.config import loader
        from kiro_crew.dashboard.handlers import members as members_handlers

        real_locked = loader.update_config_locked
        landed: list[str] = []

        def _rename_then_apply(*args, **kwargs):
            # The other process's role edit lands as the apply reaches for the
            # config lock -- after the plan read, before the locked mutation.
            if not landed:
                landed.append("rename")

                def rename(doc):
                    doc["agents"][MEMBER]["role"] = "Pager captain"
                    return doc

                real_locked(mutate=rename)
            return real_locked(*args, **kwargs)

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            mine = _member_spec(agents_dir)
            mine["prompt"] = "mine, customized"
            _write_member_spec(agents_dir, mine)
            theirs = _shipped(store_app)
            theirs["prompt"] = "theirs"
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)
            with patch.object(members_handlers, "update_config_locked", _rename_then_apply):
                resp = await _apply(
                    client,
                    {"resolutions": {FID("spec.prompt"): "theirs"}, "expected_version": "1.3.0"},
                )
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "member_changed_since_plan"
        assert landed == ["rename"]
        row = KiroCrewConfig.load().agents[MEMBER]
        assert row.role == "Pager captain" and row.template_version == "1.2.0"
        # The member's own file was not touched: its customization survives.
        assert _member_spec(agents_dir)["prompt"] == "mine, customized"
        assert (
            json.loads(member_templates.pristine_copy_path(MEMBER).read_text())["version"]
            == "1.2.0"
        )

    @pytest.mark.asyncio
    async def test_a_pristine_copy_behind_the_row_keeps_the_update_offered(
        self, agents_dir: Path, store_app: Path
    ):
        """A pristine write that failed after the row advanced leaves BASE at
        the old version. The plan keeps the update available -- applying is
        what rewrites the base -- instead of reporting the member current with
        a stale base that would turn the next template change into false
        conflicts."""
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _publish(store_app, agents_dir, "1.3.0", card={"triggers": "sev1"})
            resp = await _apply(client, {"expected_version": "1.3.0"})
            assert resp.status == 200, await resp.text()
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["update_available"] is False
            # Roll the base back to what a torn write would have left.
            path = member_templates.pristine_copy_path(MEMBER)
            pristine = json.loads(path.read_text())
            pristine["version"] = "1.2.0"
            path.write_text(json.dumps(pristine))
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["installed_version"] == plan["member_version"] == "1.3.0"
            assert plan["update_available"] is True
            assert all(f["state"] == "unchanged" for f in plan["fields"])
            resp = await _apply(client, {"expected_version": "1.3.0"})
            assert resp.status == 200, await resp.text()
            assert json.loads(path.read_text())["version"] == "1.3.0"
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["update_available"] is False

    @pytest.mark.asyncio
    async def test_a_template_body_rewritten_under_the_same_version_is_refused(
        self, agents_dir: Path, store_app: Path
    ):
        """The version is not the anchor: the apply carries the plan's digest of
        THEIRS (materialized definition + card + version), and an app that
        re-materialized different bytes under the same version is
        ``template_changed`` -- nothing nobody reviewed is merged."""
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            theirs = _shipped(store_app)
            theirs["prompt"] = "reviewed"
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            stale = plan["template_fingerprint"]
            # The app rewrites its agent under v1.3.0 after the plan was reviewed.
            theirs["prompt"] = "rewritten, never reviewed"
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)
            resp = await _apply(
                client,
                {"resolutions": {FID("spec.prompt"): "theirs"}, "expected_version": "1.3.0"},
                theirs=stale,
            )
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "template_changed" and body["installed_version"] == "1.3.0"
            assert _member_spec(agents_dir)["prompt"] == "You triage incidents."
            # Stable across re-reads; a fresh plan applies the reviewed bytes.
            fresh = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            again = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert fresh["template_fingerprint"] == again["template_fingerprint"] != stale
            resp = await _apply(client, {"expected_version": "1.3.0"})
            assert resp.status == 200, await resp.text()
        assert _member_spec(agents_dir)["prompt"] == "rewritten, never reviewed"


class TestDetach:
    @pytest.mark.asyncio
    async def test_an_apply_planned_before_a_detach_does_not_rewrite_the_detached_member(
        self, agents_dir: Path, store_app: Path
    ):
        """The apply's in-lock re-check compares the binding, the generation and
        the card; a detach strips only ``template`` / ``template_version``. A
        detach that commits between the plan and the apply's locked mutation
        (another process, or a tab) must make the apply refuse: the member the
        owner just set free is not rewritten to the template and no base is
        published for it."""
        from kiro_crew import member_templates
        from kiro_crew.dashboard.handlers import members as handlers

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            theirs = _shipped(store_app)
            theirs["prompt"] = "You triage incidents. Escalate after 15 minutes."
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["update_available"] is True
            before = _member_spec(agents_dir)
            real = handlers.update_config_locked

            def detached_first(*, mutate, after_write=None):
                # The detach landed in the document between the plan's read and
                # this apply's locked mutation.
                def mutate_after_detach(doc):
                    row = doc["agents"][MEMBER]
                    row["template"] = ""
                    row["template_version"] = ""
                    return mutate(doc)

                return real(mutate=mutate_after_detach, after_write=after_write)

            with patch.object(handlers, "update_config_locked", detached_first):
                resp = await _apply(
                    client,
                    {
                        "expected_version": "1.3.0",
                        "member_fingerprint": plan["member_fingerprint"],
                        "template_fingerprint": plan["template_fingerprint"],
                    },
                )
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "member_changed"
        # Nothing moved: the spec is the member's, no new base (the simulated
        # detach lived in the refused mutation's document, so the stored row
        # still reads as the hire left it).
        assert _member_spec(agents_dir) == before
        row = KiroCrewConfig.load().agents[MEMBER]
        assert row.template_version == "1.2.0"
        pristine = member_templates.read_pristine_copy(MEMBER, generation=row.memory_store)
        assert pristine["version"] == "1.2.0"

    @pytest.mark.asyncio
    async def test_a_second_apply_of_one_plan_does_not_overwrite_the_first_ones_choice(
        self, agents_dir: Path, store_app: Path
    ):
        """Two processes apply ONE plan of a conflicting prompt, the first
        keeping MINE: its commit advances only ``template_version`` (the ref,
        the binding, the generation and the card are unchanged), so an in-lock
        re-check on the ref alone would let the second apply -- resolved
        THEIRS -- write over the choice the first one committed. The re-check
        compares the version too: the second apply is refused and the member's
        prompt stays as the first apply left it."""
        from kiro_crew.dashboard.handlers import members as handlers

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            mine = _member_spec(agents_dir)
            mine["prompt"] = "You triage incidents for the PAGER team."
            _write_member_spec(agents_dir, mine)
            theirs = _shipped(store_app)
            theirs["prompt"] = "You triage incidents. Escalate after 15 minutes."
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert {f["field"]: f["state"] for f in plan["fields"]}["spec.prompt"] == "conflict"
            real = handlers.update_config_locked

            def first_apply_landed(*, mutate, after_write=None):
                # The other process's apply of this plan committed first, keeping
                # MINE: the row's version advanced, nothing else moved.
                def mutate_after_first(doc):
                    doc["agents"][MEMBER]["template_version"] = "1.3.0"
                    return mutate(doc)

                return real(mutate=mutate_after_first, after_write=after_write)

            with patch.object(handlers, "update_config_locked", first_apply_landed):
                resp = await _apply(
                    client,
                    {
                        "resolutions": {FID("spec.prompt"): "theirs"},
                        "expected_version": "1.3.0",
                        "member_fingerprint": plan["member_fingerprint"],
                        "template_fingerprint": plan["template_fingerprint"],
                    },
                )
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "member_changed"
        assert _member_spec(agents_dir)["prompt"] == "You triage incidents for the PAGER team."

    @pytest.mark.asyncio
    async def test_the_plan_redacts_a_credential_shaped_template_ref(
        self, agents_dir: Path, store_app: Path
    ):
        """``template`` on the row is template-author-typed text in an
        agent-writable config: the plan ships it through the same redactor as
        every identity string the roster ships, never raw."""
        from kiro_crew.config.loader import config_path

        secret = "AKIAIOSFODNN7EXAMPLE"
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            raw = json.loads(config_path().read_text())
            raw["agents"][MEMBER]["template"] = f"{APP}/{secret}"
            config_path().write_text(json.dumps(raw))
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            body = await resp.text()
        assert secret not in body

    @pytest.mark.asyncio
    async def test_the_plan_redacts_a_credential_shaped_version_and_resolves_conflicts_by_id(
        self, agents_dir: Path, store_app: Path
    ):
        """Two more leaves an agent-writable file controls: the row's
        ``template_version`` (the plan's ``member_version``) and a top-level
        spec KEY. Both ship redacted -- and because a redacted field name is not
        a name the apply could match, every plan field carries an opaque ``id``
        the client resolves by; the unresolved-conflicts refusal names ids and
        redacted labels, never the raw key."""
        from kiro_crew.config.loader import config_path

        secret = "AKIAIOSFODNN7EXAMPLE"
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            raw = json.loads(config_path().read_text())
            raw["agents"][MEMBER]["template_version"] = f"1.2.0+{secret}"
            config_path().write_text(json.dumps(raw))
            # A credential-shaped top-level key the member added AND the
            # template ships with another value: a conflict on that key.
            mine = _member_spec(agents_dir)
            mine[secret] = "mine"
            _write_member_spec(agents_dir, mine)
            theirs = _shipped(store_app)
            theirs[secret] = "theirs"
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            body = await resp.text()
            assert resp.status == 200, body
            assert secret not in body
            plan = json.loads(body)
            conflict = next(f for f in plan["fields"] if f["state"] == "conflict")
            assert conflict["id"] == FID(
                f"spec.{secret}"
            )  # derived from the name, carrying none of it
            assert secret not in conflict["field"]
            # Unresolved: the refusal names the id and a redacted label only.
            resp = await _apply(client, {"expected_version": "1.3.0"})
            refusal = await resp.text()
            assert resp.status == 409, refusal
            assert secret not in refusal
            assert json.loads(refusal)["fields"] == [conflict["id"]]
            # Resolved by id: the apply lands.
            resp = await _apply(
                client, {"resolutions": {conflict["id"]: "theirs"}, "expected_version": "1.3.0"}
            )
            assert resp.status == 200, await resp.text()
        assert _member_spec(agents_dir)[secret] == "theirs"

    @pytest.mark.asyncio
    async def test_detach_clears_provenance_and_leaves_everything_else(
        self, agents_dir: Path, store_app: Path
    ):
        from kiro_crew import members

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            before = _member_spec(agents_dir)
            slug = members.slug_for_name(MEMBER)
            resp = await client.post(f"/api/members/{MEMBER}/detach", json={})
            assert resp.status == 200, await resp.text()
            assert (await resp.json()) == {"ok": True}
            roster = {
                r["name"]: r for r in (await (await client.get("/api/members")).json())["members"]
            }
            # Detached: no template, no update to offer.
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 409
            assert (await resp.json())["code"] == "not_linked"
            # Twice is a refusal, not a second severing.
            resp = await client.post(f"/api/members/{MEMBER}/detach", json={})
            assert resp.status == 409
        row = KiroCrewConfig.load().agents[MEMBER]
        assert row.template == "" and row.template_version == ""
        assert row.role == "Oncall Triage Engineer"
        assert row.triggers == "incident, prod outage"
        assert row.kiro_agent == MEMBER
        assert roster[MEMBER]["template"] == ""
        assert roster[MEMBER]["template_origin"] == "triage"
        assert _member_spec(agents_dir) == before
        assert not member_templates.pristine_copy_path(MEMBER).exists()
        if members.member_briefing_supported():
            assert members.member_briefing_path(slug).exists()
        assert agent_state.get_fork_info(MEMBER)["private_to"] == MEMBER

    @pytest.mark.asyncio
    async def test_detach_refuses_a_member_replaced_under_it(
        self, agents_dir: Path, store_app: Path
    ):
        """The severing re-reads the row under the lock and only touches the
        member the request validated: a same-id member of another store
        generation (deleted and re-hired) or linked to another template is
        somebody else's, and its provenance and pristine base stay."""
        from kiro_crew.dashboard.handlers.members import _detach_member

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
        row = KiroCrewConfig.load().agents[MEMBER]
        pristine = member_templates.pristine_copy_path(MEMBER)
        assert pristine.exists()
        for generation, template in (
            ("member-pager-triage-old", row.template),
            (row.memory_store, "other/agent"),
        ):
            resp = _detach_member(MEMBER, generation, template)
            assert resp.status == 409
            assert json.loads(resp.text)["code"] == "member_changed"
            after = KiroCrewConfig.load().agents[MEMBER]
            assert after.template == row.template and after.template_version == "1.2.0"
            assert pristine.exists()
        # A member removed under the lock is refused the same way.
        resp = _detach_member("nobody", row.memory_store, row.template)
        assert resp.status == 409
        # The row the request saw detaches.
        resp = _detach_member(MEMBER, row.memory_store, row.template)
        assert resp.status == 200
        assert KiroCrewConfig.load().agents[MEMBER].template == ""
        assert not pristine.exists()
