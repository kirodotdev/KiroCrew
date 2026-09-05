"""``GET /api/agents`` ships an explicit allowlist, never the whole record.

The endpoint used to build each row with ``{**dataclasses.asdict(agent_cfg)}``,
which made its response contract "every field ``KiroCrewAgentConfig`` has now,
plus every field anyone adds later", automatically — a field added by someone
who never looked at this endpoint shipped to the browser by omission. #8454
converted both row sources to an explicit allowlist, mirroring the rule
``handlers/members.py`` already documents for ``GET /api/members``.

These tests are the half that keeps it converted. The key set is pinned as a
literal, and a separate ratchet compares that literal against the live record
so a field added to ``KiroCrewAgentConfig`` fails here rather than silently
appearing in the response — which is the whole point of the change: the default
becomes "nothing unless someone adds it".
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import types
import unittest.mock
from pathlib import Path
from typing import cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.sections import KiroCrewAgentConfig, _safe_avatar
from kiro_crew.dashboard.handlers.agents import (
    _agent_roster_row,
    _carries_mask,
    _name_would_be_masked,
    _roster_mask,
)

# The EXACT key set every row carries, from BOTH sources (the ``cfg.agents``
# rows and the project-scope rows). ``name`` and ``scope`` are handler-added;
# the rest are allowlisted record fields. Changing this set is a
# network-boundary contract change: check the frontend consumers first
# (``website/src/components/AgentSelector.tsx`` declares the ``KiroCrewAgent``
# interface the dashboard reads).
ROSTER_ROW_KEYS = frozenset(
    {
        "name",
        "scope",
        "kiro_agent",
        "workspace",
        "memory_store",
        "model",
        "reasoning_effort",
        "description",
        "triggers",
        "source",
        "session_color",
        "avatar",
    }
)

# Record fields deliberately withheld, each verified to have no consumer in
# ``website/src``: the two watchdog windows are backend scheduling knobs the
# roster does not render, and ``telegram_account`` is deprecated and inert.
WITHHELD_RECORD_FIELDS = frozenset(
    {
        "watchdog_tool_stall_suspect_secs",
        "watchdog_tool_stall_hard_cap_secs",
        "telegram_account",
    }
)


def _make_app() -> web.Application:
    """Minimal aiohttp app with just the roster endpoint."""
    from kiro_crew.dashboard.handlers import api_kirocrew_agents

    app = web.Application()
    app.router.add_get("/api/agents", api_kirocrew_agents)
    return app


def _seed_config_with_every_field_set() -> dict:
    """A config whose agent sets EVERY record field, withheld ones included.

    A withheld field left at its default is indistinguishable from a withheld
    field that is absent, so the fixture gives each one a distinctive value: an
    assertion on its absence then actually proves the allowlist rather than the
    default happening to be falsy.
    """
    return {
        "agents": {
            "roster-probe": {
                "kiro_agent": "kirocrew",
                "workspace": "probe-ws",
                "memory_store": "probe-ms",
                "model": "claude-opus-5",
                "reasoning_effort": "high",
                "description": "probe description",
                "triggers": "probe triggers",
                "source": "kirocrew",
                "session_color": "#abcdef",
                # Withheld — must NOT appear in the response.
                "watchdog_tool_stall_suspect_secs": 111.0,
                "watchdog_tool_stall_hard_cap_secs": 222.0,
                "telegram_account": "probe-telegram-binding",
            },
        },
        "default_agent": "roster-probe",
        "workspaces": {"default": {"dir": "workspace"}, "probe-ws": {"dir": "workspace"}},
    }


class TestRosterRowKeySet:
    """The response's exact key set, measured at the endpoint."""

    @pytest.mark.asyncio
    async def test_global_row_ships_exactly_the_allowlist(self) -> None:
        """A ``cfg.agents`` row carries the allowlist and nothing else."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config_with_every_field_set(), f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(_make_app())) as client:
                    resp = await client.get("/api/agents")
                    assert resp.status == 200
                    row = {a["name"]: a for a in (await resp.json())["agents"]}["roster-probe"]

            assert set(row) == ROSTER_ROW_KEYS
            # The allowlisted values still arrive — an allowlist that shipped
            # the right KEYS with empty values would pass a key-set assertion
            # while breaking every consumer.
            assert row["scope"] == "global"
            assert row["workspace"] == "probe-ws"
            assert row["memory_store"] == "probe-ms"
            assert row["model"] == "claude-opus-5"
            assert row["reasoning_effort"] == "high"
            assert row["description"] == "probe description"
            assert row["triggers"] == "probe triggers"
            assert row["session_color"] == "#abcdef"
            # And the withheld fields are gone even though the config set them
            # to distinctive non-default values.
            assert not (set(row) & WITHHELD_RECORD_FIELDS)
            assert "probe-telegram-binding" not in json.dumps(row)
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_project_row_ships_the_same_key_set(self, monkeypatch) -> None:
        """A project-scope row carries the SAME keys as a global row.

        The two sources were separate spreads before #8454, so they could drift
        into different key sets; pinning both is what makes one allowlist the
        answer for the whole response.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.active_project_dir",
            lambda state, key: "/probe/project",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.project_agent_names",
            lambda project_dir, **kw: frozenset({"project-only-agent"}),
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config_with_every_field_set(), f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                app = _make_app()
                # Truthy state with no conversation log: the handler then takes
                # the project-scan branch and skips the usage re-sort.
                app["state"] = types.SimpleNamespace(conversation_log=None)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents")
                    assert resp.status == 200
                    rows = {a["name"]: a for a in (await resp.json())["agents"]}

            assert "project-only-agent" in rows, "project scan produced no row to check"
            project_row = rows["project-only-agent"]
            assert set(project_row) == ROSTER_ROW_KEYS
            assert set(project_row) == set(rows["roster-probe"])
            assert project_row["scope"] == "project"
            assert not (set(project_row) & WITHHELD_RECORD_FIELDS)
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_app_token_caller_gets_the_same_keys_with_scrubbed_values(self) -> None:
        """End to end: the caller class is resolved from the request, not passed in.

        The row-level tests cover ``redact=`` directly; this one covers the
        WIRING -- that the handler reads the caller class off the request at all.
        A middleware sets ``request["app"]``, which is what ``token_auth`` does
        for a verified app token and what ``members.py::_deny_app_caller`` reads.
        """
        probe = "AKIAIOSFODNN7EXAMPLE"
        seed = _seed_config_with_every_field_set()
        seed["agents"]["roster-probe"]["description"] = f"see {probe}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(seed, f)
            tmp = Path(f.name)
        try:

            @web.middleware
            async def _as_app(request: web.Request, handler):  # type: ignore[no-untyped-def]
                request["app"] = "probe-app"
                return await handler(request)

            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                app = web.Application(middlewares=[_as_app])
                from kiro_crew.dashboard.handlers import api_kirocrew_agents

                app.router.add_get("/api/agents", api_kirocrew_agents)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents")
                    assert resp.status == 200
                    body = await resp.json()
                    row = {a["name"]: a for a in body["agents"]}["roster-probe"]

            assert set(row) == ROSTER_ROW_KEYS, "key set must not depend on caller class"
            assert probe not in json.dumps(row), "app token received an unscrubbed value"
        finally:
            tmp.unlink(missing_ok=True)


