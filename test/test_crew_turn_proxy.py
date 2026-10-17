"""The headless-crew turn proxy: what it refuses, and what it never discloses.

``POST /api/instances/{id}/crew-turn`` exists because a headless crew has no
dashboard to embed, so the hub has to be the crew's chat client. That puts a
credential in the hub's hands, and these tests pin the three properties that
follow from it rather than the forwarding mechanics:

* the crew's control secret reaches the CREW and nothing else -- not the
  response, not an error message, not a log line;
* the route refuses before it reaches for the secret at all when the crew is not
  a connected headless crew, so a misdirected turn never causes a secret read;
* "headless" is decided from the provisioner, not from the connection method,
  because the EC2 lane and the MicroVM lane share ``ssm`` and only one of them is
  headless.

Unit-level on purpose: the live end-to-end path is exercised against a real crew
in a real MicroVM, and what a test can add is the refusal matrix, which a live
run gets to see only one row of.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import handlers_crew_turn as mod


def test_a_disconnected_crew_has_no_turn_target():
    """No target means the route refuses before any secret is read."""
    assert mod._turn_target({"state": "disconnected", "local_port": 18081}, "microvm") == ""
    assert mod._turn_target({"state": "connecting", "local_port": 18081}, "microvm") == ""


def test_a_connected_gateway_crew_has_no_turn_target():
    """A full-gateway crew is embedded, not chatted with through this route.

    The EC2 lane reaches its crews with ``connection_method="ssm"`` -- the same
    method the MicroVM lane uses -- and its crews DO serve a dashboard. So a
    connected instance with a local port and no headless provisioner must not
    produce a target: posting a crew turn at a gateway's own port is a request
    that fails in a way nobody can read.
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


def test_the_secret_path_is_the_one_the_deploy_wrote():
    """Derived from the crew's launch tag, never supplied by the caller.

    A caller-supplied secret path would let any request name a secret for the
    gateway to read with the owner's credentials and send to an endpoint of the
    caller's choosing -- which is a read-any-secret primitive wearing a chat
    route's clothes.
    """
    assert (
        mod._SECRET_PATH.format(prefix="kirocrew/crew", crew="l2crew")
        == "kirocrew/crew/l2crew/CONTROL_SECRET"
    )
    from kiro_crew.cloud.microvm.engine import MicroVmLaunchSpec

    spec = MicroVmLaunchSpec(
        image_identifier="arn:aws:lambda:us-east-1:1:microvm-image:x",
        image_version="1.0",
        kms_key_id="",
    )
    # The lane's launcher and this route must name the SAME secret, or the hub
    # authenticates with a value the guest never read.
    assert spec.control_secret_name("l2crew") == mod._SECRET_PATH.format(
        prefix="kirocrew/crew", crew="l2crew"
    )


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
    text = config.read_text()
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
    order of the checks in ``api_crew_turn`` is what guarantees it.
    """
    import inspect

    source = inspect.getsource(mod.api_crew_turn)
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

    Skipping the mint on the `fargate` TRANSPORT is not enough. A MicroVM crew is
    reached over `ssm`, the same method the EC2 lane uses, so a transport-keyed
    skip lets it through to the mint -- which fails against a crew serving no
    dashboard. The connect path then returns an error status and tears down the
    forward it had already installed, so the pane reports a crew that will not
    answer about a forward that is fine.

    The decision is a property of the CREW, which is why it keys on the
    provisioner id.
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


#: Node ids of the shape the registry really validates: ``mi-`` and 8 to 17 hex
#: digits. A readable stand-in like ``mi-realcrew0000000`` is not one, so a row
#: carrying it cannot be written through ``reg.add`` at all.
_REAL_NODE = "mi-0a1b2c3d4e5f67890"
_OTHER_NODE = "mi-0dead0beef000111a"


def _lane_row(tmp_path, monkeypatch, *, tag, node_id, profile="", region=""):
    """The instance row the LANE really registers, written by the real registry.

    Driven through ``connect.register_instance`` with the launch path's own
    keyword arguments rather than hand-built, because the gap between the two is
    exactly where this route's binding broke. ``register_instance`` passes the
    launch TAG as ``name`` and the ``mi-`` node id as ``ssm_target``, and
    ``reg.add`` derives the row's ``id`` from the name -- so the id of every row
    this lane writes is a slug of the tag, which no crew record carries.

    A hand-built ``SimpleNamespace(id="mi-...")`` is a row shape production never
    writes. Every assertion made against one stays green while the route 503s on
    every real crew, which is how the binding shipped broken.
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

    ``reg.add(name=tag, ssm_target=<mi-id>)`` derives the row's ``id`` from the
    NAME, so the id is a slug of the launch tag and the node id lives in
    ``ssm_target``. A record match on ``inst.id`` therefore misses every record
    this lane ever wrote, and every turn against a real crew ends in
    ``503 crew_secret_unavailable``.

    These run against the real registry rather than a stand-in row, because the
    stand-in is what hid it.
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

    The Fargate lane registers crews as ``Kiro Crew Cloud (<tag>)``, which is not
    a valid Secrets Manager name at all, so deriving an id from it makes every
    turn report a secret it never looked for. An operator who moved
    ``microvm.secret_path_prefix`` is ignored by a hardcoded path for the same
    reason.
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

    def test_the_path_shape_takes_the_configured_prefix(self):
        """A hardcoded prefix ignores an operator who moved theirs."""
        from kiro_crew.dashboard.handlers_crew_turn import _SECRET_PATH

        assert "{prefix}" in _SECRET_PATH
        assert _SECRET_PATH.startswith("{prefix}")


