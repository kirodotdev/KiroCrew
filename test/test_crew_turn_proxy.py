"""The headless-crew turn proxy: what it refuses, and what it never discloses.

``POST /api/instances/{id}/crew-turn`` makes the hub a headless crew's chat client,
so it holds the crew's control secret. Pinned here:

* the secret reaches the crew only: not the response, an error or a log line;
* the route refuses before any secret read unless the crew is connected and headless;
* "headless" comes from the provisioner, since the EC2 and MicroVM lanes share ``ssm``.

Handling of the crew's answer is in ``test_crew_turn_proxy_reply.py``; shared fakes are
in ``crew_turn_proxy_helpers.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from crew_turn_proxy_helpers import (
    _OTHER_NODE,
    _REAL,
    _REAL_NODE,
    _capture_stream,
    _CapturingStream,
    _code,
    _connected,
    _FakeResp,
    _install_session,
    _turn_request,
    _wire_handler,
)

from kiro_crew.dashboard import handlers_crew_turn as mod


def test_a_disconnected_crew_has_no_turn_target():
    """No target means the route refuses before any secret is read."""
    assert mod._turn_target({"state": "disconnected", "local_port": 18081}, "microvm") == ""
    assert mod._turn_target({"state": "connecting", "local_port": 18081}, "microvm") == ""


def test_a_connected_gateway_crew_has_no_turn_target():
    """A full-gateway crew is embedded, not chatted with through this route.

    The EC2 lane also uses ``ssm`` but serves a dashboard, so without a headless
    provisioner there is no target.
    """
    status = {"state": "connected", "local_port": 18081}
    assert mod._turn_target(status, "") == ""
    assert mod._turn_target(status, "ec2") == ""


def test_a_connected_headless_crew_gets_a_loopback_target():
    status = {"state": "connected", "local_port": 18081}
    for provisioner in sorted(mod.HEADLESS_CREW_PROVISIONERS):
        target = mod._turn_target(status, provisioner)
        assert target == "http://127.0.0.1:18081/v1/chat/completions", provisioner


def test_the_far_ends_own_turn_url_wins_over_a_composed_one():
    """``turn_url`` is the far end's statement about itself, so it is preferred."""
    status = {
        "state": "connected",
        "local_port": 18081,
        "turn_url": "http://127.0.0.1:19999/v1/chat/completions",
    }
    assert mod._turn_target(status, "microvm") == "http://127.0.0.1:19999/v1/chat/completions"


def test_the_control_header_matches_the_containers_own_constant():
    """One definition, asserted rather than copied by eye.

    The container is a separate source tree that ships inside an image, so a
    rename there cannot break this import -- which is exactly why the agreement
    needs a test instead of a shared symbol.
    """
    import importlib.util
    import pathlib

    config = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src/kiro_crew/apps/builtins/aws_control/crew/runtime/container/common/config.py"
    )
    spec = importlib.util.spec_from_file_location("_container_config_probe", config)
    assert spec is not None and spec.loader is not None
    text = config.read_text(encoding="utf-8")
    # Read as source rather than imported: the container package has its own
    # import root and loading it here would pull its dependencies.
    assert f'CONTROL_SECRET_HEADER = "{mod.CONTROL_SECRET_HEADER}"' in text


@pytest.mark.parametrize(
    "body,code",
    [
        ({}, "bad_request"),
        ({"message": "", "thread": "t"}, "bad_request"),
        ({"message": "hi"}, "bad_request"),
        ({"message": "hi", "thread": ""}, "bad_request"),
        ({"message": "hi", "thread": "t", "stream": "yes"}, "bad_request"),
        ({"message": "x" * (mod._MAX_MESSAGE_CHARS + 1), "thread": "t"}, "message_too_large"),
    ],
)
def test_the_body_contract_is_refused_by_shape(body, code):
    """Shape refusals happen before the crew is looked up, let alone the secret.

    Checked through the module's own validation order rather than a live request:
    what matters is that an unusable body cannot reach the secret read, and the
    order of the checks in ``_crew_turn`` is what guarantees it.
    """
    import inspect

    source = inspect.getsource(mod._crew_turn)
    body_checks = source.index("message must be a non-empty string")
    secret_read = source.index("_read_control_secret")
    assert body_checks < secret_read, (
        "the body contract must be checked before the control secret is read, or a "
        "malformed request causes a credential read"
    )
    # And every refusal above is a code this route actually produces.
    assert code in source or code == "bad_request"


class TestAHeadlessCrewIsNotMintedFor:
    """A crew with no dashboard has no dashboard token.

    Keyed on the provisioner id, not the transport: a MicroVM crew uses `ssm` like EC2,
    and a failed mint would tear down a forward that is fine.
    """

    def test_the_two_headless_lanes_are_recognised(self):
        from kiro_crew.instances.registry import is_headless_provisioner

        assert is_headless_provisioner("microvm")
        assert is_headless_provisioner("aws_fargate")

    def test_a_crew_with_a_dashboard_is_not_headless(self):
        """The EC2 lane shares the `ssm` method and DOES run a full gateway, so a
        transport-keyed answer would be wrong in both directions."""
        from kiro_crew.instances.registry import is_headless_provisioner

        assert not is_headless_provisioner("aws_ec2")
        assert not is_headless_provisioner("builtin")
        assert not is_headless_provisioner("")

    def test_the_registry_and_the_lanes_agree_on_the_ids(self):
        """One set, asserted against the lanes' own constants, so a rename is a
        failure rather than a crew the registry stops recognising."""
        from kiro_crew.cloud.microvm.engine import MICROVM_PROVISIONER_ID
        from kiro_crew.instances.registry import HEADLESS_CREW_PROVISIONERS
        from kiro_crew.platform.defaults import FARGATE_PROVISIONER_ID

        assert HEADLESS_CREW_PROVISIONERS == {FARGATE_PROVISIONER_ID, MICROVM_PROVISIONER_ID}

    def test_the_pane_and_the_tunnel_read_the_same_set(self):
        from kiro_crew.dashboard import handlers_crew_turn
        from kiro_crew.instances.registry import HEADLESS_CREW_PROVISIONERS

        assert handlers_crew_turn.HEADLESS_CREW_PROVISIONERS is HEADLESS_CREW_PROVISIONERS

    def test_the_mint_is_skipped_on_headlessness_not_on_transport(self):
        """Read from the source: the condition is what decides a live connect, and
        that seam is owned elsewhere, so the change is one condition per site."""
        import pathlib
        import sys

        import kiro_crew.instances.ssh_tunnel_manager as mod

        source = "".join(
            pathlib.Path(sys.modules[name].__file__).read_text(encoding="utf-8")
            for name in (mod.__name__, *mod._OWNERS)
        )
        assert source.count("not is_headless_provisioner(") == 2, (
            "both the connect mint and the self-heal prime must ask about the CREW; "
            "leaving one on the transport puts that path back where this one was"
        )
        assert 'if params.method != "fargate":' not in source, (
            "the mint still keys on the transport, so an ssm-reached headless crew " "is minted for"
        )

    def test_the_microvm_registration_targets_the_guests_front_port(self):
        """And not the gateway dashboard port the registry defaults to, which a
        headless crew serves nothing on."""
        from kiro_crew.cloud.microvm import recipe as recipe_mod
        from kiro_crew.instances.registry import DEFAULT_REMOTE_PORT

        assert recipe_mod.GUEST_FRONT_PORT != DEFAULT_REMOTE_PORT


