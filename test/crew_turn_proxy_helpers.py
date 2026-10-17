"""Fakes and wiring shared by the ``test_crew_turn_proxy*.py`` modules.

The upstream crew is faked at the ``aiohttp.ClientSession`` the handler opens
(``_FakeSession`` and the response, post and content stand-ins behind it), and
``web.StreamResponse`` at a recorder (``_CapturingStream``), so no turn leaves the
process. ``_wire_handler`` stubs every collaborator ``api_crew_turn`` reaches out to,
``_connected`` is a tunnel status that yields a loopback target, ``_turn_request`` a
mocked request and ``_code`` the refusal code of a JSON response. ``_REAL_NODE`` and
``_OTHER_NODE`` are node ids of the shape the registry validates.
"""

from __future__ import annotations

from kiro_crew.dashboard import handlers_crew_turn as mod

#: Node ids of the shape the registry really validates: ``mi-`` and 8 to 17 hex
#: digits. A readable stand-in like ``mi-realcrew0000000`` is not one, so a row
#: carrying it cannot be written through ``reg.add`` at all.
_REAL_NODE = "mi-0a1b2c3d4e5f67890"
_OTHER_NODE = "mi-0dead0beef000111a"


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
            # launch tag, the node id is in ``ssm_target``. See ``test_crew_turn_proxy._lane_row``.
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