class TestTheCrewIsAddressedByItsOwnNameNotTheLaunchTag:
    """``model`` names the crew, and the launch tag is not that name.

    The guest's front compares ``model`` against its own ``SMC_CREW_NAME`` and
    answers 404 ``crew_not_served_here`` for anything else. ``SMC_CREW_NAME``
    comes from the bundle's manifest, while the tag is this launch's id -- so a
    turn carrying the tag reaches a crew that can be seen, connected and never
    talked to.

    This half asserts what the HOST resolves. The guest's own matcher is asserted
    in its own suite -- ``container_tests/test_front_crew_address.py`` -- because
    that function returns a ``JSONResponse`` and so cannot be imported without the
    image's web dependencies, which this lane does not install. Two lanes, one
    invariant, each pinned where it can actually run, exactly as
    ``test_crew_runtime_payload.py`` splits the two packaging lanes.

    Two functions deciding what counts as addressing a crew is how one of them
    drifts, and the one that drifts is this side, which has no 404 to show for it.
    So the container-side test names the tag SHAPE this side can send.
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


# ======================================================================
# Driving the turn handler itself.
#
# ``api_crew_turn`` is the hub's side of the conversation: it validates the
# body, refuses before it reads a secret when the crew is not a connected
# headless crew, reads the control secret, and relays the crew's answer -- as a
# single JSON response, or as the SSE frames of a streamed turn. The refusals
# above pin the ORDER of the checks from the source; the tests below drive the
# handler through each branch with the far end faked, so no turn ever leaves the
# process.
#
# The upstream crew is a fake aiohttp ``ClientSession``: the handler's own
# forward (the control-secret header, the OpenAI-shaped payload, the SSE relay)
# is real, only the socket is not. ``web.StreamResponse`` is replaced with a
# recorder so the streamed path needs no transport.
# ======================================================================


class _FakeContent:
    """``resp.content`` for the stream path: yields chunks, then maybe raises."""

    def __init__(self, chunks, raise_at_end=None):
        self._chunks = list(chunks)
        self._raise_at_end = raise_at_end

    async def iter_any(self):
        for chunk in self._chunks:
            yield chunk
        if self._raise_at_end is not None:
            raise self._raise_at_end


class _FakeResp:
    """The upstream crew's response: a status, headers, and streamable content.

    ``text=`` is delivered through ``content`` and NOT through a ``text()``
    method, deliberately: the handler reads the body through ``content`` so it
    can stop at the cap, and a fake that still answered ``text()`` would stay
    green against a handler that read the body whole.

    ``content_type`` defaults to what the crew's front really answers on the path
    the fake is built for -- JSON when a body was given, SSE when chunks were --
    so a test that does not care about the content-type gate does not have to
    name one, and one that does names it explicitly.
    """

    def __init__(self, *, status=200, text="", chunks=(), raise_at_end=None, content_type=None):
        self.status = status
        if content_type is None:
            content_type = "text/event-stream" if chunks else "application/json"
        self.headers = {"Content-Type": content_type} if content_type else {}
        body = tuple(chunks) if chunks else ((text.encode(),) if text else ())
        self.content = _FakeContent(body, raise_at_end)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePost:
    """The context manager ``session.post`` returns."""

    def __init__(self, resp, raise_on_enter):
        self._resp = resp
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self):
        if self._raise_on_enter is not None:
            raise self._raise_on_enter
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """A stand-in for ``aiohttp.ClientSession`` that never opens a socket.

    Records every ``post`` so a test can assert the control secret reaches the
    crew and nothing but the crew, and so it can read back the exact OpenAI-shaped
    payload the hub composed.
    """

    def __init__(self, calls, *, resp=None, raise_on_post=None, raise_on_enter=None):
        self._calls = calls
        self._resp = resp
        self._raise_on_post = raise_on_post
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, *, json=None, headers=None, allow_redirects=True):
        self._calls.append(
            {"url": url, "json": json, "headers": headers, "allow_redirects": allow_redirects}
        )
        if self._raise_on_post is not None:
            raise self._raise_on_post
        return _FakePost(self._resp, self._raise_on_enter)


class _CapturingStream:
    """A ``web.StreamResponse`` replacement that records frames, not a socket.

    The streamed path calls ``prepare`` then ``write`` repeatedly; recording
    those lets a test read the relayed SSE frames without a prepared transport.
    """

    def __init__(self, *, status=200, headers=None):
        self.status = status
        self.headers = dict(headers or {})
        self.frames = []
        self.prepared = False

    async def prepare(self, request):
        self.prepared = True

    async def write(self, data):
        self.frames.append(bytes(data))


#: Passed to ``_wire_handler`` for a resolver that must NOT be stubbed, so a test
#: can drive the handler with the real record lookup in place. Distinct from
#: ``""``, which is the stub answering "nothing recorded".
_REAL = object()


def _install_session(monkeypatch, calls, **kwargs):
    """Replace the module's ``aiohttp.ClientSession`` with the recording fake."""

    def _factory(*args, **_kwargs):
        return _FakeSession(calls, **kwargs)

    monkeypatch.setattr(mod.aiohttp, "ClientSession", _factory)


def _capture_stream(monkeypatch):
    """Make the handler build a recording stream rather than a real one."""
    monkeypatch.setattr(mod.web, "StreamResponse", _CapturingStream)