def _lane_row(tmp_path, monkeypatch, *, tag, node_id, profile="", region=""):
    """The instance row the LANE really registers, written by the real registry.

    Not hand-built: ``reg.add`` derives the row's ``id`` from the launch tag and puts
    the ``mi-`` node id in ``ssm_target``. A hand-built ``id="mi-..."`` row is a shape
    production never writes, and tests against it pass while real crews get 503.
    """
    from kiro_crew.cloud import connect
    from kiro_crew.cloud.microvm import recipe as recipe_mod
    from kiro_crew.instances import registry as registry_mod
    from kiro_crew.platform.defaults import MICROVM_PROVISIONER_ID

    reg = registry_mod.InstancesRegistry(tmp_path / "instances.json")
    monkeypatch.setattr(registry_mod, "InstancesRegistry", lambda *a, **k: reg)
    row_id = connect.register_instance(
        node_id,
        name=tag,
        profile=profile,
        region=region,
        connection_method="ssm",
        provisioner_id=MICROVM_PROVISIONER_ID,
        ssm_run_as=recipe_mod.GUEST_RUN_AS,
        remote_port=recipe_mod.GUEST_FRONT_PORT,
    )
    assert row_id is not None, "the lane's own registration wrote no row"
    row = reg.get(row_id)
    assert row is not None
    return row


class TestTheRowTheLaneWritesResolvesItsOwnRecord:
    """The route has to work against the row ``connect.register_instance`` writes.

    The row's ``id`` is a slug of the launch tag and the node id is in ``ssm_target``,
    so a match on ``inst.id`` would end every real turn in ``503 crew_secret_unavailable``.
    """

    @staticmethod
    def _record(tmp_path, monkeypatch, **kw):
        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

        store = CrewStore(tmp_path / "crews.json")
        store.put(CrewRecord(**kw))
        monkeypatch.setattr("kiro_crew.cloud.microvm.record.CrewStore", lambda *a, **k: store)
        return store

    def test_the_registered_rows_id_is_the_tag_and_not_the_node(self, tmp_path, monkeypatch):
        """The premise, asserted rather than described: the two fields differ, so
        a route that reads one of them cannot be matching the other."""
        row = _lane_row(tmp_path, monkeypatch, tag="kc-22d27f", node_id=_REAL_NODE)
        assert row.ssm_target == _REAL_NODE
        assert row.id != _REAL_NODE, "the row's id is the node id, so this lane changed shape"
        assert row.id == "kc-22d27f"

    def test_the_secret_reference_resolves_for_a_really_registered_row(self, tmp_path, monkeypatch):
        """The turn this route exists for. Red while the match was on ``inst.id``."""
        self._record(
            tmp_path,
            monkeypatch,
            tag="kc-22d27f",
            mi_id=_REAL_NODE,
            control_secret_ref="kirocrew/crew/l2crew/CONTROL_SECRET",
            crew_name="l2crew",
        )
        row = _lane_row(tmp_path, monkeypatch, tag="kc-22d27f", node_id=_REAL_NODE)
        assert mod.control_secret_id(row) == "kirocrew/crew/l2crew/CONTROL_SECRET"

    def test_the_crew_name_resolves_for_a_really_registered_row(self, tmp_path, monkeypatch):
        self._record(tmp_path, monkeypatch, tag="kc-22d27f", mi_id=_REAL_NODE, crew_name="l2crew")
        row = _lane_row(tmp_path, monkeypatch, tag="kc-22d27f", node_id=_REAL_NODE)
        assert mod.served_crew_name(row) == "l2crew"

    @pytest.mark.asyncio
    async def test_a_turn_against_a_really_registered_row_is_not_refused_for_its_secret(
        self, tmp_path, monkeypatch
    ):
        """End to end through the handler, with only the AWS read faked.

        The refusal the broken binding produced was ``503
        crew_secret_unavailable``, and it came from the resolution step rather
        than from anything about the secret -- so the handler is the level this
        has to be pinned at, not just the resolver.
        """
        self._record(
            tmp_path,
            monkeypatch,
            tag="kc-22d27f",
            mi_id=_REAL_NODE,
            control_secret_ref="kirocrew/crew/l2crew/CONTROL_SECRET",
            crew_name="l2crew",
        )
        row = _lane_row(tmp_path, monkeypatch, tag="kc-22d27f", node_id=_REAL_NODE)
        # The real resolvers run: only the owner gate, the registry lookup, the
        # live status and the Secrets Manager read are stood in for. The live
        # tunnel's dial identity matches the row's node and (empty) account, so
        # the binding guard sees the open tunnel and the matched record agree.
        _wire_handler(
            monkeypatch,
            inst=row,
            status=_connected(ssm_target=_REAL_NODE, aws_profile="", aws_region=""),
            secret_id=_REAL,
            crew_name=_REAL,
            record=_REAL,
            secret="S3C",
        )
        calls: list[dict] = []
        _install_session(monkeypatch, calls, resp=_FakeResp(status=200, text='{"ok": true}'))

        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 200, _code(resp)
        assert len(calls) == 1, "the turn was refused before it reached the crew"
        assert calls[0]["json"]["model"] == "l2crew"
        assert calls[0]["headers"][mod.CONTROL_SECRET_HEADER] == "S3C"