class TestRosterRowIsAnAllowlistNotASpread:
    """The property that survives the record growing."""

    def test_record_field_added_later_is_not_shipped_by_omission(self) -> None:
        """Every ``KiroCrewAgentConfig`` field is either allowlisted or withheld.

        This is the ratchet. It fails when a field is ADDED to the record, and
        that failure is the feature: the author has to decide whether the
        browser should see it, instead of a spread deciding for them.
        """
        record_fields = {f.name for f in dataclasses.fields(KiroCrewAgentConfig)}
        unclassified = record_fields - ROSTER_ROW_KEYS - WITHHELD_RECORD_FIELDS
        assert not unclassified, (
            f"KiroCrewAgentConfig grew {sorted(unclassified)}. GET /api/agents is an "
            "explicit allowlist (#8454), so decide deliberately: add the field to "
            "_agent_roster_row AND ROSTER_ROW_KEYS if the dashboard needs it, or to "
            "WITHHELD_RECORD_FIELDS if it must not leave the process."
        )
        # The withheld set must name real fields — a typo there would silently
        # stop classifying anything and let the next added field through.
        assert WITHHELD_RECORD_FIELDS <= record_fields
        # And the allowlist must not claim a record field that no longer exists.
        assert ROSTER_ROW_KEYS - {"name", "scope"} <= record_fields

    def test_an_attribute_the_allowlist_does_not_name_is_dropped(self) -> None:
        """Behavioral proof, not just a literal comparison.

        A record carrying an extra attribute — the shape of a field added
        later — serializes to the same key set. Under the old spread this row
        would have carried ``secret_future_field``.
        """

        def _clone(f: dataclasses.Field) -> dataclasses.Field:
            """Rebuild ONE field, preserving how its default is supplied.

            A probe that REBUILDS the class it inspects can manufacture a failure
            that does not exist in the class. ``avatar`` uses
            ``default_factory=dict`` -- a mutable default must -- so its
            ``f.default`` is ``dataclasses.MISSING``; passing that straight to
            ``field(default=...)`` produces a field with NO default, and a
            non-default field after defaulted ones is a hard
            ``TypeError: non-default argument 'avatar' follows default argument``
            at class-creation time. That error was this probe's, not the record's:
            ``KiroCrewAgentConfig()`` instantiates fine on a clean tree.
            """
            if f.default_factory is not dataclasses.MISSING:
                return dataclasses.field(default_factory=f.default_factory)
            return dataclasses.field(default=f.default)

        grown = dataclasses.make_dataclass(
            "GrownAgentConfig",
            [(f.name, f.type, _clone(f)) for f in dataclasses.fields(KiroCrewAgentConfig)]
            + [("secret_future_field", str, dataclasses.field(default="must-not-ship"))],
        )
        row = _agent_roster_row("grown", "global", cast(KiroCrewAgentConfig, grown()), redact=False)
        assert set(row) == ROSTER_ROW_KEYS
        assert "secret_future_field" not in row
        assert "must-not-ship" not in json.dumps(row)