def _wire_handler(
    monkeypatch,
    *,
    denied=None,
    owner=True,
    reg_none=False,
    inst="__present__",
    status=None,
    secret_id="kirocrew/crew/l2crew/CONTROL_SECRET",
    secret="s3cret-value",
    crew_name="l2crew",
    record="__match__",
):
    """Stub the handler's lazily-imported collaborators and secret reads.

    Everything the route reaches OUT to -- the owner gate, the registry, the live
    status, the two secret steps -- is replaced so each test drives one branch of
    ``api_crew_turn`` with the rest held fixed. Returns the instance the registry
    will hand back, so a test can read the name the payload must echo.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aiohttp import web

    import kiro_crew.dashboard.handlers._shared as sh
    import kiro_crew.dashboard.handlers.source_providers as sp
    import kiro_crew.dashboard.handlers_instances as hi

    monkeypatch.setattr(hi, "_guard", lambda request, operation: denied)
    monkeypatch.setattr(sp, "is_owner_dashboard_request", lambda request: owner)
    monkeypatch.setattr(
        sh,
        "_owner_denial_response",
        lambda request, detail="": web.json_response(
            {"code": "owner_only", "detail": detail}, status=403
        ),
    )

    if inst == "__present__":
        inst = SimpleNamespace(
            name="Kiro Crew Cloud (l2crew)",
            provisioner_id="microvm",
            # The row shape the lane really registers: the id is a slug of the
            # launch tag, the node id is in ``ssm_target``. See ``_lane_row``.
            id="kc-22d27f",
            ssm_target=_REAL_NODE,
            aws_profile="prof",
            aws_region="us-east-1",
        )
    reg = None if reg_none else SimpleNamespace(get=lambda instance_id: inst)
    monkeypatch.setattr(hi, "_registry", lambda state: reg)
    monkeypatch.setattr(hi, "_status_for", lambda state, instance_id: status or {})
    # ``_REAL`` leaves the module's own resolver in place, so a test can drive
    # the handler against a really-registered row and a real crew record.
    if secret_id is not _REAL:
        monkeypatch.setattr(mod, "control_secret_id", lambda i: secret_id)
    if crew_name is not _REAL:
        monkeypatch.setattr(mod, "served_crew_name", lambda i: crew_name)
    # The record ``api_crew_turn`` binds the live tunnel to. Stubbed to the crew
    # the instance really IS unless ``record`` is ``_REAL`` (then the module's own
    # resolver runs against a real ``CrewStore``). The default record matches the
    # row's node/profile/region, so the connected-path tests reach the branch they
    # are each pinning; a test driving the mismatch guard overrides ``status`` or
    # ``record`` so the two disagree.
    if record == "__match__":
        record = SimpleNamespace(
            mi_id=str(getattr(inst, "ssm_target", "") or ""),
            profile=str(getattr(inst, "aws_profile", "") or ""),
            region=str(getattr(inst, "aws_region", "") or ""),
            crew_name=crew_name if crew_name is not _REAL else "l2crew",
        )
    if record is not _REAL:
        monkeypatch.setattr(mod, "_record_for", lambda i: record)
    monkeypatch.setattr(mod, "_read_control_secret", AsyncMock(return_value=secret))
    return inst


def _connected(**extra):
    """A tunnel status that yields a loopback turn target for a headless crew.

    Carries the dial identity the live tunnel was built with -- transport ``ssm``
    on the real crew's node, profile and region -- so ``api_crew_turn``'s binding
    guard sees the open tunnel and the matched record agree. A test driving the
    mismatch itself overrides these keys (or the record) so they disagree.
    """
    base = {
        "state": "connected",
        "turn_url": "http://127.0.0.1:18081/v1/chat/completions",
        "transport": "ssm",
        "ssm_target": _REAL_NODE,
        "aws_profile": "prof",
        "aws_region": "us-east-1",
    }
    base.update(extra)
    return base


def _turn_request(body, *, instance_id="i1", bad_json=False):
    """A mocked ``POST /api/instances/{id}/crew-turn`` whose ``json()`` is *body*."""
    from unittest.mock import AsyncMock

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    app = web.Application()
    app["state"] = object()
    request = make_mocked_request(
        "POST",
        f"/api/instances/{instance_id}/crew-turn",
        match_info={"id": instance_id},
        app=app,
    )
    if bad_json:
        request.json = AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
    else:
        request.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return request


def _code(response):
    """The refusal code a non-streaming JSON response carries."""
    import json as _json

    return _json.loads(response.body)["code"]


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
    async def test_a_body_that_is_not_an_object_is_refused(self, monkeypatch):
        _wire_handler(monkeypatch)
        resp = await mod.api_crew_turn(_turn_request(["not", "a", "dict"]))
        assert resp.status == 400
        assert _code(resp) == "bad_request"

    @pytest.mark.asyncio
    async def test_an_empty_message_is_refused(self, monkeypatch):
        _wire_handler(monkeypatch)
        resp = await mod.api_crew_turn(_turn_request({"message": "   ", "thread": "t"}))
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
    async def test_an_empty_thread_is_refused(self, monkeypatch):
        _wire_handler(monkeypatch)
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "  "}))
        assert resp.status == 400
        assert _code(resp) == "bad_request"

    @pytest.mark.asyncio
    async def test_a_non_boolean_stream_flag_is_refused(self, monkeypatch):
        _wire_handler(monkeypatch)
        body = {"message": "hi", "thread": "t", "stream": "yes"}
        resp = await mod.api_crew_turn(_turn_request(body))
        assert resp.status == 400
        assert _code(resp) == "bad_request"

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

        ``_record_for`` matches the (now-rewritten) row to a real crew's record,
        but the OPEN tunnel was dialled on another node before the edit and does
        not rebuild on it. Binding the credential to the live tunnel's own dial
        identity -- not to the row -- is what stops the real crew's control secret
        from being read and sent down that tunnel. Asserting the secret read was
        never awaited is the property the module's docstring names.
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

    A Fargate crew is headless too, but serving it here needs the crew name and
    the control secret THAT task ran with, and both are resolvable only from the
    operator's configuration -- a live document naming whichever crew is
    configured now. With a second crew configured in the same account and region,
    a turn to the first would present the second one's credential to a task that
    must not see it. Recording the deployment on the crew's row at launch is the
    fix, and it belongs to that lane's launch path.

    So the lane is declined instead. Declining is the mechanism: there is no
    branch to get wrong and nothing on this path reads the configuration.
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
    """``control_secret_id`` prefers this crew's recorded reference, then a derive
    FROM THAT RECORD'S OWN TAG -- never from a label the row supplied.

    A row that owns no record resolves to nothing. Deriving a secret path from the
    tag in its display name instead would be the hole rather than a fallback:
    ``instances.json`` is not write-protected, so the label is agent-supplied, and
    a relabelled row would name whichever crew's secret its label claims. The
    derive is available only where the record is already matched by identity, so
    the tag it uses is the record's own.

    Nothing legitimate needs the wider form. Every record this lane's launch
    writes carries ``control_secret_ref`` from the start, and this function is
    called only from the turn route, which serves only this lane.
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

    def test_the_derive_still_runs_for_a_matched_record_without_a_reference(
        self, tmp_path, monkeypatch
    ):
        """The surviving half of the precedence: identity matched, no reference
        stored, so the path is derived -- from the RECORD's tag."""
        from types import SimpleNamespace

        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

        store = CrewStore(tmp_path / "crews.json")
        store.put(CrewRecord(tag="l2crew", mi_id=_REAL_NODE))
        monkeypatch.setattr("kiro_crew.cloud.microvm.record.CrewStore", lambda *a, **k: store)

        inst = SimpleNamespace(
            name="Kiro Crew Cloud (anything-else)",
            provisioner_id="microvm",
            id="anything-else",
            connection_method="ssm",
            ssm_target=_REAL_NODE,
            aws_profile="",
            aws_region="",
        )
        got = mod.control_secret_id(inst)
        assert got.endswith("/l2crew/CONTROL_SECRET"), "the record's own tag was not used"
        assert "anything-else" not in got, "the row's label reached the secret name"