class TestTheSecretIsNamedByTheLaneNotTheDisplayName:
    """A crew's display name is a label, and labels are not secret names.

    ``Kiro Crew Cloud (<tag>)`` is not a valid Secrets Manager name.
    """

    def test_a_recorded_reference_is_preferred(self, tmp_path, monkeypatch):
        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore
        from kiro_crew.dashboard.handlers_crew_turn import control_secret_id

        store = CrewStore(tmp_path / "crews.json")
        store.put(
            CrewRecord(
                tag="l2crew",
                mi_id=_REAL_NODE,
                control_secret_ref="kirocrew/crew/l2crew/CONTROL_SECRET",
            )
        )
        monkeypatch.setattr("kiro_crew.cloud.microvm.record.CrewStore", lambda *a, **k: store)

        # The record is matched on the node the row's tunnel dials, which the
        # registry keeps in ``ssm_target``; see
        # ``TestTheCredentialIsBoundToTheNodeTheTunnelDials`` for why neither a
        # label nor the row's id can stand in for it.
        class Inst:
            name = "Kiro Crew Cloud (l2crew)"
            id = "l2crew"
            ssm_target = _REAL_NODE
            connection_method = "ssm"
            aws_profile = ""
            aws_region = ""

        assert control_secret_id(Inst()) == "kirocrew/crew/l2crew/CONTROL_SECRET"

    def test_a_display_name_is_never_used_as_the_secret_name(self, tmp_path, monkeypatch):
        from kiro_crew.cloud.microvm.record import CrewStore
        from kiro_crew.dashboard.handlers_crew_turn import control_secret_id

        monkeypatch.setattr(
            "kiro_crew.cloud.microvm.record.CrewStore",
            lambda *a, **k: CrewStore(tmp_path / "empty.json"),
        )

        class Inst:
            name = "Kiro Crew Cloud (l2crew)"

        assert "Kiro Crew Cloud" not in control_secret_id(Inst())

    def test_an_instance_with_no_name_resolves_nothing(self, tmp_path, monkeypatch):
        """Reported as a crew that cannot be authenticated to, rather than a
        guessed name that reads some other crew's secret."""
        from kiro_crew.cloud.microvm.record import CrewStore
        from kiro_crew.dashboard.handlers_crew_turn import control_secret_id

        monkeypatch.setattr(
            "kiro_crew.cloud.microvm.record.CrewStore",
            lambda *a, **k: CrewStore(tmp_path / "empty.json"),
        )

        class Inst:
            name = ""

        assert control_secret_id(Inst()) == ""