class TestUnshowableValuesAreMasked:
    """The read half: nothing that cannot be shown verbatim leaves as content."""

    PROBE = "AKIAIOSFODNN7EXAMPLE"
    UNCOERCED_BY_LOADER = ("kiro_agent", "workspace", "memory_store", "description", "source")
    # ``avatar`` is the ONE structured value a row ships, so the uniform
    # string sweeps below do not apply to it: it is shape-allowlisted by
    # ``_safe_avatar`` with masking confined to user-authored ``traits``
    # values, and is pinned separately -- in BOTH directions -- by
    # ``TestAvatarIsShapeAllowlistedNotMasked``. Excluded here rather than
    # softening these assertions, so the rule for every other field stays
    # "the sentinel, exactly".
    RECORD_FIELDS_SHIPPED = tuple(sorted(ROSTER_ROW_KEYS - {"name", "scope", "avatar"}))

    def _full(self) -> KiroCrewAgentConfig:
        return KiroCrewAgentConfig(
            kiro_agent=self.PROBE,
            workspace=f"ws-{self.PROBE}",
            memory_store=f"ms-{self.PROBE}",
            triggers=f"use when {self.PROBE}",
            model=self.PROBE,
            reasoning_effort=self.PROBE,
            session_color=self.PROBE,
            description=f"see {self.PROBE}",
            source=self.PROBE,
        )

    @pytest.mark.parametrize("redact", [False, True])
    def test_no_record_field_ships_a_credential_shaped_value(self, redact: bool) -> None:
        """Uniform across callers: an earlier revision exempted the fields the
        agents page writes back, which encoded a claim about the CLIENT that this
        side could not enforce (the PUT accepts `description` and `source` too)."""
        row = _agent_roster_row("probe", "global", self._full(), redact=redact)
        for field in self.RECORD_FIELDS_SHIPPED:
            assert self.PROBE not in row[field], f"{field} shipped unmasked"
            assert _carries_mask(row[field]), f"{field} should be the sentinel"

    @pytest.mark.parametrize("redact", [False, True])
    @pytest.mark.parametrize("field", UNCOERCED_BY_LOADER)
    def test_a_non_string_is_masked_not_emptied(self, field: str, redact: bool) -> None:
        """The loader lets an object through five declared-`str` fields.

        Masking rather than coercing to `""` is what lets the write-side rule
        PRESERVE it: an echoed `""` would read as a genuine edit and overwrite the
        stored value, which was a real defect in an earlier revision.
        """
        cfg = KiroCrewAgentConfig()
        object.__setattr__(cfg, field, {"nested": "object"})
        row = _agent_roster_row("probe", "global", cfg, redact=redact)
        assert _carries_mask(row[field])
        assert "nested" not in json.dumps(row)

    def test_every_value_is_a_string_except_the_one_structured_field(self) -> None:
        """`dict[str, object]` is honest about exactly one field, not a loophole.

        Every value is a `str` but `avatar`, which is a `dict` the dashboard needs
        verbatim. Asserting the exception BY NAME means a second structured field
        cannot appear without this test failing.
        """
        cfg = KiroCrewAgentConfig()
        object.__setattr__(cfg, "description", {"nested": "object"})
        for redact in (False, True):
            row = _agent_roster_row("probe", "global", cfg, redact=redact)
            assert isinstance(row["avatar"], dict)
            non_str = {k for k, v in row.items() if not isinstance(v, str)}
            assert non_str == {"avatar"}, f"unexpected structured value(s): {non_str}"

    def test_owner_keeps_name_addressable_but_app_token_does_not_need_it(self) -> None:
        """``name`` survives only where something can actually address it.

        A GLOBAL row's name is its only handle -- it addresses
        ``/api/agents/{name}`` for edit and delete and keys the usage sort -- and
        masking it for a non-app caller would buy nothing, since the same names
        are readable unmasked from ``GET /api/config/kirocrew`` as the ``agents``
        map's KEYS (``_masked_config_dict`` masks only schema-``sensitive``
        VALUES).

        A PROJECT row is masked for every caller. That argument does not transfer
        to it: project names come from a filesystem scan and appear in no config.
        Nothing can address one either -- both mutating routes 404 on a name
        absent from ``cfg.agents``, and a scanned project agent never is -- so no
        opaque replacement identifier is needed.
        """
        # Global + owner: verbatim, because it is addressable.
        g = _agent_roster_row(f"crew-{self.PROBE}", "global", KiroCrewAgentConfig(), redact=False)
        assert g["name"] == f"crew-{self.PROBE}"
        assert g["scope"] == "global"
        # Global + app token: masked, because an app cannot reach those routes.
        ga = _agent_roster_row(f"crew-{self.PROBE}", "global", KiroCrewAgentConfig(), redact=True)
        assert _carries_mask(ga["name"])
        # Project: masked for BOTH callers, because nothing addresses a project row.
        for redact in (False, True):
            p = _agent_roster_row(
                f"crew-{self.PROBE}", "project", KiroCrewAgentConfig(), redact=redact
            )
            assert _carries_mask(p["name"]), f"project name unmasked at redact={redact}"
            assert p["scope"] == "project"
        # An ordinary name is byte-identical everywhere, so normal rosters and
        # normal project agents stay selectable.
        for scope in ("global", "project"):
            for redact in (False, True):
                row = _agent_roster_row("my-agent", scope, KiroCrewAgentConfig(), redact=redact)
                assert row["name"] == "my-agent"

    @pytest.mark.parametrize("redact", [False, True])
    def test_benign_content_is_never_altered(self, redact: bool) -> None:
        """A mask that swallows legitimate text is a rendering bug, not hardening."""
        cfg = KiroCrewAgentConfig(description="a plain crew", triggers="use for triage")
        row = _agent_roster_row("kirocrew", "global", cfg, redact=redact)
        assert row["description"] == "a plain crew"
        assert row["triggers"] == "use for triage"
        assert row["name"] == "kirocrew"

    def test_both_caller_classes_ship_the_same_key_set(self) -> None:
        """Only values differ. A caller-dependent KEY set would be a second contract."""
        cfg = KiroCrewAgentConfig(description="d", triggers="t")
        owner = _agent_roster_row("probe", "global", cfg, redact=False)
        app = _agent_roster_row("probe", "global", cfg, redact=True)
        assert set(owner) == set(app) == ROSTER_ROW_KEYS