class _ExplodingStream(_CapturingStream):
    """A stream whose every ``write`` fails, modelling a client already gone."""

    async def write(self, data):
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


class TestTheCrewsReplyIsRedactedBeforeItReachesTheCaller:
    """A crew's reply is model output, and model output can carry a credential.

    The crew runs with a Kiro identity and its own control secret in its
    environment, and it reads whatever its tools fetch. So a reply can quote a
    credential either because the model repeated its own environment or because an
    injected page told it to. This route's answer lands in the owner's browser and
    in a transcript, so it is redacted on the way out like every other peer reply.
    """

    _KEY = "AKIAIOSFODNN7EXAMPLE"

    @pytest.mark.asyncio
    async def test_a_non_streamed_reply_is_redacted(self, monkeypatch):
        import json

        _wire_handler(monkeypatch, status=_connected(), secret="S3C")
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(status=200, text=json.dumps({"answer": f"your key is {self._KEY}"})),
        )
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )

        assert self._KEY.encode() not in resp.body
        assert b"REDACTED" in resp.body
        # Still the JSON the caller has to parse, not a redacted blob.
        assert json.loads(resp.body)["answer"].startswith("your key is ")

    @pytest.mark.asyncio
    async def test_a_clean_reply_is_passed_through_unchanged(self, monkeypatch):
        """Non-vacuity: the redactor must not be rewriting every answer, or the
        assertions above would pass on a route that returned nothing useful."""
        import json

        _wire_handler(monkeypatch, status=_connected())
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, text='{"answer": "aarch64"}'))
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert json.loads(resp.body) == {"answer": "aarch64"}

    @pytest.mark.asyncio
    async def test_a_streamed_reply_is_redacted(self, monkeypatch):
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        event = b'data: {"delta": "key ' + self._KEY.encode() + b'"}\n\n'
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, chunks=(event,)))
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        written = b"".join(resp.frames)
        assert self._KEY.encode() not in written
        assert b"REDACTED" in written

    @pytest.mark.asyncio
    async def test_a_credential_split_across_two_chunks_is_still_caught(self, monkeypatch):
        """The property that makes this event-by-event rather than chunk-by-chunk.

        ``iter_any`` hands back whatever the socket delivered, so a credential can
        straddle two chunks. Redacting each chunk as it arrived would see neither
        half as a credential and forward both.
        """
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        whole = b'data: {"delta": "key ' + self._KEY.encode() + b'"}\n\n'
        cut = len(b'data: {"delta": "key ') + 8
        _install_session(
            monkeypatch, [], resp=_FakeResp(status=200, chunks=(whole[:cut], whole[cut:]))
        )
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        written = b"".join(resp.frames)
        assert self._KEY.encode() not in written, "a split credential was forwarded"
        assert b"REDACTED" in written

    @pytest.mark.asyncio
    async def test_a_refusal_detail_from_the_crew_is_redacted(self, monkeypatch):
        """The crew's refusal is the crew's own words too, and it can quote the
        request it refused."""
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        _install_session(
            monkeypatch, [], resp=_FakeResp(status=403, text=f"refused for {self._KEY}")
        )
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        written = b"".join(resp.frames)
        assert self._KEY.encode() not in written
        assert b"crew_refused" in written

    @pytest.mark.asyncio
    async def test_a_leading_bom_does_not_hide_an_escaped_credential(self, monkeypatch):
        """A BOM at the head of the stream must not cost the JSON-aware pass.

        ``_redact_sse_event`` sends a ``data:`` line through the JSON decoder, which
        is what sees a credential written as a JSON escape; every other line goes
        through the plain-text chain, which cannot. So a stream whose first line is
        ``\\ufeffdata: ...`` does not match ``data:``, takes the text chain, and the
        escaped credential passes through -- while the browser, which skips the BOM
        per the SSE spec, parses the field and decodes the credential back.
        """
        import json

        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        # The literal bytes never spell the key id: only a JSON decode produces it.
        escaped = b"\\u0041" + self._KEY[1:].encode()
        event = b"\xef\xbb\xbf" + b'data: {"delta": "key ' + escaped + b'"}\n\n'
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, chunks=(event,)))
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        written = b"".join(resp.frames).decode()
        # Read it the way a BOM-stripping EventSource client does.
        payload = "\n".join(
            line[len("data: ") :]
            for line in written.removeprefix("\ufeff").split("\n")
            if line.startswith("data: ")
        )
        assert self._KEY not in json.dumps(json.loads(payload)), (
            "the BOM sent the data line through the plain-text chain, so the "
            "escaped credential reached the caller"
        )

    @pytest.mark.asyncio
    async def test_a_bom_split_across_chunks_is_still_stripped(self, monkeypatch):
        """The BOM is three bytes and the socket may split it.

        Stripping only when the whole prefix has arrived is why the strip waits for
        the third byte instead of testing the first chunk and giving up.
        """
        import json

        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        escaped = b"\\u0041" + self._KEY[1:].encode()
        stream = b"\xef\xbb\xbf" + b'data: {"delta": "key ' + escaped + b'"}\n\n'
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(status=200, chunks=(stream[:1], stream[1:2], stream[2:])),
        )
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        written = b"".join(resp.frames).decode()
        payload = "\n".join(
            line[len("data: ") :]
            for line in written.removeprefix("\ufeff").split("\n")
            if line.startswith("data: ")
        )
        assert self._KEY not in json.dumps(json.loads(payload))