class TestTheCrewIsAddressedByItsOwnNameNotTheLaunchTag:
    """``model`` names the crew, and the launch tag is not that name.

    The guest answers 404 ``crew_not_served_here`` unless ``model`` equals its
    ``SMC_CREW_NAME`` (from the bundle manifest), so the launch tag would never be served.

    This pins the host side; the guest's matcher is pinned in
    ``container_tests/test_front_crew_address.py``, which also names the tag shape this
    side can send.
    """

    @staticmethod
    def _store(tmp_path, monkeypatch, record=None):
        from kiro_crew.cloud.microvm.record import CrewStore

        store = CrewStore(tmp_path / "crews.json")
        if record is not None:
            store.put(record)
        monkeypatch.setattr("kiro_crew.cloud.microvm.record.CrewStore", lambda *a, **k: store)
        return store

    def test_a_tag_that_is_not_the_crew_name_still_resolves_the_name(self, tmp_path, monkeypatch):
        from kiro_crew.cloud.microvm.record import CrewRecord
        from kiro_crew.dashboard.handlers_crew_turn import served_crew_name

        self._store(
            tmp_path,
            monkeypatch,
            CrewRecord(tag="kc-22d27f", mi_id=_REAL_NODE, crew_name="l2crew"),
        )
        row = _lane_row(tmp_path, monkeypatch, tag="kc-22d27f", node_id=_REAL_NODE)
        assert served_crew_name(row) == "l2crew"

    def test_the_resolved_name_is_never_the_tag_even_when_both_are_present(
        self, tmp_path, monkeypatch
    ):
        """The red half of the contract, on this side: a record that carries both
        must yield the crew name. Sending the tag is the 404 the live take hit."""
        from kiro_crew.cloud.microvm.record import CrewRecord
        from kiro_crew.dashboard.handlers_crew_turn import served_crew_name

        self._store(
            tmp_path,
            monkeypatch,
            CrewRecord(tag="kc-22d27f", mi_id=_REAL_NODE, crew_name="l2crew"),
        )
        row = _lane_row(tmp_path, monkeypatch, tag="kc-22d27f", node_id=_REAL_NODE)
        resolved = served_crew_name(row)
        assert resolved == "l2crew"
        assert resolved != "kc-22d27f", "the display label reaches the guest as a 404"

    def test_an_unrecorded_name_resolves_to_nothing_rather_than_the_tag(
        self, tmp_path, monkeypatch
    ):
        """No fallback. A tag that happened to equal the crew name would make this
        look like it works and leave every other crew answering 404, so a record
        written before the name was stored resolves to nothing and the turn says
        so."""
        from kiro_crew.cloud.microvm.record import CrewRecord
        from kiro_crew.dashboard.handlers_crew_turn import served_crew_name

        self._store(tmp_path, monkeypatch, CrewRecord(tag="kc-22d27f", mi_id=_REAL_NODE))
        row = _lane_row(tmp_path, monkeypatch, tag="kc-22d27f", node_id=_REAL_NODE)
        assert served_crew_name(row) == ""

    def test_a_lane_with_no_headless_crew_resolves_nothing(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_crew_turn import served_crew_name

        class Inst:
            name = "some-ec2-box"
            provisioner_id = "aws_ec2"

        assert served_crew_name(Inst()) == ""


# Driving the turn handler itself.
#
# These drive ``api_crew_turn`` through each branch with a fake aiohttp
# ``ClientSession`` as the crew; the handler's own forward is real, only the socket is
# not. ``web.StreamResponse`` is a recorder, so the streamed path needs no transport.


class TestTheTurnHandlerRefusesBeforeItForwards:
    """Each early exit of ``api_crew_turn``, driven through the real control flow.

    These are the refusals the body-shape matrix above asserts the ORDER of; here
    the handler actually runs them, so the status and code each one returns are
    pinned rather than inferred from the source.
    """

    @pytest.mark.asyncio
    async def test_the_guards_denial_short_circuits_the_route(self, monkeypatch):
        """When the shared gate denies, nothing else -- no registry, no secret -- runs."""
        from aiohttp import web

        sentinel = web.json_response({"code": "denied"}, status=403)
        _wire_handler(monkeypatch, denied=sentinel)
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp is sentinel

    @pytest.mark.asyncio
    async def test_an_authenticated_non_owner_is_refused(self, monkeypatch):
        """The route runs with the owner's credentials, so a non-owner subject
        must not reach it even once the shared gate has let them through."""
        _wire_handler(monkeypatch, owner=False)
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 403
        assert _code(resp) == "owner_only"

    @pytest.mark.asyncio
    async def test_an_unavailable_registry_is_a_service_error(self, monkeypatch):
        _wire_handler(monkeypatch, reg_none=True)
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 503
        assert _code(resp) == "instances_unavailable"

    @pytest.mark.asyncio
    async def test_an_unknown_instance_is_a_not_found(self, monkeypatch):
        _wire_handler(monkeypatch, inst=None)
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 404
        assert _code(resp) == "instance_unknown"

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_json_is_refused(self, monkeypatch):
        _wire_handler(monkeypatch)
        resp = await mod.api_crew_turn(_turn_request(None, bad_json=True))
        assert resp.status == 400
        assert _code(resp) == "bad_request"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(["not", "a", "dict"], id="test_a_body_that_is_not_an_object_is_refused"),
            pytest.param({"message": "   ", "thread": "t"}, id="test_an_empty_message_is_refused"),
            pytest.param({"message": "hi", "thread": "  "}, id="test_an_empty_thread_is_refused"),
            pytest.param(
                {"message": "hi", "thread": "t", "stream": "yes"},
                id="test_a_non_boolean_stream_flag_is_refused",
            ),
        ],
    )
    async def test_a_malformed_body_is_refused(self, monkeypatch, body):
        _wire_handler(monkeypatch)
        resp = await mod.api_crew_turn(_turn_request(body))
        assert resp.status == 400
        assert _code(resp) == "bad_request"

    @pytest.mark.asyncio
    async def test_an_oversized_message_is_refused_without_a_round_trip(self, monkeypatch):
        """The hub caps the body here so an oversized prompt never crosses the tunnel."""
        _wire_handler(monkeypatch)
        body = {"message": "x" * (mod._MAX_MESSAGE_CHARS + 1), "thread": "t"}
        resp = await mod.api_crew_turn(_turn_request(body))
        assert resp.status == 413
        assert _code(resp) == "message_too_large"

    @pytest.mark.asyncio
    async def test_a_crew_that_is_not_connected_is_refused_before_any_secret(self, monkeypatch):
        """No turn target means the route stops before it reaches for the secret.

        The ``_read_control_secret`` stub is an ``AsyncMock``; asserting it was
        never awaited is the property the module's docstring names -- a
        misdirected turn causes no credential read.
        """
        _wire_handler(monkeypatch, status={"state": "disconnected"})
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 409
        assert _code(resp) == "crew_not_connected"
        assert mod._read_control_secret.await_count == 0

    @pytest.mark.asyncio
    async def test_a_tunnel_that_does_not_match_the_record_is_refused_before_any_secret(
        self, monkeypatch
    ):
        """The row was relabelled onto a tunnel that reaches a different node.

        The rewritten row matches a real crew's record, but the open tunnel was dialled
        to another node and does not rebuild. The credential binds to the tunnel's dial
        identity, so the secret read is never awaited.
        """
        # The matched record names the REAL crew's node; the live tunnel reaches
        # a DIFFERENT node (an SSH peer, or an SSM peer dialled elsewhere).
        record = SimpleNamespace(
            mi_id=_REAL_NODE, profile="prof", region="us-east-1", crew_name="l2crew"
        )
        _wire_handler(
            monkeypatch,
            status=_connected(ssm_target="mi-ffffffffffffffff0"),
            record=record,
        )
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 409
        assert _code(resp) == "crew_tunnel_mismatch"
        assert mod._read_control_secret.await_count == 0

    @pytest.mark.asyncio
    async def test_an_ssh_tunnel_never_binds_a_microvm_record(self, monkeypatch):
        """An SSM record cannot be served over an SSH forward.

        A row relabelled onto an already-connected SSH peer keeps the peer's
        tunnel, whose transport is ``ssh`` and which carries no SSM coordinates.
        The guard refuses it even though ``_record_for`` matched the record.
        """
        record = SimpleNamespace(
            mi_id=_REAL_NODE, profile="prof", region="us-east-1", crew_name="l2crew"
        )
        _wire_handler(
            monkeypatch,
            status=_connected(transport="ssh", ssm_target="", aws_profile="", aws_region=""),
            record=record,
        )
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 409
        assert _code(resp) == "crew_tunnel_mismatch"
        assert mod._read_control_secret.await_count == 0

    @pytest.mark.asyncio
    async def test_a_tunnel_to_another_guest_port_is_refused_before_any_secret(self, monkeypatch):
        """The forward reaches the record's node and account, but not the crew's front.

        The row's ``remote_port`` is agent-writable, so a forward built from an edited
        row can reach another listener in the guest -- here 8080, the platform's
        lifecycle-hook listener. The credential binds to the front port too, so the
        secret read is never awaited.
        """
        _wire_handler(monkeypatch, status=_connected(remote_port=8080))
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 409
        assert _code(resp) == "crew_tunnel_mismatch"
        assert mod._read_control_secret.await_count == 0

    @pytest.mark.asyncio
    async def test_an_unrecorded_secret_reference_is_a_service_error(self, monkeypatch):
        """No recorded reference, so the gateway has nothing to authenticate with."""
        _wire_handler(monkeypatch, status=_connected(), secret_id="")
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 503
        assert _code(resp) == "crew_secret_unavailable"
        assert mod._read_control_secret.await_count == 0

    @pytest.mark.asyncio
    async def test_an_unreadable_secret_is_a_service_error(self, monkeypatch):
        """A reference that resolves to nothing readable is one refusal, not two."""
        _wire_handler(monkeypatch, status=_connected(), secret=None)
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 503
        assert _code(resp) == "crew_secret_unavailable"

    @pytest.mark.asyncio
    async def test_a_crew_with_no_recorded_name_is_refused_before_the_forward(self, monkeypatch):
        """Its own refusal rather than a turn sent with the tag. A turn carrying
        the tag reaches the guest, which answers 404 ``crew_not_served_here`` --
        a crew the reader can see, connect and never talk to, with nothing in the
        pane saying why."""
        _wire_handler(monkeypatch, status=_connected(), crew_name="")
        calls: list[dict] = []
        _install_session(monkeypatch, calls)
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 503
        assert _code(resp) == "crew_name_unavailable"
        assert calls == []