class TestAvatarIsShapeAllowlistedNotMasked:
    """The one structured field: masked where it can carry text, intact elsewhere.

    Both directions are asserted on purpose. A test that only checks masking
    passes just as well against a blanket mask -- and a blanket here would break
    the feature: a masked ``file`` makes the per-crew avatar endpoint resolve
    nothing, and a masked ``kind`` loses ghost-vs-image.
    """

    PROBE = "AKIAIOSFODNN7EXAMPLE"
    FILE_PIN = "0123456789abcdef.png"

    def test_a_credential_shaped_trait_value_is_masked(self) -> None:
        row = _agent_roster_row(
            "probe",
            "global",
            cast(
                KiroCrewAgentConfig,
                types.SimpleNamespace(
                    **{
                        **{f.name: "" for f in dataclasses.fields(KiroCrewAgentConfig)},
                        "avatar": {"kind": "ghost", "traits": {"eyes": self.PROBE}},
                    }
                ),
            ),
            redact=False,
        )
        avatar = cast(dict, row["avatar"])
        assert _carries_mask(avatar["traits"]["eyes"]), "a user-authored trait was not masked"
        assert self.PROBE not in json.dumps(row)

    def test_the_pinned_file_and_kind_survive_intact(self) -> None:
        """The direction that rots. `file` is regex-pinned, so it needs no mask.

        A value constrained by ``_AVATAR_FILE_PIN_RE`` is safer than a masked one:
        the pin refuses a bad value, where masking destroys a good one.
        """
        row = _agent_roster_row(
            "probe",
            "global",
            cast(
                KiroCrewAgentConfig,
                types.SimpleNamespace(
                    **{
                        **{f.name: "" for f in dataclasses.fields(KiroCrewAgentConfig)},
                        "avatar": {"kind": "image", "v": 17, "file": self.FILE_PIN},
                    }
                ),
            ),
            redact=False,
        )
        avatar = cast(dict, row["avatar"])
        assert avatar["kind"] == "image", "the dashboard could not tell ghost from image"
        assert avatar["file"] == self.FILE_PIN, "the avatar endpoint would resolve nothing"
        assert avatar["v"] == 17
        assert not _carries_mask(avatar["file"])

    def test_the_pin_that_makes_targeted_masking_safe_still_holds(self) -> None:
        """The PRECONDITION, not the consequence -- and this one can redden.

        No test can separate "mask only `traits`" from "mask every leaf": while
        `_safe_avatar` pins each non-`traits` leaf to a shape the redactors do not
        alter, the two behave identically, so a mutation masking everything leaves
        the suite green. The targeted rule is therefore documentation.

        What actually protects `file` is the PIN. So assert the pin. This fails the
        day someone loosens the validator -- which is exactly the day the targeted
        masking stops being documentation and starts being load-bearing, and the
        day `_roster_avatar`'s docstring claim ("a blanket would behave the same")
        stops being true.

        Measured shapes, not assumed: a `file` failing the pin is DROPPED while
        `kind` is kept; a junk or missing `kind` collapses the whole override; a
        non-hex `tile` collapses it too, which is what keeps `javascript:` out of
        the SVG markup `tile` is interpolated into.
        """
        good = {"kind": "image", "v": 17, "file": self.FILE_PIN}
        assert _safe_avatar(good)["file"] == self.FILE_PIN

        for bad_file in ("notadigest.png", "../../etc/passwd", "0123456789abcdef.svg", "0123.png"):
            got = _safe_avatar({"kind": "image", "file": bad_file})
            assert "file" not in got, f"the pin let {bad_file!r} through"
            assert got.get("kind") == "image"

        for bad_kind in (
            {"kind": "nonsense", "traits": {"eyes": "wide"}},
            {"traits": {"eyes": "wide"}},
        ):
            assert _safe_avatar(bad_kind) == {}, "kind is no longer held to its literal set"

        assert _safe_avatar({"kind": "ghost", "traits": {"tile": "javascript:alert(1)"}}) == {}
        assert (
            _safe_avatar({"kind": "ghost", "traits": {"tile": "#a1b2c3"}})["traits"]["tile"]
            == "#a1b2c3"
        )

        # And the roster inherits the pin rather than re-implementing it.
        row = _agent_roster_row(
            "probe",
            "global",
            cast(
                KiroCrewAgentConfig,
                types.SimpleNamespace(
                    **{
                        **{f.name: "" for f in dataclasses.fields(KiroCrewAgentConfig)},
                        "avatar": {"kind": "image", "file": "../../etc/passwd"},
                    }
                ),
            ),
            redact=False,
        )
        assert "file" not in cast(dict, row["avatar"])

    def test_junk_collapses_rather_than_shipping_verbatim(self) -> None:
        row = _agent_roster_row(
            "probe",
            "global",
            cast(
                KiroCrewAgentConfig,
                types.SimpleNamespace(
                    **{
                        **{f.name: "" for f in dataclasses.fields(KiroCrewAgentConfig)},
                        "avatar": {"kind": "nonsense", "evil": self.PROBE},
                    }
                ),
            ),
            redact=False,
        )
        assert self.PROBE not in json.dumps(row)
        assert "evil" not in cast(dict, row["avatar"])


