"""End-to-end wiring of the per-member append-only event log into the members
roster: the ``/api/members`` projections with the lazy, idempotent config
reconcile, and the roster and per-member projections routes as pure reads.

The startup reconcile sweep is pinned in
``test_members_eventlog_wiring_startup_reconcile.py``, the runtime writers and
the subscribe baseline in ``test_members_eventlog_wiring_live_writers.py``, and
the ordering and durability of queued appends in
``test_members_eventlog_wiring_append_ordering.py``. The fixtures they share
live in ``members_eventlog_wiring_helpers.py``.

Isolation: the ``_fresh_eventlog`` fixture re-roots ``kiro_crew.members.data_home``
at ``tmp_path`` and calls ``set_service(None)``, so no test sees another's log.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from members_eventlog_wiring_helpers import _fresh_eventlog  # noqa: F401
from members_eventlog_wiring_helpers import CREW, FAKE_CONFIG_DIGEST, _agent, _fake_config

from kiro_crew import eventlog_hooks, members
from kiro_crew.dashboard.handlers import members as handlers_members
from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK
from kiro_crew.eventlog import types
from kiro_crew.eventlog.service import get_service, set_service


@pytest.fixture
def live_config_stamp_matches(monkeypatch):
    """Make the live config stamp agree with what ``_fake_config`` claims to be.

    Without this the real :func:`config_content_stamp` reads the host's own files, so the
    reconcile refuses on a mismatch and a test that means to exercise it silently does
    not.
    """
    from kiro_crew.config import loader as loader_mod

    monkeypatch.setattr(loader_mod, "config_content_stamp", lambda: FAKE_CONFIG_DIGEST)
    return FAKE_CONFIG_DIGEST


# 1. api_members: projections + idempotent lazy config reconcile
def _members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_members

    @web.middleware
    async def _auth(request, handler):
        # Same shape as test/dashboard_owner_helpers.py: `local-app` is the owner,
        # and a test that wants a NON-owner sends X-Test-User. Read from the header
        # so the owner gate below can actually be exercised on both sides -- a
        # fixture hard-coding the owner can only ever prove the owner still works,
        # which cannot tell a working gate from no gate at all.
        request["app"] = request.headers.get("X-Test-App", "")
        request["user"] = request.headers.get("X-Test-User", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    return app


class TestApiMembersProjections:
    @pytest.mark.asyncio
    async def test_rows_carry_projections_and_reconcile_is_idempotent(
        self, tmp_path, monkeypatch, live_config_stamp_matches
    ):
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        app = _members_app(state)

        # The roster is a pure READ, so a member with no log has nothing to serve.
        # This test is about what an EXISTING log projects onto a row.
        svc = get_service()
        slug = members.slug_for_name(CREW)
        svc.ensure(slug, CREW)

        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        row = data["members"][0]
        proj = row["projections"]
        assert set(proj["values"]) == {types.PROJ_ROSTER}, (
            "a list ROW paints the roster line only, so the three drawer views must "
            f"not ride along on every row: {sorted(proj['values'])}"
        )
        assert isinstance(proj["asOfSeq"], int)

        # First call's reconcile appended exactly one member/config (the log had
        # never seen one). A SECOND pass must append nothing: the roster view
        # now matches the live config, so the reconcile is a no-op.
        seq_after_first = svc.last_seq(slug)
        # A real bound, not `>= 0`: `last_seq` answers 0 for a header-only log, so a
        # read that reconciled NOTHING satisfies that and the no-op check below then
        # compares two zeros. Require the baseline to be visible in the fold.
        assert seq_after_first > 0, "the first read never established a config baseline"
        assert (
            proj["values"][types.PROJ_ROSTER].get("model") == "claude-x"
        ), "the first read did not reconcile the fold against the config it loaded"
        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")
        assert svc.last_seq(slug) == seq_after_first, "config reconcile is not idempotent"

    @pytest.mark.asyncio
    async def test_a_save_landing_after_the_config_load_is_not_overwritten(
        self, tmp_path, monkeypatch
    ):
        """A save landing mid-request must not be overwritten by the stale config.

        `api_members` loads the config once, then reads more before projecting; a save
        in that gap is already in the fold. Only checking that the config is still the
        bytes the request read tells the two apart, since the fold did not move.
        """
        from kiro_crew.config import loader as loader_mod
        from kiro_crew.dashboard.handlers import members as handlers

        cfg = _fake_config({CREW: _agent(model="sonnet")})
        # Provenance rides on the config object: a real load records the digest of the
        # bytes it parsed, and that is what arms the refusal being tested. The live
        # content agrees with it until the save lands, so the guard starts armed and
        # matching and the baseline below is free to be written.
        live = {"stamp": "stamp-as-this-request-read-it"}
        cfg._content_digest = live["stamp"]
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        monkeypatch.setattr(loader_mod, "config_content_stamp", lambda: live["stamp"])
        state = _make_state(tmp_path)
        app = _members_app(state)

        svc = get_service()
        slug = members.slug_for_name(CREW)
        svc.ensure(slug, CREW)

        # A first read establishes the baseline the log holds for this member.
        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")
        assert (
            svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {}).get("model") == "sonnet"
        ), "the first read never established a config baseline"

        # The save lands after the request bound its stamp and before any row is
        # projected: `_logged_slugs` runs between the config load and the row loop.
        # The request's `cfg` is left untouched; holding the pre-save object is the
        # staleness being tested.
        real_logged_slugs = handlers._logged_slugs

        def _save_lands_mid_request(service):
            out = real_logged_slugs(service)
            if live["stamp"] != "stamp-after-the-save":
                service.append(
                    slug,
                    types.MEMBER_CONFIG,
                    {
                        **eventlog_hooks._config_snapshot_for_agent(_agent(model="opus")),
                        "changed": ["model"],
                    },
                )
                # config.json now holds different bytes than this request read.
                live["stamp"] = "stamp-after-the-save"
            return out

        monkeypatch.setattr(handlers, "_logged_slugs", _save_lands_mid_request)

        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")

        monkeypatch.setattr(handlers, "_logged_slugs", real_logged_slugs)
        folded = svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {}).get("model")
        assert folded == "opus", (
            "the save was overwritten: the reconcile appended the config this request "
            f"loaded before the save, so the projection regressed to {folded!r}"
        )

    @pytest.mark.asyncio
    async def test_a_load_that_could_not_name_its_bytes_reconciles_nothing(
        self, tmp_path, monkeypatch
    ):
        """A request holding a config of unknown provenance must not correct the log.

        With no digest (unreadable files, or defaults standing in) the read skips the
        correcting write rather than append unguarded, and the rows still render. The
        sweep's exemption is reached by its own name, so a failed load cannot borrow it.
        """
        from kiro_crew.config import loader as loader_mod
        from kiro_crew.dashboard.handlers import members as handlers

        cfg = _fake_config({CREW: _agent(model="sonnet")})
        live = {"stamp": "stamp-as-the-file-actually-is"}
        # NO provenance rides on this object, which is what a load that could not name
        # its bytes returns. The attribute is removed rather than mismatched: a wrong
        # digest exercises the currency refusal instead, and this test is about the
        # absence of one.
        del cfg._content_digest
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        monkeypatch.setattr(loader_mod, "config_content_stamp", lambda: live["stamp"])
        state = _make_state(tmp_path)
        app = _members_app(state)

        svc = get_service()
        slug = members.slug_for_name(CREW)
        svc.ensure(slug, CREW)

        # A baseline the log holds for this member, written by a save rather than by a
        # reconcile: this read cannot reconcile at all, so it establishes nothing.
        svc.append(
            slug,
            types.MEMBER_CONFIG,
            {
                **eventlog_hooks._config_snapshot_for_agent(_agent(model="sonnet")),
                "changed": list(eventlog_hooks._CONFIG_FIELDS),
            },
        )

        # THE SAVE, landing inside the window, exactly as in the stamped case above.
        real_logged_slugs = handlers._logged_slugs

        def _save_lands_mid_request(service):
            out = real_logged_slugs(service)
            if live["stamp"] != "stamp-after-the-save":
                service.append(
                    slug,
                    types.MEMBER_CONFIG,
                    {
                        **eventlog_hooks._config_snapshot_for_agent(_agent(model="opus")),
                        "changed": ["model"],
                    },
                )
                live["stamp"] = "stamp-after-the-save"
            return out

        monkeypatch.setattr(handlers, "_logged_slugs", _save_lands_mid_request)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/members")
            assert resp.status == 200, "the rows must still render without a stamp"
        monkeypatch.setattr(handlers, "_logged_slugs", real_logged_slugs)

        folded = svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {}).get("model")
        assert folded == "opus", (
            "a request whose load could not name its bytes reconciled anyway, so the "
            f"save was overwritten and the projection regressed to {folded!r}"
        )

    @pytest.mark.asyncio
    async def test_a_config_degraded_to_defaults_reconciles_nothing(
        self, tmp_path, monkeypatch, live_config_stamp_matches
    ):
        """Current is not enough: the config must also be what the operator wrote.

        A file that will not parse still passes the currency check, but its object holds
        field defaults; correcting from those would overwrite good values over a typo.
        ``degraded_sections`` reports that.
        """
        cfg = _fake_config({CREW: _agent(model="sonnet")})
        cfg.degraded_sections = frozenset({"agent"})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        app = _members_app(state)

        svc = get_service()
        slug = members.slug_for_name(CREW)
        svc.ensure(slug, CREW)
        # A good value already projected, written by a save rather than a reconcile.
        svc.append(
            slug,
            types.MEMBER_CONFIG,
            {
                **eventlog_hooks._config_snapshot_for_agent(_agent(model="opus")),
                "changed": ["model"],
            },
        )
        seq_before = svc.last_seq(slug)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/members")
            assert resp.status == 200, "the rows must still render from a degraded config"

        assert (
            svc.last_seq(slug) == seq_before
        ), "a degraded config reconciled anyway, so a typo can overwrite the log"
        folded = svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {}).get("model")
        assert (
            folded == "opus"
        ), f"the projected value was replaced by a default from a degraded config: {folded!r}"

    @pytest.mark.asyncio
    async def test_a_cached_config_under_a_colliding_fingerprint_reconciles_nothing(
        self, tmp_path, monkeypatch
    ):
        """The stamp must describe the bytes the returned object was parsed from.

        `KiroCrewConfig.load` caches on stat metadata, so a same-size replacement can be
        served the earlier document; the digest comes from the load's own entry. Here the
        live content moved past the served entry, so the reconcile writes nothing.
        """
        from kiro_crew.config import loader as loader_mod

        cfg = _fake_config({CREW: _agent(model="sonnet")})
        state = _make_state(tmp_path)
        app = _members_app(state)

        svc = get_service()
        slug = members.slug_for_name(CREW)
        svc.ensure(slug, CREW)
        # A newer word already folded, so a stale write would be a visible regression.
        svc.append(
            slug,
            types.MEMBER_CONFIG,
            {
                **eventlog_hooks._config_snapshot_for_agent(_agent(model="opus")),
                "changed": ["model"],
            },
        )
        assert svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {}).get("model") == "opus"

        # The load is served a cached document whose bytes are stale: it reports the
        # digest of what it was parsed from, and the live content differs.
        cfg._content_digest = "digest-of-the-bytes-this-entry-was-parsed-from"
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        monkeypatch.setattr(loader_mod, "config_content_stamp", lambda: "digest-of-the-live-bytes")

        seq_before = svc.last_seq(slug)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/members")

        assert resp.status == 200, "a stale cached config must not fail the roster read"
        folded = svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {}).get("model")
        assert svc.last_seq(slug) == seq_before, (
            "a config served from a colliding cache entry still reconciled, so the log "
            f"now holds values the live config does not carry (model reads {folded!r})"
        )
        assert folded == "opus", f"the stale config was written over the newer word: {folded!r}"

    @pytest.mark.asyncio
    async def test_a_writer_between_the_observation_and_the_write_refuses_the_correction(
        self, tmp_path, monkeypatch
    ):
        """The second window: the fold moves after the comparison was made.

        Another writer can commit between the comparison and this write, and its values
        are newer. The direct call passes no stamp, so only the conditional append can
        refuse it.
        """
        from kiro_crew.config import loader as loader_mod

        cfg = _fake_config({CREW: _agent(model="sonnet")})
        # A real load names the bytes it parsed, and the baseline read below depends on
        # that: a config of unknown provenance is reconciled by no read at all.
        cfg._content_digest = "the-bytes-this-request-read"
        monkeypatch.setattr(loader_mod, "config_content_stamp", lambda: cfg._content_digest)
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        app = _members_app(state)

        svc = get_service()
        slug = members.slug_for_name(CREW)
        svc.ensure(slug, CREW)
        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")

        # What the caller observed, and then a foreign commit moving it.
        observed = svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {})
        assert observed.get("model") == "sonnet"
        svc.append(
            slug,
            types.MEMBER_CONFIG,
            {
                **eventlog_hooks._config_snapshot_for_agent(_agent(model="opus")),
                "changed": ["model"],
            },
        )
        assert svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {}).get("model") == "opus"

        changed = eventlog_hooks.reconcile_member_config(
            slug, CREW, _agent(model="haiku"), observed, config_stamp=cfg._content_digest
        )

        folded = svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {}).get("model")
        assert changed is None, (
            "the correction reported a write against a projection that had already "
            f"moved: changed={changed!r}"
        )
        assert folded == "opus", (
            "a correction decided against the older fold landed anyway, so the newer "
            f"word was regressed to {folded!r}"
        )

    @pytest.mark.asyncio
    async def test_a_row_whose_log_belongs_to_another_member_gets_no_projection(
        self, tmp_path, monkeypatch
    ):
        """One log holds ONE member's folded state, so a colliding row must not read it.

        A slug is lossy, so a projection served on the wrong row would show one member's
        state as the other's. The row is served empty; the header tells the two apart.
        """
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        app = _members_app(state)

        # Give the slug a log that belongs to somebody else, then ask for the roster.
        svc = get_service()
        slug = members.slug_for_name(CREW)
        svc.ensure(slug, "Somebody Else")
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "member-somebody-else"})
        assert svc.logged_name(slug) == "Somebody Else"

        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        proj = data["members"][0]["projections"]
        assert proj["asOfSeq"] == -1, f"served another member's projection: {proj}"
        assert proj["values"] == {}

    @pytest.mark.asyncio
    async def test_two_rows_sharing_one_slug_are_both_blank(self, tmp_path, monkeypatch):
        """The projection map is keyed by slug, so a collision must blank BOTH rows.

        Whichever row is projected last owns the key, so the result would depend on
        iteration order; a shared slug is blank for everyone.
        """
        other = "Code_Reviewer"
        assert members.slug_for_name(other) == members.slug_for_name(CREW), "precondition"
        cfg = _fake_config(
            {CREW: _agent(model="claude-x"), other: _agent(model="claude-y")},
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        app = _members_app(state)

        # Give the shared slug a log that genuinely belongs to ONE of them, with
        # state a wrong row would visibly render.
        svc = get_service()
        slug = members.slug_for_name(CREW)
        svc.ensure(slug, CREW)
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "member-code-reviewer"})
        assert svc.last_seq(slug) >= 0

        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        rows = data["members"]
        assert len(rows) == 2, rows
        for row in rows:
            proj = row["projections"]
            assert proj["asOfSeq"] == -1, f"a colliding row was served a projection: {row}"
            assert proj["values"] == {}

    @pytest.mark.asyncio
    async def test_a_header_locked_to_the_slug_still_serves_the_members_own_projection(
        self, tmp_path, monkeypatch
    ):
        """A placeholder header names nobody, so it is not evidence of a second member.

        A nameless writer for a member not yet in config leaves the slug in the header.
        Reading that as an owner would blank the member's own state once they appear.
        """
        named = "Code_Reviewer"
        slug = members.slug_for_name(named)
        assert slug != named, "this test needs a name its slug does not equal"

        # Phase 1: the member is not in the config, so the header keeps the slug.
        empty = _fake_config({})
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: empty
        )
        svc = get_service()
        svc.ensure(slug, slug)
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "member-code-reviewer"})
        assert svc.logged_name(slug) == slug, "resolution should have come up empty here"

        # Phase 2: the member now exists under a name the slug does not equal.
        cfg = _fake_config({named: _agent(model="claude-x")}, default=named)
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        app = _members_app(state)

        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        proj = data["members"][0]["projections"]
        assert proj["asOfSeq"] >= 0, f"own projection withheld on a placeholder header: {proj}"
        assert set(proj["values"]) == {types.PROJ_ROSTER}

    @pytest.mark.asyncio
    async def test_a_placeholder_header_is_served_but_never_written_through(
        self, tmp_path, monkeypatch
    ):
        """Serving a placeholder log's projection is a read; reconciling the row's
        config INTO it is a write into a log whose owner cannot be told apart
        from a retired member handed the same slug. The row still renders, but
        the roster read appends nothing to that log."""
        named = "Code_Reviewer"
        slug = members.slug_for_name(named)
        empty = _fake_config({})
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: empty
        )
        svc = get_service()
        svc.ensure(slug, slug)
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "member-code-reviewer"})
        assert svc.logged_name(slug) == slug
        before = svc.last_seq(slug)

        cfg = _fake_config({named: _agent(model="claude-x")}, default=named)
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_members_app(state))) as client:
            data = await (await client.get("/api/members")).json()
        proj = data["members"][0]["projections"]
        assert proj["asOfSeq"] >= 0
        assert svc.last_seq(slug) == before, "the roster read wrote into a placeholder log"
        assert "model" not in proj["values"].get(types.PROJ_ROSTER, {})

    @pytest.mark.asyncio
    async def test_a_fresh_log_resolves_a_placeholder_name_from_the_roster(
        self, tmp_path, monkeypatch
    ):
        """A nameless writer must not decide the header says the slug.

        ``emit`` passes ``name or slug`` and the header is written once, so a kept slug
        would cost the log its collision check. Resolution runs only on the fresh path.
        """
        named = "Code_Reviewer"
        cfg = _fake_config({named: _agent(model="claude-x")}, default=named)
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        svc = get_service()
        slug = members.slug_for_name(named)
        assert slug != named, "this test needs a name its slug does not equal"

        svc.ensure(slug, slug)
        assert svc.logged_name(slug) == named, "the header kept the placeholder"

    @pytest.mark.asyncio
    async def test_projection_values_are_redacted_before_egress(
        self, tmp_path, monkeypatch, live_config_stamp_matches
    ):
        """The roster list embeds ``svc.snapshot()`` per member, and snapshot
        returns raw values. ``avatar`` is an agent-writable config field folded into
        the roster view verbatim, so it can embed a credential or presigned URL, and
        the list route must scrub it before the response crosses the network
        boundary -- the same chain the ``/activity`` read runs over the text it
        surfaces."""
        import json

        secret_url = "https://evil.example/x?token=AKIAIOSFODNN7EXAMPLE"
        cfg = _fake_config({CREW: _agent(avatar=secret_url)})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_members_app(state))) as client:
            data = await (await client.get("/api/members")).json()
        blob = json.dumps(data)
        assert secret_url not in blob
        assert "AKIAIOSFODNN7EXAMPLE" not in blob
        # The projection block is still present (redacted), not dropped.
        roster = data["members"][0]["projections"]["values"].get(types.PROJ_ROSTER)
        assert roster is not None, "the roster view was dropped rather than redacted"
        # The KEY must be there: an absent `avatar` makes `.get` return None, which
        # differs from the secret and would pass this without any redaction happening.
        assert "avatar" in roster, (
            "no avatar was projected, so this asserts nothing about redaction: " f"{sorted(roster)}"
        )
        assert roster["avatar"] != secret_url

    @pytest.mark.asyncio
    async def test_a_short_url_param_leaf_is_scrubbed_in_the_projection_as_in_the_row(
        self, tmp_path, monkeypatch, live_config_stamp_matches
    ):
        """A short secret URL parameter must not survive in the roster projection.

        ``GET /api/members`` ships the leaf as the row field (``_roster_mask``) and in
        ``projections.values.roster`` (``_redact_projection_value``). A short query such as
        ``?api_key=abc123`` is under the exfiltration length floor, and ``MembersPage``
        renders the projection first, so both chains must mask it.
        """
        import json

        leaf = "https://h.example/v1?api_key=abc123"
        cfg = _fake_config({CREW: _agent(model=leaf)})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_members_app(state))) as client:
            data = await (await client.get("/api/members")).json()

        row = data["members"][0]
        # The row masks it: pinned so the projection assertion below is measured
        # against a real divergence between the two egress chains.
        assert row["model"] == _SENSITIVE_MASK, (
            "the row does not mask the short URL-secret param, so this test does "
            "not exercise the row/projection divergence it is about"
        )
        # The projection must carry the same mask as the row; the whole response
        # must not ship the secret.
        roster = row["projections"]["values"].get(types.PROJ_ROSTER)
        assert roster is not None, "the roster view was dropped rather than redacted"
        assert "model" in roster, (
            "the model leaf was not projected, so this asserts nothing about the "
            f"projection chain: {sorted(roster)}"
        )
        assert "abc123" not in json.dumps(data), (
            "the roster projection ships the short URL-secret param while the row "
            "masks it; the two egress chains disagree"
        )

    @pytest.mark.asyncio
    async def test_editing_model_appends_one_member_config_changed_model(
        self, tmp_path, monkeypatch
    ):
        """A config edited outside the dashboard reaches the log on the next read.

        The reconcile compares the folded roster against the config THIS request
        loaded, so the case that matters is the one that bypasses the dashboard's
        own save: an operator editing the file with the gateway up. Nothing is
        remembered between requests, so the next read is the one that corrects it.
        """
        from kiro_crew.config import loader as loader_mod

        cfg = _fake_config({CREW: _agent(model="claude-x")})
        # A real load names the bytes it parsed; a hand edit changes the file, so the
        # digest moves with it and the live stamp keeps agreeing with what was read.
        cfg._content_digest = "the-bytes-holding-claude-x"
        monkeypatch.setattr(loader_mod, "config_content_stamp", lambda: cfg._content_digest)
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        app = _members_app(state)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)

        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")
        seq_before = svc.last_seq(slug)
        assert seq_before >= 0, "the first read never established a config baseline"

        # The edit: the loader answers the new model from here on, out of new bytes.
        cfg.agents[CREW].model = "gpt-y"
        cfg._content_digest = "the-bytes-holding-gpt-y"
        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")

        assert svc.last_seq(slug) == seq_before + 1, (
            "the edited config never reached the log: the roster read did not "
            "reconcile the fold against the config it loaded"
        )
        newest = svc.history(slug, before=None, limit=1)[0]
        assert newest["type"] == types.MEMBER_CONFIG
        assert newest["data"]["changed"] == ["model"]
        assert newest["data"]["model"] == "gpt-y"


# 2. The roster is a READ: no creates, one view per row, the rest per member
def _projections_app(state) -> web.Application:
    """The per-member projections route, mounted the way the roster app above is."""
    from kiro_crew.dashboard.handlers.members import api_member_projections

    @web.middleware
    async def _auth(request, handler):
        request["app"] = request.headers.get("X-Test-App", "")
        request["user"] = request.headers.get("X-Test-User", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members/{slug}/projections", api_member_projections)
    return app


SECRET_URL = "https://evil.example/x?token=AKIAIOSFODNN7EXAMPLE"


def _member_log_root():
    """The directory a member log is actually written into.

    Not ``members_root()``: logs live under the crew-log root for the member kind, so
    a count anywhere else proves nothing. The conftest pins ``KIROCREW_HOME`` per test.
    """
    from kiro_crew.crew_log.store import crew_log_root
    from kiro_crew.eventlog.service import KIND_MEMBER

    return crew_log_root(KIND_MEMBER)


def _log_files() -> list[str]:
    root = _member_log_root()
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


class TestRosterIsAPureRead:
    @pytest.mark.asyncio
    async def test_a_get_creates_no_log_for_a_member_that_has_none(self, tmp_path, monkeypatch):
        """Opening the page is not an event in any member's life.

        A read of the roster must not bring a member's log into being -- a
        directory, a header write and an fsync each, for members who have done
        nothing. A member with no log has no recorded state that could be stale, so
        there is nothing for the read to correct and nothing for it to create.
        """
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)

        before = _log_files()

        async with TestClient(TestServer(_members_app(state))) as client:
            data = await (await client.get("/api/members")).json()

        after = _log_files()
        assert after == before, (
            "the roster read created files under the member log root for a member "
            f"with no log: {sorted(set(after) - set(before))}"
        )
        assert members.slug_for_name(CREW) not in set(
            get_service().slugs()
        ), "the read brought a member's log into existence"
        # The row is still served -- from live config, with an empty BASELINE. The
        # sequence is what makes it a baseline instead of a refusal: the client
        # mutes a slug's live frames when the roster sends a negative one, and this
        # member is about to receive its first frame the moment anything is
        # recorded about it.
        row = data["members"][0]
        assert row["projections"] == {"asOfSeq": 0, "values": {}}
        assert row["projections"]["asOfSeq"] >= 0, (
            "a member with no log was served the unattributable sentinel, which "
            "mutes the live frames that follow"
        )
        assert row["name"] == CREW, "the row itself must still be served"

    @pytest.mark.asyncio
    async def test_a_log_the_store_will_not_prove_is_refused_not_called_empty(
        self, tmp_path, monkeypatch
    ):
        """A log that exists but cannot be proved is a REFUSAL, never a fresh baseline.

        ``unit_ids`` skips a unit whose header it cannot read, so the log is missing from
        the listing. An empty baseline would keep the client's stale cached rows on show;
        the refusal sentinel clears the slug instead.
        """
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.MEMBER_RULES, {"text": "a recorded event"})
        assert slug in set(svc.slugs()), "the log must be enumerable before it is broken"

        # Break only the HEADER, leaving the directory and the file in place: this is
        # the exact state the enumeration drops and a filesystem check still sees.
        log_file = (
            _member_log_root()
            / sorted(p.name for p in _member_log_root().iterdir() if p.is_dir())[0]
        )
        segment = sorted(log_file.glob("*.jsonl"))[0]
        rest = segment.read_text(encoding="utf-8").splitlines()[1:]
        segment.write_text("\n".join(["not-a-json-header", *rest]) + "\n", encoding="utf-8")
        set_service(None)
        assert slug not in set(
            get_service().slugs()
        ), "the corrupted header must make the enumeration omit this slug"

        async with TestClient(TestServer(_members_app(state))) as client:
            data = await (await client.get("/api/members")).json()
        row = data["members"][0]
        assert row["projections"]["asOfSeq"] < 0, (
            "a log the store would not prove was served as an empty baseline "
            f"({row['projections']['asOfSeq']}), which preserves the stale rows the "
            "client already holds instead of clearing them"
        )
        assert row["name"] == CREW, "the row itself must still be served"

    @pytest.mark.asyncio
    async def test_the_drawer_route_errors_on_a_log_it_cannot_prove(self, tmp_path, monkeypatch):
        """The drawer gets an ERROR for an unprovable log, never an empty answer.

        The roster can blank one row among many, but the drawer's whole request is
        that one member. An empty answer there renders the affirmative "nothing
        scheduled" over a patrol state the read could not see, so the unprovable case
        takes the route's failure path and the error notice the page already has.
        """
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.MEMBER_RULES, {"text": "a recorded event"})

        unit_dir = sorted(p for p in _member_log_root().iterdir() if p.is_dir())[0]
        segment = sorted(unit_dir.glob("*.jsonl"))[0]
        rest = segment.read_text(encoding="utf-8").splitlines()[1:]
        segment.write_text("\n".join(["not-a-json-header", *rest]) + "\n", encoding="utf-8")
        set_service(None)

        async with TestClient(TestServer(_projections_app(state))) as client:
            resp = await client.get(f"/api/members/{slug}/projections?member={CREW}")
            status = resp.status
            payload = await resp.json()
        assert status == 500, (
            f"an unprovable log answered {status} rather than the route's error "
            "path, so the drawer renders it as read state"
        )
        assert payload["code"] == "member_projections_failed"

    @pytest.mark.asyncio
    async def test_a_log_created_after_the_listing_is_read_not_refused(self, tmp_path, monkeypatch):
        """A member's FIRST event must not cost that member its projections.

        A first event landing after the listing gives a log the listing never saw.
        Refusing it would clear the row and drop that frame, so the store is re-asked.
        """
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.MEMBER_RULES, {"text": "the member's first event"})
        real_seq = svc.last_seq(slug)
        assert real_seq >= 1, "the log must hold a real event for this to mean anything"

        # The race, reproduced exactly: the listing this read was given predates the
        # log, while the log itself is complete and provable by the time the row is
        # projected. One stale answer, so a fresh listing sees the log.
        stale = {"count": 0}
        real_logged_slugs = handlers_members._logged_slugs

        def _stale_once(service):
            stale["count"] += 1
            return set() if stale["count"] == 1 else real_logged_slugs(service)

        monkeypatch.setattr(handlers_members, "_logged_slugs", _stale_once)

        async with TestClient(TestServer(_members_app(state))) as client:
            data = await (await client.get("/api/members")).json()
        assert stale["count"] >= 2, (
            "the stale listing was never re-asked, so this test did not exercise the "
            "path it is pinning"
        )
        row = data["members"][0]
        assert row["projections"]["asOfSeq"] >= real_seq, (
            "a log created after the listing was served "
            f"{row['projections']['asOfSeq']} instead of a real position at or above "
            f"{real_seq}; the refusal sentinel would clear the row and drop the frame "
            "announcing that very first event"
        )
        assert "roster" in row["projections"]["values"], "the read must serve the log it found"

    @pytest.mark.asyncio
    async def test_the_drawer_route_reads_a_log_created_after_its_listing(
        self, tmp_path, monkeypatch
    ):
        """The drawer must not 500 on a member whose log has only just appeared."""
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.MEMBER_RULES, {"text": "the member's first event"})
        real_seq = svc.last_seq(slug)

        stale = {"count": 0}
        real_logged_slugs = handlers_members._logged_slugs

        def _stale_once(service):
            stale["count"] += 1
            return set() if stale["count"] == 1 else real_logged_slugs(service)

        monkeypatch.setattr(handlers_members, "_logged_slugs", _stale_once)

        async with TestClient(TestServer(_projections_app(state))) as client:
            resp = await client.get(f"/api/members/{slug}/projections?member={CREW}")
            status = resp.status
            payload = await resp.json()
        assert status == 200, (
            f"a log created after the listing answered {status}; the drawer would "
            "render an error for a member whose record has just begun"
        )
        assert payload["asOfSeq"] >= real_seq
        assert "roster" in payload["values"], "the route must serve the log it found"

    @pytest.mark.asyncio
    async def test_the_drawer_route_appends_nothing_even_when_config_has_drifted(
        self, tmp_path, monkeypatch
    ):
        """The per-member read is a READ: it never writes, not even a correction.

        The reconcile can undo a save landing mid-request, so it runs only on the roster
        read; a second writer here would reopen that window for one member's correction.
        """
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.MEMBER_RULES, {"text": "a recorded event"})

        # Drift the config so a reconcile WOULD have something to append.
        cfg.agents[CREW].model = "gpt-y"
        seq_before = svc.last_seq(slug)

        async with TestClient(TestServer(_projections_app(state))) as client:
            resp = await client.get(f"/api/members/{slug}/projections?member={CREW}")
            status = resp.status
            payload = await resp.json()
        assert status == 200
        assert svc.last_seq(slug) == seq_before, (
            "the per-member read appended to the log: it is a read path, and an append "
            "here can carry a config older than a save that landed since the load"
        )
        assert "roster" in payload["values"], "the route must still serve what it read"

    @pytest.mark.asyncio
    async def test_the_open_member_gets_all_four_views_from_its_own_route(
        self, tmp_path, monkeypatch
    ):
        """What the drawer mounts on: the list row's three missing views.

        The list row paints the roster line; the drawer paints the activity
        timeline, the patrol state and the driven-slot list. A narrow list is
        only correct if the drawer has its own way to get them, so this asserts the
        whole set arrives for the member that is open.
        """
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.ACTIVITY_RECORD, {"ts": 1.0, "member": CREW, "via": "chat"})
        svc.append(slug, types.PATROL_STARTED, {"slot_key": "member-code-reviewer"})
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "worker-1"})

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_projections_app(state))) as client:
            resp = await client.get(f"/api/members/{slug}/projections?member={CREW}")
            assert resp.status == 200, await resp.text()
            block = await resp.json()

        assert set(block["values"]) == {
            types.PROJ_ROSTER,
            types.PROJ_ACTIVITY,
            types.PROJ_WAKE,
            types.PROJ_DRIVING,
        }, f"the drawer's own route withheld a view it paints: {sorted(block['values'])}"
        assert isinstance(block["asOfSeq"], int) and block["asOfSeq"] >= 0

    @pytest.mark.asyncio
    async def test_the_per_member_route_creates_no_log_either(self, tmp_path, monkeypatch):
        """Opening a drawer is not an event either, and the baseline must match.

        A member with no log answers the same empty block the roster sends for that
        member, so the client seeds one shape from either source -- including its
        sequence, which is what decides whether later frames are applied or muted.
        """
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        state = _make_state(tmp_path)
        before = _log_files()

        async with TestClient(TestServer(_projections_app(state))) as client:
            block = await (
                await client.get(f"/api/members/{slug}/projections?member={CREW}")
            ).json()

        after = _log_files()
        assert after == before, f"the drawer read created {sorted(set(after) - set(before))}"
        assert block == {"asOfSeq": 0, "values": {}}

    @pytest.mark.asyncio
    async def test_both_routes_redact_the_same_planted_payload(self, tmp_path, monkeypatch):
        """One credential, both reads, neither ships it.

        The list and the drawer read the same folded views through different
        handlers, so the projection redaction chain has to run on both. A payload
        scrubbed on the list and shipped by the drawer is the same leak with a
        different URL.
        """
        import json

        cfg = _fake_config({CREW: _agent(avatar=SECRET_URL)})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        # Planted in BOTH an activity record (drawer-only) and the config-derived
        # avatar (carried by the roster view, so it reaches the list as well).
        svc.append(
            slug,
            types.ACTIVITY_RECORD,
            {"ts": 1.0, "member": CREW, "project": SECRET_URL, "via": "chat"},
        )
        state = _make_state(tmp_path)

        async with TestClient(TestServer(_members_app(state))) as client:
            listing = await (await client.get("/api/members")).json()
        async with TestClient(TestServer(_projections_app(state))) as client:
            block = await (
                await client.get(f"/api/members/{slug}/projections?member={CREW}")
            ).json()

        for label, payload in (("list", listing), ("per-member", block)):
            blob = json.dumps(payload)
            assert SECRET_URL not in blob, f"the {label} route shipped the planted URL"
            assert "AKIAIOSFODNN7EXAMPLE" not in blob, f"the {label} route shipped the credential"

        # Both carry the roster view, and both carry the SAME redacted avatar --
        # identical scrubbing, not merely both non-empty.
        listed = listing["members"][0]["projections"]["values"][types.PROJ_ROSTER]
        drawn = block["values"][types.PROJ_ROSTER]
        assert listed.get("avatar") == drawn.get("avatar"), (
            "the two routes redact the same field differently, so which handler "
            "served a value decides what the browser is shown"
        )
        # The drawer's activity view is present and scrubbed, not dropped.
        assert block["values"].get(types.PROJ_ACTIVITY) is not None

    @pytest.mark.asyncio
    async def test_a_row_carries_the_roster_view_only(self, tmp_path, monkeypatch):
        """The payload contract, asserted on a log that holds all four views.

        A member with activity, a patrol and an open slot still ships one view on
        its list row: the three the drawer paints are read per member, so a roster
        of any size costs one fold each rather than four.
        """
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.ACTIVITY_RECORD, {"ts": 1.0, "member": CREW, "via": "chat"})
        svc.append(slug, types.PATROL_STARTED, {"slot_key": "member-code-reviewer"})
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "worker-1"})
        state = _make_state(tmp_path)

        async with TestClient(TestServer(_members_app(state))) as client:
            data = await (await client.get("/api/members")).json()

        values = data["members"][0]["projections"]["values"]
        assert set(values) == {types.PROJ_ROSTER}, (
            "a list row carries a view nothing on it paints: " f"{sorted(values)}"
        )
        # asOfSeq is a property of the LOG, not of the subset, so the client's
        # higher-seq-wins rule still measures against the real sequence.
        assert data["members"][0]["projections"]["asOfSeq"] == svc.last_seq(slug)

    @pytest.mark.asyncio
    async def test_the_drawer_route_refuses_a_member_that_does_not_own_the_slug(
        self, tmp_path, monkeypatch
    ):
        """``?member=`` may not name one member while the path names another's log.

        A slug-placeholder header admits any name, which would let another member's
        config be reconciled into this log for good. Ownership is checked in the config.
        """
        other = "other-crew"
        cfg = _fake_config({CREW: _agent(model="claude-x"), other: _agent(model="claude-y")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        # Header holds the SLUG, the placeholder a writer with no name in hand
        # leaves: the case that passes the header check for every member.
        svc.ensure(slug, slug)
        svc.append(slug, types.ACTIVITY_RECORD, {"ts": 1.0, "member": CREW, "via": "chat"})
        seq_before = svc.last_seq(slug)
        state = _make_state(tmp_path)

        async with TestClient(TestServer(_projections_app(state))) as client:
            resp = await client.get(f"/api/members/{slug}/projections?member={other}")

        assert (
            resp.status == 400
        ), f"a member that does not derive this slug was served its log: {resp.status}"
        assert (
            svc.last_seq(slug) == seq_before
        ), "another member's config was appended to this member's log"

    @pytest.mark.asyncio
    async def test_one_roster_read_enumerates_the_member_store_once(self, tmp_path, monkeypatch):
        """The whole-roster enumeration is paid once per request, not per closure.

        ``slugs()`` is uncached and reads a header per member, so a second call doubles
        the cost for the same answer.
        """
        from kiro_crew.eventlog.service import get_service as _get

        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        svc = _get()
        svc.ensure(slug, CREW)
        state = _make_state(tmp_path)

        calls = {"n": 0}
        real_slugs = type(svc).slugs

        def _counting(self):
            calls["n"] += 1
            return real_slugs(self)

        monkeypatch.setattr(type(svc), "slugs", _counting)

        async with TestClient(TestServer(_members_app(state))) as client:
            data = await (await client.get("/api/members")).json()

        assert calls["n"] == 1, (
            f"one roster read enumerated the member store {calls['n']} times; the "
            "enumeration walks the root and reads a header per member, so each "
            "extra pass is the whole roster again for an answer already held"
        )
        assert data["members"][0]["projections"]["values"], (
            "the row lost its projection, so the single enumeration is not reaching "
            "both closures"
        )

    @pytest.mark.asyncio
    async def test_a_nameless_writer_still_gets_the_real_name_into_the_header(
        self, tmp_path, monkeypatch
    ):
        """A nameless writer's own create resolves the placeholder, not the read.

        ``ensure`` resolves a slug placeholder against live config on the fresh-create
        path. The name differs from its slug, or a resolved header would look unresolved.
        """
        import json

        name = "Review_Agent"
        slug = members.slug_for_name(name)
        assert slug != name, "this pin needs a name that differs from its slug"

        conf = tmp_path / "config.json"
        conf.write_text(
            json.dumps({"agents": {name: {"kiro_agent": "reviewer"}}}), encoding="utf-8"
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: conf)
        monkeypatch.setattr(
            "kiro_crew.config.loader.config_local_path", lambda: tmp_path / "config.local.json"
        )
        monkeypatch.setattr(members, "data_home", lambda: tmp_path)

        svc = get_service()
        assert slug not in set(svc.slugs()), "the log must not exist before the writer runs"

        # What `eventlog_hooks.emit` does for a writer holding no name: it passes
        # `name or slug`, so the slug arrives as the name, and no config is handed in.
        svc.ensure(slug, slug)

        assert svc.logged_name(slug) == name, (
            "a nameless writer locked the slug placeholder into the header, so this "
            f"member's own activity records scope out of their projection: "
            f"{svc.logged_name(slug)!r}"
        )