class TestTheTurnHandlerForwardsToTheCrew:
    """The forward itself: the secret reaches the crew, and only the crew."""

    @pytest.mark.asyncio
    async def test_a_non_streamed_turn_relays_the_crews_answer(self, monkeypatch):
        inst = _wire_handler(monkeypatch, status=_connected(), secret="S3C")
        calls = []
        _install_session(
            monkeypatch,
            calls,
            resp=_FakeResp(status=200, text='{"ok": true}'),
        )
        body = {"message": "hello crew", "thread": "slot-1", "stream": False}
        resp = await mod.api_crew_turn(_turn_request(body))

        assert resp.status == 200
        assert resp.body == b'{"ok": true}'
        assert resp.content_type == "application/json"
        # The control secret went to the crew and lives in no part of the reply.
        assert len(calls) == 1
        assert calls[0]["url"] == "http://127.0.0.1:18081/v1/chat/completions"
        assert calls[0]["headers"][mod.CONTROL_SECRET_HEADER] == "S3C"
        assert b"S3C" not in resp.body
        # And the payload is the OpenAI shape the crew's front expects.
        sent = calls[0]["json"]
        # The crew this deployment SERVES, not the instance's display label.
        assert sent["model"] == "l2crew"
        assert sent["model"] != inst.name
        assert sent["id"] == "slot-1"
        assert sent["stream"] is False
        assert sent["messages"] == [{"role": "user", "content": "hello crew"}]

    @pytest.mark.asyncio
    async def test_a_non_streamed_turn_passes_the_crews_own_status_through(self, monkeypatch):
        """A crew that answers 4xx/5xx keeps its own status, not a masked one.

        The crew's words reach the caller as the refusal's bounded ``detail``
        rather than as the body, which is what ``crew_refused`` is.
        """
        import json as _json

        _wire_handler(monkeypatch, status=_connected())
        calls = []
        _install_session(monkeypatch, calls, resp=_FakeResp(status=503, text="busy"))
        body = {"message": "hi", "thread": "t", "stream": False}
        resp = await mod.api_crew_turn(_turn_request(body))
        assert resp.status == 503
        payload = _json.loads(resp.body)
        assert payload["code"] == "crew_refused"
        assert payload["status"] == 503
        assert payload["detail"] == "busy"

    @pytest.mark.asyncio
    async def test_a_non_streamed_turn_to_an_unreachable_crew_is_a_gateway_error(self, monkeypatch):
        """A transport failure is reported as a 502 and never leaks its type to the body."""
        _wire_handler(monkeypatch, status=_connected())
        calls = []
        _install_session(monkeypatch, calls, raise_on_post=RuntimeError("connection refused"))
        body = {"message": "hi", "thread": "t", "stream": False}
        resp = await mod.api_crew_turn(_turn_request(body))
        assert resp.status == 502
        assert _code(resp) == "crew_unreachable"
        assert b"connection refused" not in resp.body

    @pytest.mark.asyncio
    async def test_a_streamed_turn_relays_the_crews_frames_verbatim(self, monkeypatch):
        """The relay is thin: the crew's SSE bytes reach the pane unframed."""
        _wire_handler(monkeypatch, status=_connected(), secret="HDR")
        _capture_stream(monkeypatch)
        calls = []
        chunks = [b"data: {}\n\n", b"", b"data: [DONE]\n\n"]
        _install_session(monkeypatch, calls, resp=_FakeResp(status=200, chunks=chunks))
        body = {"message": "hi", "thread": "t"}  # stream defaults to True
        out = await mod.api_crew_turn(_turn_request(body))

        assert isinstance(out, _CapturingStream)
        assert out.prepared is True
        assert out.headers["Content-Type"] == "text/event-stream"
        assert out.headers["X-Accel-Buffering"] == "no"
        # The empty chunk is dropped; the two real frames are relayed as-is.
        assert out.frames == [b"data: {}\n\n", b"data: [DONE]\n\n"]
        assert calls[0]["json"]["stream"] is True
        assert calls[0]["headers"][mod.CONTROL_SECRET_HEADER] == "HDR"

    @pytest.mark.asyncio
    async def test_a_streamed_turn_reports_a_crew_refusal_as_a_frame(self, monkeypatch):
        """The head is already sent, so a non-200 upstream becomes an error FRAME.

        A status code cannot be sent any more; the pane learns of the refusal from
        a ``crew_refused`` event carrying the crew's own status and a bounded slice
        of its body, then a ``[DONE]`` so the reader stops waiting.
        """
        import json as _json

        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        calls = []
        _install_session(
            monkeypatch,
            calls,
            resp=_FakeResp(status=401, text="nope " * 200),
        )
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        assert out.status == 200  # the stream head was 200 before the upstream answered
        assert out.frames[-1] == b"data: [DONE]\n\n"
        first = _json.loads(out.frames[0][len(b"data: ") :].decode())
        assert first["error"]["code"] == "crew_refused"
        assert first["error"]["status"] == 401
        assert len(first["error"]["detail"]) <= 400

    @pytest.mark.asyncio
    async def test_a_streamed_turn_that_breaks_mid_body_closes_with_an_error_frame(
        self, monkeypatch
    ):
        """A mid-stream failure cannot be a status, so it is a frame plus ``[DONE]``.

        Closing silently would leave the pane waiting for a reply that is never
        coming, so the relay writes a ``crew_unreachable`` event even though some
        frames already went out.
        """
        import json as _json

        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        calls = []
        _install_session(
            monkeypatch,
            calls,
            resp=_FakeResp(
                status=200,
                chunks=[b"data: partial\n\n"],
                raise_at_end=RuntimeError("socket died"),
            ),
        )
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        assert out.frames[0] == b"data: partial\n\n"
        assert out.frames[-1] == b"data: [DONE]\n\n"
        error_frame = _json.loads(out.frames[-2][len(b"data: ") :].decode())
        assert error_frame["error"]["code"] == "crew_unreachable"
        assert not any(b"socket died" in frame for frame in out.frames)