class TestCredentialShapedNamesAreRefusedAtCreation:
    """The hazard is closed where the name comes to exist, not where it is read.

    GPT 5.6 asked for `_roster_mask(name)` on every caller, owner included. That
    masks the field the owner needs to SELECT, RENAME and tell crews apart, and it
    closes one read site out of N -- a stored credential-shaped name still reaches
    logs, error messages and telemetry. Refusing it at creation closes the source.
    """

    PROBE = "AKIAIOSFODNN7EXAMPLE"

    def test_the_create_rule_is_keyed_to_the_read_rule(self) -> None:
        """One detector, so the two halves cannot drift apart.

        Anything the roster would mask is refused at creation; anything it ships
        verbatim is accepted. That equivalence is the invariant, not two lists.
        """
        for candidate in (self.PROBE, f"https://x.example/?token={self.PROBE}"):
            assert _name_would_be_masked(candidate) is True
            assert _roster_mask(candidate) != candidate
        for benign in ("oncall", "kirocrew", "crew-7", "release manager"):
            assert _name_would_be_masked(benign) is False
            assert _roster_mask(benign) == benign

    @pytest.mark.asyncio
    async def test_creation_refuses_a_credential_shaped_name(self) -> None:
        seed = _seed_config_with_every_field_set()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(seed, f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                from kiro_crew.dashboard.handlers import api_kirocrew_agents_create

                # POST is owner-gated (`_require_owner`), so the caller must BE the
                # owner for the request to reach the name check at all.
                @web.middleware
                async def _owner(request: web.Request, handler):  # type: ignore[no-untyped-def]
                    request["app"] = ""
                    request["user"] = "owner-1"
                    return await handler(request)

                app = web.Application(middlewares=[_owner])
                app["state"] = types.SimpleNamespace(owner_id="owner-1", conversation_log=None)
                app.router.add_post("/api/agents", api_kirocrew_agents_create)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.post(
                        "/api/agents",
                        json={"name": self.PROBE, "kiro_agent": "kirocrew"},
                    )
                    assert resp.status == 400, await resp.text()
                    payload = await resp.json()
                    assert payload["code"] == "credential_shaped_name"
                    # The refusal must not reflect the value into the response or
                    # the request log -- that is the disclosure being prevented.
                    assert self.PROBE not in json.dumps(payload)
            # And nothing was stored under it.
            assert self.PROBE not in json.loads(tmp.read_text()).get("agents", {})
        finally:
            tmp.unlink(missing_ok=True)


class TestCallerClassIsTheOwnerPredicate:
    """Who counts as the owner is delegated, not hand-rolled per caller class."""

    PROBE = "AKIAIOSFODNN7EXAMPLE"

    def _app(self, *, user: str, owner_id: str) -> web.Application:
        """An app whose requests carry a dashboard identity and an owner_id.

        `is_owner_dashboard_request` requires `app == ""` (a dashboard token, not
        an app token), a non-empty `user`, and a match against `state.owner_id`.
        """

        @web.middleware
        async def _identity(request: web.Request, handler):  # type: ignore[no-untyped-def]
            request["app"] = ""
            request["user"] = user
            return await handler(request)

        from kiro_crew.dashboard.handlers import api_kirocrew_agents

        app = web.Application(middlewares=[_identity])
        app["state"] = types.SimpleNamespace(owner_id=owner_id, conversation_log=None)
        app.router.add_get("/api/agents", api_kirocrew_agents)
        return app

    async def _name_for(self, app: web.Application, tmp: Path) -> str:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/agents")
                assert resp.status == 200
                rows = (await resp.json())["agents"]
        return str(rows[0]["name"])

    def _seed(self) -> Path:
        seed = _seed_config_with_every_field_set()
        seed["agents"] = {f"crew-{self.PROBE}": seed["agents"]["roster-probe"]}
        seed["default_agent"] = f"crew-{self.PROBE}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(seed, f)
            return Path(f.name)

    @pytest.mark.asyncio
    async def test_a_non_owner_dashboard_session_gets_the_name_masked(self) -> None:
        """The hole an app-token-only check leaves open.

        `request.get("app", "")` asks "is this an app?", and a non-owner DASHBOARD
        session answers no -- an allow-listed messaging user running `!dashboard`
        holds a dashboard token with `app == ""`. That caller is not the trust
        root, so it must not receive a credential-shaped crew name raw.
        """
        tmp = self._seed()
        try:
            name = await self._name_for(self._app(user="someone-else", owner_id="owner-1"), tmp)
            assert _carries_mask(name), "a non-owner dashboard session saw the raw name"
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_the_owner_still_gets_an_addressable_global_name(self) -> None:
        """The owner keeps the row's only handle, or edit and delete break."""
        tmp = self._seed()
        try:
            name = await self._name_for(self._app(user="owner-1", owner_id="owner-1"), tmp)
            assert name == f"crew-{self.PROBE}"
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_a_stateless_app_fails_closed(self) -> None:
        """No state means no owner can be resolved, so mask rather than show.

        `is_owner_dashboard_request` subscripts `app["state"]`. For a disclosure
        control, "unknown caller" must mean "mask".
        """
        tmp = self._seed()
        try:
            name = await self._name_for(_make_app(), tmp)
            assert _carries_mask(name)
        finally:
            tmp.unlink(missing_ok=True)

    def test_a_mask_nested_in_a_structured_field_is_refused(self) -> None:
        """The corruption a top-level-only check let through.

        `avatar` is a dict whose `traits` values are masked, so an echoed avatar
        carries the sentinel one level DOWN. A flat check sees a `dict`, answers
        "not a mask", and lets `_safe_avatar` persist the sentinel over the stored
        trait. Any string anywhere inside the value must count.
        """
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

        assert _carries_mask({"kind": "ghost", "traits": {"eyes": _SENSITIVE_MASK}}) is True
        assert _carries_mask({"kind": "ghost", "traits": {"eyes": f"{_SENSITIVE_MASK} x"}}) is True
        assert _carries_mask([{"traits": {"eyes": _SENSITIVE_MASK}}]) is True
        # A clean structured value is still a genuine edit.
        assert _carries_mask({"kind": "image", "v": 17, "file": "0123456789abcdef.png"}) is False
        assert _carries_mask({"kind": "ghost", "traits": {"eyes": "wide", "blush": True}}) is False

    @pytest.mark.asyncio
    async def test_an_echoed_avatar_does_not_overwrite_a_masked_trait(self) -> None:
        """End to end: the sentinel never reaches config.json through the nest."""
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

        seed = _seed_config_with_every_field_set()
        stored = f"eyes-{self.PROBE}"
        seed["agents"]["roster-probe"]["avatar"] = {"kind": "ghost", "traits": {"eyes": stored}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(seed, f)
            tmp = Path(f.name)
        try:
            with (
                unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp),
                unittest.mock.patch(
                    "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
                    lambda request: True,
                ),
            ):
                from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

                app = web.Application()
                app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
                async with TestClient(TestServer(app)) as client:
                    put = await client.put(
                        "/api/agents/roster-probe",
                        json={"avatar": {"kind": "ghost", "traits": {"eyes": _SENSITIVE_MASK}}},
                    )
                    assert put.status == 200, await put.text()
            after = json.loads(tmp.read_text())["agents"]["roster-probe"]["avatar"]
            assert _SENSITIVE_MASK not in json.dumps(after), "the nested sentinel was persisted"
            assert after["traits"]["eyes"] == stored
        finally:
            tmp.unlink(missing_ok=True)


class TestMaskIsTreatedAsUnchangedOnWrite:
    """The write half, without which the read half destroys stored config."""

    PROBE = "AKIAIOSFODNN7EXAMPLE"

    @pytest.fixture(autouse=True)
    def _as_owner(self, monkeypatch):
        """Run past the owner gate.

        These tests exercise the write-side rule, not the owner boundary -- that
        has its own enumerate-the-invariant coverage in
        `test_agents_endpoints_owner_auth.py`. Same patch `test_config_api.py`
        uses for the mutating agent endpoints.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    def test_the_sentinel_is_the_one_the_config_endpoint_uses(self) -> None:
        """Not a private copy: drift would silently break the round-trip rule."""
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

        assert _carries_mask(_SENSITIVE_MASK)
        row = _agent_roster_row(
            "probe", "global", KiroCrewAgentConfig(description=f"see {self.PROBE}"), redact=False
        )
        assert row["description"] == _SENSITIVE_MASK

    def test_real_content_is_not_treated_as_the_mask(self) -> None:
        assert _carries_mask("plain text") is False
        assert _carries_mask("") is False
        assert _carries_mask({"a": 1}) is False

    def test_a_mask_the_operator_appended_to_is_still_refused(self) -> None:
        """The editor renders the mask into a text input, so it can be typed past.

        An exact-match rule closes only the echo-it-back case. Appending to the
        mask produces a string that is not the sentinel, so equality would persist
        the redaction glyphs plus the addition over the stored original -- the data
        loss this rule exists to stop.
        """
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

        assert _carries_mask(_SENSITIVE_MASK) is True
        assert _carries_mask(f"{_SENSITIVE_MASK} and also X") is True
        assert _carries_mask(f"prefix {_SENSITIVE_MASK}") is True
        assert _carries_mask(f"a {_SENSITIVE_MASK} b") is True

    @pytest.mark.asyncio
    async def test_appending_to_a_masked_field_does_not_overwrite_it(self) -> None:
        """End to end: glyphs never reach config.json, and the original survives.

        This is GPT 5.6's finding on `a34a51a88`'s successor as an executable test:
        masked trigger -> editor appends text -> PUT must NOT persist the sentinel
        plus the text.
        """
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

        seed = _seed_config_with_every_field_set()
        stored = f"use when {self.PROBE}"
        seed["agents"]["roster-probe"]["triggers"] = stored
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(seed, f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

                app = web.Application()
                app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
                async with TestClient(TestServer(app)) as client:
                    put = await client.put(
                        "/api/agents/roster-probe",
                        json={"triggers": f"{_SENSITIVE_MASK} and also triage"},
                    )
                    assert put.status == 200, await put.text()
            after = json.loads(tmp.read_text())["agents"]["roster-probe"]
            assert after["triggers"] == stored, "the appended mask was persisted"
            assert _SENSITIVE_MASK not in after["triggers"]
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_end_to_end_read_then_write_preserves_the_stored_value(self) -> None:
        """The whole point, over HTTP: GET the roster, PUT the row back.

        This is the round-trip defect as an executable test. The agents page seeds
        its edit sheet from a roster row and `saveEdit` returns every field
        unconditionally, so without the write-side rule the mask would be
        persisted over the operator's stored value on the next save of an
        unrelated field.
        """
        seed = _seed_config_with_every_field_set()
        stored_triggers = f"use when {self.PROBE}"
        seed["agents"]["roster-probe"]["triggers"] = stored_triggers
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(seed, f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                from kiro_crew.dashboard.handlers import (
                    api_kirocrew_agent_update,
                    api_kirocrew_agents,
                )

                app = web.Application()
                app.router.add_get("/api/agents", api_kirocrew_agents)
                app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents")
                    rows = {a["name"]: a for a in (await resp.json())["agents"]}
                    row = rows["roster-probe"]
                    assert _carries_mask(row["triggers"]), "the value should arrive masked"
                    # Echo the row back the way `saveEdit` does: every field, always.
                    put = await client.put(
                        "/api/agents/roster-probe",
                        json={
                            "kiro_agent": row["kiro_agent"],
                            "workspace": row["workspace"],
                            "memory_store": row["memory_store"],
                            "triggers": row["triggers"],
                            "model": row["model"],
                            "reasoning_effort": row["reasoning_effort"],
                            "session_color": row["session_color"],
                        },
                    )
                    assert put.status == 200, await put.text()

                stored = json.loads(tmp.read_text())["agents"]["roster-probe"]
                assert stored["triggers"] == stored_triggers, "the mask was persisted"
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_a_stale_view_cannot_corrupt_the_config(self) -> None:
        """The failure mode a recomputed-equality rule had and a sentinel does not.

        If the stored value changes between the GET and the PUT (an agent editing
        `config.json`, a second dashboard tab), a rule that recognised the view by
        recomputing the redaction of the CURRENT value would stop matching and
        write the redacted text in as though the operator had typed it. The
        sentinel does not depend on the stored value, so the stale echo is still
        recognised and dropped.
        """
        seed = _seed_config_with_every_field_set()
        seed["agents"]["roster-probe"]["triggers"] = f"first {self.PROBE}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(seed, f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                from kiro_crew.dashboard.handlers import (
                    api_kirocrew_agent_update,
                    api_kirocrew_agents,
                )

                app = web.Application()
                app.router.add_get("/api/agents", api_kirocrew_agents)
                app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
                async with TestClient(TestServer(app)) as client:
                    rows = {
                        a["name"]: a
                        for a in (await (await client.get("/api/agents")).json())["agents"]
                    }
                    stale = rows["roster-probe"]["triggers"]
                    # Someone else rewrites the stored value AFTER the read.
                    on_disk = json.loads(tmp.read_text())
                    on_disk["agents"]["roster-probe"]["triggers"] = f"second {self.PROBE} changed"
                    tmp.write_text(json.dumps(on_disk))
                    put = await client.put("/api/agents/roster-probe", json={"triggers": stale})
                    assert put.status == 200, await put.text()

                stored = json.loads(tmp.read_text())["agents"]["roster-probe"]
                assert stored["triggers"] == f"second {self.PROBE} changed"
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_an_echoed_mask_cannot_reject_an_unrelated_edit(self) -> None:
        """The mask filter runs BEFORE the validated fields are validated.

        ``model`` and ``reasoning_effort`` are checked before the config load and
        reject a bad value with 400. If the mask filter ran after them, a client
        echoing a masked ``reasoning_effort`` back would have its whole save
        rejected -- failing an edit to some unrelated field. Dropping masked
        entries first means a mask is never validated as if it were content.
        """
        seed = _seed_config_with_every_field_set()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(seed, f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                from kiro_crew.dashboard.handlers import api_kirocrew_agent_update
                from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

                app = web.Application()
                app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
                async with TestClient(TestServer(app)) as client:
                    put = await client.put(
                        "/api/agents/roster-probe",
                        # The mask in a VALIDATED field, alongside a real edit.
                        json={
                            "reasoning_effort": _SENSITIVE_MASK,
                            "model": _SENSITIVE_MASK,
                            "triggers": "a genuine new value",
                        },
                    )
                    assert put.status == 200, await put.text()
                stored = json.loads(tmp.read_text())["agents"]["roster-probe"]
                # The real edit landed...
                assert stored["triggers"] == "a genuine new value"
                # ...and the masked fields kept their stored values.
                assert stored["reasoning_effort"] == "high"
                assert stored["model"] == "claude-opus-5"
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_a_real_edit_still_writes_through(self) -> None:
        """The rule must not swallow an actual change, or editing is broken."""
        seed = _seed_config_with_every_field_set()
        seed["agents"]["roster-probe"]["triggers"] = f"use when {self.PROBE}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(seed, f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

                app = web.Application()
                app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
                async with TestClient(TestServer(app)) as client:
                    put = await client.put(
                        "/api/agents/roster-probe", json={"triggers": "an actual new value"}
                    )
                    assert put.status == 200, await put.text()
                stored = json.loads(tmp.read_text())["agents"]["roster-probe"]
                assert stored["triggers"] == "an actual new value"
        finally:
            tmp.unlink(missing_ok=True)