class TestTheCredentialIsBoundToTheNodeTheTunnelDials:
    """`instances.json` is NOT write-protected, so a row is agent-writable.

    Resolving the crew record by the row's TAG, its display name, or its registry
    id binds the credential to a label while the tunnel stays bound to a NODE. An
    agent that controls a connected hostile peer's row can relabel it with a real
    crew's tag, lane and AWS coordinates, and the owner's next turn then reads the
    REAL crew's control secret and sends it down the HOSTILE peer's already-open
    tunnel.

    So the record is found by `ssm_target`, which is the field the tunnel is
    actually built from -- the forward dials `validate_ssm_target(inst.
    ssm_target)`. Any other field leaves the same gap one step over: a row whose
    `ssm_target` names a node of the attacker's choosing passes a check on the id,
    and the secret goes down the tunnel that `ssm_target` opened. Its account and
    region have to agree with the record too, because they are what the secret is
    READ with.
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

        A check on the row's registry `id` is satisfied by a row that keeps the
        real crew's id -- its label -- and points `ssm_target` at a node of the
        attacker's choosing. Profile, region and lane all still agree, the record
        is matched, and the owner's next turn sends the real crew's control secret
        down the OTHER node's tunnel: the same relabelling attack, one field over.
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


class TestTheCrewsReplyIsBoundedAndTyped:
    """What the far end sends is not what this gateway agrees to hold or forward.

    The crew is a separate machine running a model with tools. Its reply can be
    arbitrarily large, of any content type, and -- once it is on the loop -- the
    redaction of it is CPU work on the sole event loop that also serves chat,
    websockets, timers and the heartbeat. So the reply is capped before it is
    read, typed before it is trusted, and scrubbed in a worker thread.

    The peer proxy already does all three; these pin that this route does too.
    """

    @staticmethod
    def _small_cap(monkeypatch, limit=64):
        """Shrink the cap this module reads, rather than build a real 8 MB reply."""
        monkeypatch.setattr(mod, "PROXY_REDACT_BUFFER_MAX_BYTES", limit)

    def test_the_cap_is_the_shared_one_and_not_a_local_number(self):
        """The shrink above is only legitimate if the module reads the shared cap."""
        from kiro_crew.instances import constants

        assert mod.PROXY_REDACT_BUFFER_MAX_BYTES == constants.PROXY_REDACT_BUFFER_MAX_BYTES

    @pytest.mark.asyncio
    async def test_an_oversized_non_streamed_reply_is_refused_not_buffered(self, monkeypatch):
        """``resp.text()`` would read all of it; the cap stops at the limit.

        What makes this a refusal rather than a truncation: a partial JSON
        document cannot be redacted as a document, so forwarding a prefix of it
        would forward bytes that were never checked.
        """
        _wire_handler(monkeypatch, status=_connected())
        self._small_cap(monkeypatch, 64)
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(
                status=200, chunks=[b"x" * 40, b"y" * 40], content_type="application/json"
            ),
        )
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 502
        assert _code(resp) == "crew_reply_too_large"
        assert b"yyyy" not in resp.body

    @pytest.mark.asyncio
    async def test_a_reply_at_the_cap_is_still_forwarded(self, monkeypatch):
        """Non-vacuity: the cap refuses what is OVER it, not everything."""
        import json

        _wire_handler(monkeypatch, status=_connected())
        body = json.dumps({"answer": "a" * 40})
        self._small_cap(monkeypatch, len(body))
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, text=body))
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 200
        assert json.loads(resp.body)["answer"] == "a" * 40

    @pytest.mark.asyncio
    async def test_the_redaction_does_not_run_on_the_event_loop(self, monkeypatch):
        """Walking and re-serializing a JSON document is CPU work, and on the loop
        it stalls every other connection this gateway is serving.

        Asserted by the THREAD the redactor observes itself on, not by reading the
        source for ``to_thread``: a call that was moved back onto the loop by any
        other route still fails this.
        """
        import threading

        _wire_handler(monkeypatch, status=_connected())
        seen: list[int] = []

        def _record(text):
            seen.append(threading.get_ident())
            return text

        monkeypatch.setattr(mod, "_redact_peer_payload", _record)
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, text='{"answer": "ok"}'))
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t", "stream": False}))

        assert seen, "the reply was forwarded without being redacted at all"
        assert seen[0] != threading.get_ident(), "the redaction ran on the event loop"

    @pytest.mark.asyncio
    async def test_a_non_json_reply_is_refused_before_its_body_is_read(self, monkeypatch):
        """A crew made to serve HTML must not have it land on the hub's own origin,
        where it would run as the authenticated owner."""
        _wire_handler(monkeypatch, status=_connected())
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(status=200, text="<script>alert(1)</script>", content_type="text/html"),
        )
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 502
        assert _code(resp) == "crew_content_type_refused"
        assert b"script" not in resp.body

    @pytest.mark.asyncio
    async def test_a_reply_with_no_content_type_is_refused(self, monkeypatch):
        _wire_handler(monkeypatch, status=_connected())
        _install_session(
            monkeypatch, [], resp=_FakeResp(status=200, text='{"ok": 1}', content_type="")
        )
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert _code(resp) == "crew_content_type_refused"

    @pytest.mark.asyncio
    async def test_a_charset_parameter_does_not_refuse_a_json_reply(self, monkeypatch):
        """``application/json; charset=utf-8`` is JSON. The gate reads the media
        type, so a crew that names its charset is not refused for it."""
        _wire_handler(monkeypatch, status=_connected())
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(
                status=200, text='{"ok": 1}', content_type="Application/JSON; charset=utf-8"
            ),
        )
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_a_streamed_200_that_is_not_sse_is_refused_as_a_frame(self, monkeypatch):
        """Without the gate a JSON body with no ``data:`` framing is ONE event to
        the reassembly, which sends it down ``_redact_sse_event``'s plain-text
        chain -- the chain that cannot see a credential written as a JSON escape.

        The head went out as 200 before the crew answered, so the refusal is a
        frame; what matters is that none of the body is relayed.
        """
        import json

        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(
                status=200,
                chunks=[b'{"choices": [{"delta": {"content": "hi"}}]}'],
                content_type="application/json",
            ),
        )
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        written = b"".join(out.frames)
        assert b"choices" not in written, "an unframed body was relayed to the pane"
        assert out.frames[-1] == b"data: [DONE]\n\n"
        first = json.loads(out.frames[0][len(b"data: ") :].decode())
        assert first["error"]["code"] == "crew_content_type_refused"

    @pytest.mark.asyncio
    async def test_a_real_sse_stream_is_still_relayed(self, monkeypatch):
        """Non-vacuity for the gate above."""
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(
                status=200,
                chunks=[b"data: {}\n\n"],
                content_type="text/event-stream; charset=utf-8",
            ),
        )
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert out.frames == [b"data: {}\n\n"]


class TestTheCrewsRefusalIsRedactedBeforeItIsCut:
    """The streamed error frame carries a slice of the crew's refusal body.

    Cut first and the slice is raw text, so a credential that straddles the cut
    is two fragments the redactor does not recognise and the leading one is
    forwarded -- and a raw-text pass has no JSON decode, which is the pass that
    sees a credential written as a ``\\uXXXX`` escape. So the body is capped,
    redacted whole, and only then cut.
    """

    _KEY = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _detail(out):
        import json

        return json.loads(out.frames[0][len(b"data: ") :].decode())["error"]["detail"]

    @pytest.mark.asyncio
    async def test_a_credential_straddling_the_cut_does_not_survive_in_part(self, monkeypatch):
        """Ten characters of an access key id is what cutting first forwarded."""
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        # The key starts at 390 and the frame keeps 400 characters, so a cut
        # taken BEFORE the redaction leaves its first ten characters behind.
        body = "refused: " + "x" * 381 + self._KEY + " trailing"
        assert (
            body.index(self._KEY)
            < mod._REFUSAL_DETAIL_CHARS
            < body.index(self._KEY) + len(self._KEY)
        ), "the fixture no longer straddles the cut"
        _install_session(monkeypatch, [], resp=_FakeResp(status=403, text=body))
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        detail = self._detail(out)
        assert self._KEY not in detail
        assert self._KEY[:10] not in detail, "the cut split the key and forwarded its head"
        assert len(detail) <= mod._REFUSAL_DETAIL_CHARS

    @pytest.mark.asyncio
    async def test_a_credential_written_as_a_json_escape_is_redacted(self, monkeypatch):
        """``redact_peer_text`` runs on the bytes as they arrived, so an escaped
        credential reads as prose to it and as a credential to the browser.
        ``_redact_peer_payload`` decodes the document first."""
        import json

        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        # The literal bytes never spell the key id: only a JSON decode produces it.
        escaped = '{"detail": "refused for \\u0041' + self._KEY[1:] + '"}'
        assert self._KEY not in escaped
        _install_session(monkeypatch, [], resp=_FakeResp(status=403, text=escaped))
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        detail = self._detail(out)
        # Read it the way the pane does: the frame's detail is itself a JSON
        # document here, so decode it before looking for the key.
        decoded = json.dumps(json.loads(detail)) if detail.startswith("{") else detail
        assert self._KEY not in decoded, "the escaped credential reached the pane"

    @pytest.mark.asyncio
    async def test_an_oversized_refusal_body_is_not_buffered_whole(self, monkeypatch):
        """And the frame says the detail was withheld rather than carrying nothing."""
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        monkeypatch.setattr(mod, "PROXY_REDACT_BUFFER_MAX_BYTES", 32)
        _install_session(monkeypatch, [], resp=_FakeResp(status=500, chunks=[b"z" * 20, b"w" * 20]))
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        detail = self._detail(out)
        assert "wwww" not in detail
        assert "too large" in detail

    @pytest.mark.asyncio
    async def test_the_crews_own_status_still_reaches_the_frame(self, monkeypatch):
        """Non-vacuity: the refusal frame is still a refusal frame."""
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        _install_session(monkeypatch, [], resp=_FakeResp(status=404, text="no such crew"))
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        import json

        error = json.loads(out.frames[0][len(b"data: ") :].decode())["error"]
        assert error["status"] == 404
        assert error["detail"] == "no such crew"


class TestANonStreamedCrewErrorIsARefusalNotAContentTypeComplaint:
    """A crew's 4xx/5xx on the non-streamed path is reported as what it is.

    The content-type gate is for a reply this route would FORWARD. A refusal body
    is never forwarded, so gating it by type reported the crew's error as
    ``crew_content_type_refused`` -- which names the gateway's own check and
    hides the status and the words the crew actually sent. Both types of refusal
    body now become ``crew_refused``, carrying the crew's status and the same
    capped, redacted slice the streamed frame carries.
    """

    _KEY = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _payload(resp):
        import json as _json

        return _json.loads(resp.body)

    async def _turn(self, monkeypatch, resp):
        _wire_handler(monkeypatch, status=_connected())
        _install_session(monkeypatch, [], resp=resp)
        return await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )

    @pytest.mark.asyncio
    async def test_a_json_error_is_the_crews_refusal(self, monkeypatch):
        resp = await self._turn(
            monkeypatch,
            _FakeResp(status=400, text='{"detail": "no such slot"}'),
        )
        payload = self._payload(resp)
        assert resp.status == 400
        assert payload["code"] == "crew_refused"
        assert payload["status"] == 400
        assert "no such slot" in payload["detail"]

    @pytest.mark.asyncio
    async def test_a_text_plain_error_is_the_crews_refusal_too(self, monkeypatch):
        """A refusal body's content type does not decide which refusal is reported."""
        resp = await self._turn(
            monkeypatch,
            _FakeResp(status=429, text="slow down", content_type="text/plain"),
        )
        payload = self._payload(resp)
        assert payload["code"] == "crew_refused"
        assert payload["code"] != "crew_content_type_refused"
        assert payload["status"] == 429
        assert payload["detail"] == "slow down"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_a_forward_that_changed_mid_turn_gets_no_secret(self, monkeypatch, stream):
        """The tunnel drops while the secret is read and another forward takes the
        port. The second status read sees a new connection, so nothing is posted."""
        import kiro_crew.dashboard.handlers_instances as hi

        calls: list = []
        _wire_handler(monkeypatch, status=_connected(connected_at=1.0, local_port=18081))
        reads = iter(
            [
                _connected(connected_at=1.0, local_port=18081),
                _connected(connected_at=2.0, local_port=18081),
            ]
        )
        monkeypatch.setattr(hi, "_status_for", lambda state, instance_id: next(reads))
        if stream:
            _capture_stream(monkeypatch)
        _install_session(monkeypatch, calls, resp=_FakeResp(status=200, text="{}"))
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": stream})
        )
        assert calls == [], "the control secret was posted to a forward that changed"
        if stream:
            assert any(b"crew_tunnel_changed" in f for f in resp.frames), resp.frames
        else:
            assert resp.status == 409
            assert _code(resp) == "crew_tunnel_changed"

    @pytest.mark.asyncio
    async def test_a_redirect_is_refused_and_not_forwarded(self, monkeypatch):
        """A 3xx is never followed, so it is answered as a refusal, never relayed."""
        resp = await self._turn(
            monkeypatch,
            _FakeResp(status=307, text='{"choices": []}'),
        )
        payload = self._payload(resp)
        assert resp.status == 502
        assert payload["code"] == "crew_refused"
        assert payload["status"] == 307

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_neither_path_follows_a_redirect(self, monkeypatch, stream):
        """Following a redirect would carry the control secret out of the tunnel."""
        calls: list = []
        _wire_handler(monkeypatch, status=_connected())
        if stream:
            _capture_stream(monkeypatch)
            resp = _FakeResp(status=307, chunks=(b"",), content_type="text/event-stream")
        else:
            resp = _FakeResp(status=307, text="{}")
        _install_session(monkeypatch, calls, resp=resp)
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t", "stream": stream}))
        assert calls, "the crew was never called"
        assert all(c["allow_redirects"] is False for c in calls), calls

    @pytest.mark.asyncio
    async def test_a_text_plain_200_is_still_refused_for_its_type(self, monkeypatch):
        """Non-vacuity: the gate still holds for a reply this route would forward."""
        resp = await self._turn(
            monkeypatch,
            _FakeResp(status=200, text="not json at all", content_type="text/plain"),
        )
        assert resp.status == 502
        assert _code(resp) == "crew_content_type_refused"
        assert b"not json at all" not in resp.body

    @pytest.mark.asyncio
    async def test_the_detail_is_redacted_before_it_is_cut(self, monkeypatch):
        """Same order as the streamed frame: capped, redacted whole, then cut.

        Cut first and a credential straddling the cut is two fragments the
        redactor does not recognise, and the leading one is forwarded.
        """
        body = "refused: " + "x" * 381 + self._KEY + " trailing"
        assert (
            body.index(self._KEY)
            < mod._REFUSAL_DETAIL_CHARS
            < body.index(self._KEY) + len(self._KEY)
        ), "the fixture no longer straddles the cut"
        resp = await self._turn(monkeypatch, _FakeResp(status=403, text=body))
        payload = self._payload(resp)
        assert payload["code"] == "crew_refused"
        detail = payload["detail"]
        assert self._KEY not in detail
        assert self._KEY[:10] not in detail, "the cut split the key and forwarded its head"
        assert len(detail) <= mod._REFUSAL_DETAIL_CHARS

    @pytest.mark.asyncio
    async def test_an_oversized_error_body_is_not_buffered_whole(self, monkeypatch):
        """And the detail says it was withheld rather than carrying nothing."""
        _wire_handler(monkeypatch, status=_connected())
        monkeypatch.setattr(mod, "PROXY_REDACT_BUFFER_MAX_BYTES", 32)
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(status=500, chunks=[b"z" * 20, b"w" * 20], content_type="text/plain"),
        )
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        detail = self._payload(resp)["detail"]
        assert "wwww" not in detail
        assert "too large" in detail