class TestReadingTheControlSecret:
    """``_read_control_secret`` turns a Secrets Manager read into a value or ``None``.

    It never raises and never logs the VALUE: an absent, unreadable, or empty
    secret is a single ``None``, which the caller reports as one refusal, because
    distinguishing them would report whether a named secret exists.
    """

    @pytest.mark.asyncio
    async def test_a_present_secret_string_is_returned(self, monkeypatch):
        import kiro_crew.cloud.aws as aws

        monkeypatch.setattr(aws, "checked_json", lambda *a, **k: {"SecretString": "the-secret"})
        assert await mod._read_control_secret("sid", "prof", "us-east-1") == "the-secret"

    @pytest.mark.asyncio
    async def test_an_unreadable_secret_becomes_none_without_raising(self, monkeypatch):
        import kiro_crew.cloud.aws as aws

        def _boom(*a, **k):
            raise RuntimeError("access denied")

        monkeypatch.setattr(aws, "checked_json", _boom)
        assert await mod._read_control_secret("sid", "prof", "us-east-1") is None

    @pytest.mark.asyncio
    async def test_a_response_with_no_secret_string_is_none(self, monkeypatch):
        import kiro_crew.cloud.aws as aws

        monkeypatch.setattr(aws, "checked_json", lambda *a, **k: {})
        assert await mod._read_control_secret("sid", "prof", "us-east-1") is None

    @pytest.mark.asyncio
    async def test_a_non_dict_response_is_none(self, monkeypatch):
        import kiro_crew.cloud.aws as aws

        monkeypatch.setattr(aws, "checked_json", lambda *a, **k: "unexpected")
        assert await mod._read_control_secret("sid", "prof", "us-east-1") is None

    @pytest.mark.asyncio
    async def test_the_secret_value_is_never_logged(self, monkeypatch, caplog):
        import logging

        import kiro_crew.cloud.aws as aws

        def _boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(aws, "checked_json", _boom)
        with caplog.at_level(logging.INFO, logger=mod.logger.name):
            await mod._read_control_secret("secret/path/CONTROL", "prof", "us-east-1")
        # The crew's NAME and the exception TYPE may be logged; the value may not,
        # and here the read raised before any value existed to leak.
        assert "RuntimeError" in caplog.text
        assert "boom" not in caplog.text


class TestOnlyTheMicrovmLaneIsServed:
    """This route serves the MicroVM lane, and declines any other.

    A Fargate crew's name and secret resolve only from the live operator config, which
    names whichever crew is configured now, so a second crew in the same account could
    get the first one's credential. Declining leaves no branch to get wrong.
    """

    def test_the_served_lanes_are_exactly_the_microvm_lane(self):
        from kiro_crew.platform.defaults import FARGATE_PROVISIONER_ID, MICROVM_PROVISIONER_ID

        assert mod.TURN_LANE == MICROVM_PROVISIONER_ID
        assert FARGATE_PROVISIONER_ID != mod.TURN_LANE

    def test_a_fargate_crew_is_not_named_by_this_route(self):
        """``served_crew_name`` answers for a lane this route serves, and Fargate
        is not one -- so nothing here resolves a name from the configuration."""
        from types import SimpleNamespace

        inst = SimpleNamespace(name="Kiro Crew Cloud (kc-22d27f)", provisioner_id="aws_fargate")
        assert mod.served_crew_name(inst) == ""

    @pytest.mark.asyncio
    async def test_the_handler_declines_a_fargate_crew_before_reading_any_secret(self, monkeypatch):
        """And it declines EARLY. A lane this route does not serve must not reach
        the secret read or the address resolution at all."""
        import json

        inst = _wire_handler(monkeypatch, status=_connected(), secret="S3C")
        inst.provisioner_id = "aws_fargate"

        def _must_not_run(*a, **k):
            raise AssertionError("a declined lane reached the control-secret read")

        monkeypatch.setattr(mod, "control_secret_id", _must_not_run)
        monkeypatch.setattr(mod, "_read_control_secret", _must_not_run)

        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )

        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "crew_lane_not_served"


