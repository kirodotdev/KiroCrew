"""Doubles shared by the ``test_instances_crew_reads*.py`` family.

``_FakeContent`` and ``_FakeResp`` stand in for an aiohttp response the tunnel
manager yields; ``CrewReadsCase`` carries the per-test builders every part uses:
the registry, the manager double whose peer is a current build by default, the
internal-secret request shapes (owner tab, channel-born, keyed, bare), and the
live-slot registry the gate reads the person's presence from. The two test
modules subclass it so their bodies read ``self._mgr_with(...)`` unchanged.
"""

from __future__ import annotations

import json
import types

from instances_helpers import _FakeReq, _State


class _FakeContent:
    """Minimal aiohttp resp.content stand-in: one-chunk async iterator."""

    def __init__(self, body: bytes):
        self._body = body

    async def iter_chunked(self, _n):
        if self._body:
            yield self._body


class _FakeResp:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None):
        self.status = status
        self.content = _FakeContent(body)
        self.headers = dict(headers or {})


class CrewReadsCase:
    """Per-test builders for the crew-scoped session read handlers."""

    def _reg(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        return InstancesRegistry(path=tmp_path / "instances.json")

    def _internal_req(self, state, **query):
        # user=None models the real transport: token_auth's internal-secret branch
        # sets `internal_auth` and derives identity from the CALLING SESSION, and
        # never publishes a dashboard `user` claim (see `derive_caller_app`). A
        # fake that supplied one would exercise a shape this route never sees.
        # The calling session is the owner's canonical tab, attested by the
        # Unix-socket peer check (`peer_verified`), as a real agent call arrives.
        req = _FakeReq(state, query=query, user=None, headers={"X-Session-Key": "dashboard:ui"})
        req._attrs["internal_auth"] = True
        req._attrs["peer_verified"] = True
        if getattr(state, "_slots", None) is None:
            # The gate admits only a live tab running the person's turn; a wire
            # test that did not build one gets the ordinary owner's tab.
            state._slots = self._slots(ui=self._tab())
        return req

    def _channel_req(self, state, **query):
        """An internal-secret request whose CALLING SESSION is channel-born.

        This is the identity an app-claim-only check could not see: a
        channel/cron-born slot is created without an ``_app``, so
        ``derive_caller_app`` returns ``""`` and ``request["app"]`` is left
        absent — the Slack participant arrives shaped exactly like the owner.
        """
        req = _FakeReq(
            state, query=query, user=None, headers={"X-Session-Key": "slack:1712345.678"}
        )
        req._attrs["internal_auth"] = True
        # Attested: the refusal under test is the ROSTER's, not the attestation's.
        req._attrs["peer_verified"] = True
        return req

    def _mgr_with(
        self,
        *,
        search=None,
        list_body=None,
        read_body=None,
        raise_exc=None,
        peer_confirms_exclusion=True,
        read_status=200,
        list_status=200,
    ):
        """A manager double whose peer is, by default, a CURRENT build.

        A current peer answers every guarded leg (search, list, read) with
        ``INCOGNITO_EXCLUDED_HEADER``, its proof that it understood
        ``exclude_incognito=1``. ``peer_confirms_exclusion=False`` models the older
        peer that ignores the unknown parameter and serves unfiltered rows with no
        header -- the case every leg must refuse. ``search`` keeps the
        ``(ok, payload)`` shape the earlier carrier returned: ``(True, body)`` is a
        200 with that JSON body, ``(False, {...})`` a peer 502 whose body the hub
        must not echo.
        """
        import contextlib

        from kiro_crew.history import INCOGNITO_EXCLUDED_HEADER

        confirm = {INCOGNITO_EXCLUDED_HEADER: "1"} if peer_confirms_exclusion else {}
        if isinstance(search, tuple):
            search_status = 200 if search[0] else 502
            search_body = json.dumps(search[1]).encode()
        else:
            search_status, search_body = 200, b'{"sessions": []}'

        class FakeMgr:
            body_reads = 0

            @contextlib.asynccontextmanager
            async def proxy_request(self, iid, method, path, *, params=None, **_k):
                self.last_params = params
                self.last_path = path
                if raise_exc is not None:
                    raise raise_exc
                if path == "/api/sessions/search":
                    yield _FakeResp(search_status, search_body, confirm)
                elif path == "/api/sessions":
                    yield _FakeResp(list_status, list_body or b"{}", confirm)
                else:  # /api/sessions/{key}
                    resp = _FakeResp(read_status, read_body or b"[]", confirm)
                    content = resp.content
                    mgr = self

                    async def _counting(n, _inner=content.iter_chunked):
                        mgr.body_reads += 1
                        async for chunk in _inner(n):
                            yield chunk

                    content.iter_chunked = _counting  # type: ignore[method-assign]
                    yield resp

        return FakeMgr()

    def _keyed_req(self, state, session_key, *, attested=True, **query):
        """An internal-secret request whose calling session carries *session_key*.

        Attested by default (the peer check vouched for the key), so a refusal in
        a test built on it is the roster's or the walk's, not the attestation's.
        ``attested=False`` is the caller's bare word: a key with nothing behind it.
        """
        req = _FakeReq(state, query=query, user=None, headers={"X-Session-Key": session_key})
        req._attrs["internal_auth"] = True
        if attested:
            req._attrs["peer_verified"] = True
        return req

    def _bare_req(self, state, **query):
        """An internal-secret request with NO ``X-Session-Key`` at all."""
        req = _FakeReq(state, query=query, user=None, headers={})
        req._attrs["internal_auth"] = True
        req._attrs["peer_verified"] = True
        return req

    @staticmethod
    def _tab(**overrides):
        """One live dashboard slot, by default the person's own tab mid-turn.

        Every field the gate reads is gateway memory on the slot and is modelled
        here with the value the owner's tab has while the owner's own message is
        running: no app, a turn in flight, opened and steered by nobody but the
        owner, and created by no caller. A test names the one signal it flips.
        """
        fields = dict(
            # ``key`` is set by :meth:`_slots` from the name the slot is registered
            # under; ``caller_slot_key`` maps the caller's ``dashboard:<key>`` onto
            # it through ``slot_history_key``, which reads these two as well.
            key="",
            linked_session_key="",
            channel_origin=False,
            _app="",
            turn_running=True,
            _turn_channel_origin=False,
            _turn_channel_narrowed=False,
            _turn_user_sent=True,
            _turn_owner_sent=True,
            _created_by="",
            _lineage_minted=False,
        )
        fields.update(overrides)
        return types.SimpleNamespace(**fields)

    @staticmethod
    def _slots(**tabs):
        """A slot registry stub: ``get(slot_id)`` over the given live slots.

        ``caller_names_a_missing_slot`` reaches the registry through ``.get`` and
        also probes ``linked_session_key``; a mapping with neither a hit nor a
        linked row is exactly the popped-tab state. Each value is a slot built by
        :meth:`_tab`; ``_slots(ui=self._tab())`` is the ordinary owner's tab.
        """

        for name, tab in tabs.items():
            if not getattr(tab, "key", ""):
                tab.key = name

        class _Slots:
            def get(self, slot_id):
                return tabs.get(slot_id)

            def items(self):
                return list(tabs.items())

            def values(self):
                return list(tabs.values())

        return _Slots()

    def _owner_state(self, reg, *, slot="ui", mgr=None, **tab):
        """A state whose one live slot is the owner's tab (or the variant *tab* names)."""
        state = _State(reg, mgr or self._mgr_with(read_body=b'[{"role": "user", "content": "hi"}]'))
        state._slots = self._slots(**{slot: self._tab(**tab)})
        return state