class TestEveryTurnIsAudited:
    """A turn sends this crew's control secret down a tunnel, so the SEL trail has
    to hold the turns that WORKED and not only the ones that were refused.

    Nothing else records it: the route logs a line for each failure arm and
    nothing at all for a success, so a reader asking which crew this gateway
    authenticated to, and when, had no record to read.
    """

    @staticmethod
    def _record(monkeypatch):
        events: list[tuple] = []

        def _audit(operation, outcome, *, request_id="", error=""):
            events.append((operation, outcome, request_id, error))

        monkeypatch.setattr(mod, "_audit", _audit)
        return events

    def test_the_route_audits_under_one_operation_name(self):
        """The shared gate audits its own denials with this name, so a reader sees
        one operation per turn rather than two that have to be joined."""
        import inspect

        assert mod._AUDIT_OP == "crew_turn"
        assert '_guard(request, "crew_turn")' in inspect.getsource(mod.api_crew_turn)

    @pytest.mark.asyncio
    async def test_a_successful_non_streamed_turn_is_audited(self, monkeypatch):
        _wire_handler(monkeypatch, status=_connected())
        events = self._record(monkeypatch)
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, text='{"ok": 1}'))
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 200
        assert ("crew_turn", "success", "i1", "") in events

    @pytest.mark.asyncio
    async def test_a_non_streamed_crew_error_is_not_audited_as_a_success(self, monkeypatch):
        """The crew answered, so the turn did not fail -- but it did not succeed
        either, and a trail that calls a 500 a success cannot be read for which
        turns the crew actually took. Audited as ``declined``, the same outcome
        the streamed path records for the same refusal."""
        _wire_handler(monkeypatch, status=_connected())
        events = self._record(monkeypatch)
        _install_session(monkeypatch, [], resp=_FakeResp(status=500, text='{"detail": "boom"}'))
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t", "stream": False}))

        outcomes = [outcome for _, outcome, _, _ in events]
        assert "success" not in outcomes
        assert ("crew_turn", "declined", "i1", "crew refused with 500") in events

    @pytest.mark.asyncio
    async def test_a_successful_streamed_turn_is_audited(self, monkeypatch):
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        events = self._record(monkeypatch)
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, chunks=[b"data: {}\n\n"]))
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert ("crew_turn", "success", "i1", "") in events

    @pytest.mark.asyncio
    async def test_a_stream_that_ends_unredactable_is_audited_as_partial_not_success(
        self, monkeypatch
    ):
        """Some frames went out and the rest were dropped, which is neither of the
        two outcomes a reader would otherwise infer."""
        from kiro_crew.dashboard.handlers_instances import ProxyReplyUnredactable

        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        events = self._record(monkeypatch)

        async def _refuse_event(event):
            raise ProxyReplyUnredactable()

        monkeypatch.setattr(mod, "_redact_sse_event_async", _refuse_event)
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, chunks=[b"data: {}\n\n"]))
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        outcomes = [outcome for _, outcome, _, _ in events]
        assert "partial" in outcomes
        assert "success" not in outcomes

    @pytest.mark.asyncio
    async def test_an_unreachable_crew_is_audited_as_a_failure(self, monkeypatch):
        _wire_handler(monkeypatch, status=_connected())
        events = self._record(monkeypatch)
        _install_session(monkeypatch, [], raise_on_post=RuntimeError("connection refused"))
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t", "stream": False}))

        failures = [e for e in events if e[1] == "failure"]
        assert failures, "an unreachable crew left no audit record"
        # The exception TYPE, never its message: the message can quote the target.
        assert failures[-1][3] == "RuntimeError"
        assert not any("connection refused" in e[3] for e in events)

    @pytest.mark.asyncio
    async def test_a_declined_lane_is_audited(self, monkeypatch):
        inst = _wire_handler(monkeypatch, status=_connected())
        inst.provisioner_id = "aws_fargate"
        events = self._record(monkeypatch)
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t", "stream": False}))
        assert any(outcome == "declined" for _, outcome, _, _ in events)

    @pytest.mark.asyncio
    async def test_an_unavailable_secret_is_audited(self, monkeypatch):
        _wire_handler(monkeypatch, status=_connected(), secret_id="")
        events = self._record(monkeypatch)
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t", "stream": False}))
        assert any(outcome == "failure" for _, outcome, _, _ in events)