class TestControlSecretIdLanePrecedence:
    """``control_secret_id`` answers with this crew's recorded reference or nothing.

    A row with no record resolves to nothing: ``instances.json`` is not write-protected,
    so deriving from the row's label would let a relabelled row name any crew's secret.
    """

    def test_a_record_for_another_instance_is_not_borrowed(self, tmp_path, monkeypatch):
        """A record that is not THIS row's must yield nothing -- neither its
        reference nor a path derived from the row's own label."""
        from types import SimpleNamespace

        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

        store = CrewStore(tmp_path / "crews.json")
        store.put(
            CrewRecord(
                tag="someone-else",
                mi_id=_OTHER_NODE,
                control_secret_ref="kirocrew/crew/someone-else/CONTROL_SECRET",
            )
        )
        monkeypatch.setattr("kiro_crew.cloud.microvm.record.CrewStore", lambda *a, **k: store)

        inst = SimpleNamespace(
            name="Kiro Crew Cloud (l2crew)",
            provisioner_id="microvm",
            id="l2crew",
            ssm_target=_REAL_NODE,
            aws_profile="",
            aws_region="",
        )
        got = mod.control_secret_id(inst)
        assert "someone-else" not in got, "another instance's reference was borrowed"
        assert got == "", "a secret path was derived from the row's own label"

    def test_a_matched_record_without_a_reference_resolves_nothing(self, tmp_path, monkeypatch):
        """Identity matched, no reference stored: no name is guessed for the
        secret, so the route answers ``crew_secret_unavailable``."""
        from types import SimpleNamespace

        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

        store = CrewStore(tmp_path / "crews.json")
        store.put(CrewRecord(tag="l2crew", mi_id=_REAL_NODE))
        monkeypatch.setattr("kiro_crew.cloud.microvm.record.CrewStore", lambda *a, **k: store)

        inst = SimpleNamespace(
            name="Kiro Crew Cloud (l2crew)",
            provisioner_id="microvm",
            id="l2crew",
            connection_method="ssm",
            ssm_target=_REAL_NODE,
            aws_profile="",
            aws_region="",
        )
        assert mod.control_secret_id(inst) == ""


class _ExplodingStream(_CapturingStream):
    """A stream whose every ``write`` fails, modelling a client already gone."""

    async def write(self, data):
        self.frames.append(bytes(data))
        raise RuntimeError("broken pipe")


@pytest.mark.asyncio
async def test_a_streamed_turn_whose_error_frame_also_fails_does_not_raise(monkeypatch):
    """When even the error frame cannot be written, the handler returns, not raises.

    The relay's inner guard: the client has already dropped, so writing the
    ``crew_unreachable`` frame fails too. Letting that propagate would turn a
    gone client into a 500 and an unhandled task error; the handler swallows it
    and returns the (useless but closed) response.
    """
    _wire_handler(monkeypatch, status=_connected())
    monkeypatch.setattr(mod.web, "StreamResponse", _ExplodingStream)
    calls = []
    _install_session(
        monkeypatch,
        calls,
        resp=_FakeResp(status=200, chunks=[b"data: x\n\n"]),
    )
    out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
    assert isinstance(out, _ExplodingStream)
    assert len(calls) == 1
    # The relayed frame failed, then the error frame was attempted and failed too;
    # the [DONE] frame after it is never reached.
    assert out.frames == [
        b"data: x\n\n",
        b'data: {"error": {"code": "crew_unreachable"}}\n\n',
    ]


def _wire_resume(monkeypatch, *, state, raises=None, on_resume=None, on_lookup=None):
    """Stub the lane record lookup and the lifecycle resume the handler reaches.

    The handler imports both lazily inside a thread, so the stubs go on the
    modules it imports rather than on the handler. ``on_lookup`` records that the
    record store was consulted at all, which is how a test proves another lane is
    never asked about a suspend it does not have.
    """
    from types import SimpleNamespace

    import kiro_crew.cloud.config as cloud_config_mod
    from kiro_crew.cloud.microvm import record as record_mod
    from kiro_crew.cloud.microvm import wiring as wiring_mod

    class _Store:
        def get(self, tag):
            if on_lookup is not None:
                on_lookup(tag)
            return SimpleNamespace(tag=tag, state=state)

    class _Lifecycle:
        def resume(self, tag):
            if on_resume is not None:
                on_resume(tag)
            if raises is not None:
                raise raises
            return SimpleNamespace(tag=tag)

    monkeypatch.setattr(record_mod, "CrewStore", _Store)
    monkeypatch.setattr(wiring_mod, "production_lifecycle", lambda config, **kw: _Lifecycle())
    monkeypatch.setattr(
        cloud_config_mod.CloudConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(microvm_config=lambda: SimpleNamespace())),
    )


class TestTheCredentialIsBoundToTheNodeTheTunnelDials:
    """`instances.json` is NOT write-protected, so a row is agent-writable.

    Matching on a label (tag, display name, registry id) lets a relabelled hostile row
    receive a real crew's secret down its own tunnel. So the record is found by
    `ssm_target`, the field the forward dials, and its account and region must agree
    because they are what the secret is read with.
    """

    REAL_REF = "kirocrew/crew/realcrew/CONTROL_SECRET"

    def _store(self, tmp_path, monkeypatch):
        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

        store = CrewStore(tmp_path / "crews.json")
        store.put(
            CrewRecord(
                tag="realcrew",
                mi_id=_REAL_NODE,
                profile="owner",
                region="us-east-1",
                control_secret_ref=self.REAL_REF,
                crew_name="real",
            )
        )
        monkeypatch.setattr("kiro_crew.cloud.microvm.record.CrewStore", lambda *a, **k: store)
        return store

    class _Inst:
        def __init__(self, **kw):
            from kiro_crew.platform.defaults import MICROVM_PROVISIONER_ID

            self.name = ""
            # The slug of the launch tag, which is what `reg.add` writes here.
            self.id = "realcrew"
            self.connection_method = "ssm"
            self.ssm_target = ""
            self.provisioner_tag = ""
            # The real lane id, so the lane gate passes and these tests exercise
            # the record match rather than being refused one step earlier.
            self.provisioner_id = MICROVM_PROVISIONER_ID
            self.aws_profile = "owner"
            self.aws_region = "us-east-1"
            for k, v in kw.items():
                setattr(self, k, v)

    def test_the_real_crews_own_row_still_resolves(self, tmp_path, monkeypatch):
        """Non-vacuity: the binding must not refuse the legitimate crew."""
        from kiro_crew.dashboard.handlers_crew_turn import control_secret_id, served_crew_name

        self._store(tmp_path, monkeypatch)
        inst = self._Inst(ssm_target=_REAL_NODE, provisioner_tag="realcrew")
        assert control_secret_id(inst) == self.REAL_REF
        assert served_crew_name(inst) == "real"

    def test_a_relabelled_hostile_row_gets_no_credential(self, tmp_path, monkeypatch):
        """The attack: the hostile peer's own tunnel, wearing the real crew's tag."""
        from kiro_crew.dashboard.handlers_crew_turn import control_secret_id

        self._store(tmp_path, monkeypatch)
        hostile = self._Inst(ssm_target=_OTHER_NODE, provisioner_tag="realcrew")
        got = control_secret_id(hostile)
        assert got != self.REAL_REF, "the real crew's secret was handed to another instance"
        assert got == "", "a secret name was still guessed for a row that owns no record"

    def test_an_ssh_row_carrying_the_real_node_gets_no_credential(self, tmp_path, monkeypatch):
        """An ``ssh`` row dials its own host whatever ``ssm_target`` it holds, so
        copying a real crew's node, profile and region onto an SSH peer must not
        resolve that crew's secret."""
        from kiro_crew.dashboard.handlers_crew_turn import control_secret_id

        self._store(tmp_path, monkeypatch)
        hostile = self._Inst(connection_method="ssh", ssm_target=_REAL_NODE)
        assert control_secret_id(hostile) == ""

    def test_a_relabelled_display_name_gets_no_credential(self, tmp_path, monkeypatch):
        """Same attack through the display name, which the old tag parser read."""
        from kiro_crew.dashboard.handlers_crew_turn import control_secret_id

        self._store(tmp_path, monkeypatch)
        hostile = self._Inst(ssm_target=_OTHER_NODE, name="Kiro Crew Cloud (realcrew)")
        assert control_secret_id(hostile) == ""

    def test_a_relabelled_hostile_row_is_not_told_the_crew_name(self, tmp_path, monkeypatch):
        """`model` is also resolved from the record, and naming the real crew to a
        hostile front is what lets it answer as that crew instead of 404ing."""
        from kiro_crew.dashboard.handlers_crew_turn import served_crew_name

        self._store(tmp_path, monkeypatch)
        hostile = self._Inst(ssm_target=_OTHER_NODE, provisioner_tag="realcrew")
        assert served_crew_name(hostile) == ""

    def test_a_row_carrying_the_real_crews_id_but_another_node_gets_no_credential(
        self, tmp_path, monkeypatch
    ):
        """The binding must be on the field the tunnel goes down, not beside it.

        A row keeping the real crew's `id` but pointing `ssm_target` at another node
        would pass a check on `id` and send the secret down the other node's tunnel.
        """
        from kiro_crew.dashboard.handlers_crew_turn import control_secret_id, served_crew_name

        self._store(tmp_path, monkeypatch)
        # The row the reviewer described: `id` is the real crew's NODE id, which
        # is the field a check on `inst.id` compares, and `ssm_target` -- the
        # field the forward dials -- is another node entirely. Profile, region
        # and lane all agree, so nothing else refuses it.
        moved = self._Inst(id=_REAL_NODE, ssm_target=_OTHER_NODE, provisioner_tag="realcrew")
        assert control_secret_id(moved) == "", "the secret followed the row's id, not its node"
        assert served_crew_name(moved) == ""

    def test_a_row_with_no_node_at_all_gets_no_credential(self, tmp_path, monkeypatch):
        """There is nothing to match and nothing to dial, so there is no crew."""
        from kiro_crew.dashboard.handlers_crew_turn import control_secret_id

        self._store(tmp_path, monkeypatch)
        assert control_secret_id(self._Inst(ssm_target="", provisioner_tag="realcrew")) == ""

    @pytest.mark.parametrize(
        "field,value",
        [("aws_profile", "attacker"), ("aws_region", "eu-west-1")],
        ids=["profile-moved", "region-moved"],
    )
    def test_moved_aws_coordinates_get_no_credential(self, tmp_path, monkeypatch, field, value):
        """The secret is READ with the row's profile and region, so a row that
        keeps the node but moves the account points the read at an account the
        attacker controls, where that secret name can be made to exist."""
        from kiro_crew.dashboard.handlers_crew_turn import control_secret_id

        self._store(tmp_path, monkeypatch)
        inst = self._Inst(ssm_target=_REAL_NODE, provisioner_tag="realcrew", **{field: value})
        assert control_secret_id(inst) == ""


class TestTheFakeSessionStaysInTheHandler:
    """``_install_session`` fakes the handler's ``aiohttp.ClientSession`` and nothing else.

    Patching the package attribute leaks: ``aiohttp.test_utils``, first imported while
    the patch is live, keeps the fake after the undo, and later aiohttp test clients in
    the same worker cannot close it.
    """

    def test_the_package_keeps_its_real_client_session(self, monkeypatch):
        import aiohttp

        real = aiohttp.ClientSession
        _install_session(monkeypatch, [])
        assert aiohttp.ClientSession is real, "the fake reached the aiohttp package"
        assert mod.aiohttp.ClientSession is not real, "the handler does not see the fake"
        assert mod.aiohttp.ClientTimeout is aiohttp.ClientTimeout

    def test_the_handler_opens_the_fake(self, monkeypatch):
        calls = []
        _install_session(monkeypatch, calls)
        session = mod.aiohttp.ClientSession(timeout=mod.aiohttp.ClientTimeout(total=1))
        assert type(session).__name__ == "_FakeSession"
