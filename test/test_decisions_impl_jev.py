"""``impl_jev`` against a real aiohttp server: 200, 429, 5xx, timeout, bad JSON.

A live loopback server rather than a mocked ``ClientSession``: the thing most
likely to be wrong here is the WIRE, and a mock asserts only that the code calls
the mock. The server below records the request body, so the request-shape
assertions are made against what actually went over a socket.

Every field name asserted here is quoted from ``https://docs.typesafe.ai/api``.
These tests are the record of what that page specifies, and
``_to_wire``/``_from_wire`` are the only two functions to edit when it changes.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kiro_crew.config.sections import DecisionProviderConfig
from kiro_crew.decisions import impl_jev
from kiro_crew.decisions.impl_jev import (
    JevHttpError,
    JevOracle,
    JevProtocolError,
    _to_wire,
    close_sessions,
    resolve_api_key,
)
from kiro_crew.decisions.types import Choice, Noul, Score

VAULT_REF = "secret://TYPESAFE_API_KEY"
VAULT_KEY = "test-key"


@pytest.fixture(autouse=True)
def fake_vault(monkeypatch):
    """A dashboard vault holding ``TYPESAFE_API_KEY``; returns a setter for its value.

    The vault is the ONLY key source ``resolve_api_key`` honours, so every request
    test goes through it. Tests that need a different vault shape patch over this.
    """
    import kiro_crew.secrets.vault as vault_mod

    store = {"TYPESAFE_API_KEY": VAULT_KEY}

    class _Value:
        def __init__(self, value):
            self._value = value

        def reveal(self):
            return self._value

    class _Vault:
        def __init__(self, *_a, **_k):
            pass

        def get(self, name):
            return _Value(store[name]) if name in store else None

    monkeypatch.setattr(vault_mod, "SecretVault", _Vault)
    return lambda value: store.__setitem__("TYPESAFE_API_KEY", value)


CHOICE = Choice(id="department", prompt="Which team?", options=["billing", "technical", "sales"])
URGENT = Choice(id="is_urgent", prompt="Does this convey urgency?", options=["yes", "no"])


def _yes(p=0.9):
    """A well-formed Choice answer for ``URGENT``."""
    return {"type": "choice", "choice": "yes", "probabilities": {"yes": p, "no": 1 - p}}


class _Recorder:
    """A tiny aiohttp app that answers with a canned response and records the request."""

    def __init__(self, *, status=200, body=None, raw=None, delay=0.0):
        self.status = status
        self.body = body
        self.raw = raw
        self.delay = delay
        self.requests: list[dict] = []
        self.headers: list[dict] = []

    async def handle(self, request: web.Request) -> web.Response:
        self.headers.append(dict(request.headers))
        try:
            self.requests.append(await request.json())
        except Exception:
            self.requests.append({})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raw is not None:
            return web.Response(status=self.status, text=self.raw, content_type="application/json")
        return web.json_response(self.body or {}, status=self.status)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/systemone", self.handle)
        return app


async def _run(
    recorder: _Recorder,
    questions,
    *,
    timeout_ms=5000,
    api_key=VAULT_REF,
    state="hi",
    model="jev-latest",
    host="localhost",
):
    """Serve *recorder* on loopback and ask *questions* through a real socket.

    *host* defaults to the NAME ``localhost``, which reaches the same socket but is
    not a literal loopback address, so the client treats it as a remote provider and
    sends the key. Pass ``127.0.0.1`` to exercise the local-model path, which sends
    none.
    """
    server = TestServer(recorder.app(), host="127.0.0.1")
    await server.start_server()
    try:
        provider = DecisionProviderConfig(
            endpoint=f"http://{host}:{server.port}/v1/systemone",
            api_key=api_key,
            model=model,
            timeout_ms=timeout_ms,
        )
        return await JevOracle(provider).ask(state, questions)
    finally:
        # The shared keep-alive session belongs to this loop, which ends with the call.
        await close_sessions()
        await server.close()


def _ok_body(answers, *, input_tokens=312):
    return {
        "model": "jev-latest",
        "answers": answers,
        "usage": {"input_tokens": input_tokens, "output_tokens": 48},
    }


# ---------------------------------------------------------------------------
# The request that goes over the wire
# ---------------------------------------------------------------------------


class TestRequestShape:
    def test_the_bearer_header_carries_the_vault_key(self, fake_vault):
        fake_vault("sk-live-abc")
        rec = _Recorder(body=_ok_body({"is_urgent": _yes()}))
        asyncio.run(_run(rec, [URGENT]))
        assert rec.headers[0]["Authorization"] == "Bearer sk-live-abc"

    def test_a_literal_key_in_config_never_reaches_the_network(self):
        """``provider.api_key`` is agent-writable: a literal there could be any
        secret the agent read, so it is not a credential and nothing is sent."""
        rec = _Recorder(body=_ok_body({"is_urgent": _yes()}))
        with pytest.raises(JevProtocolError, match="no api key"):
            asyncio.run(_run(rec, [URGENT], api_key="sk-live-abc"))
        assert rec.requests == []

    @pytest.mark.parametrize(
        "model",
        ["", " ", "-leading-dash", "jev latest", "a" * 65, "AKIA" + "A" * 16 + "\n", "x/y"],
        ids=["empty", "space", "leading-dash", "inner-space", "too-long", "newline", "slash"],
    )
    def test_a_model_that_is_not_an_id_never_reaches_the_network(self, model):
        """``provider.model`` goes on the wire verbatim from agent-writable config,
        so its shape is bounded here as well as by the gate's scrub."""
        rec = _Recorder(body=_ok_body({"is_urgent": _yes()}))
        with pytest.raises(JevProtocolError, match="model id"):
            _to_wire("hi", model, [URGENT])
        if model:
            # An empty model falls back to the default before reaching the wire.
            with pytest.raises(JevProtocolError, match="model id"):
                asyncio.run(_run(rec, [URGENT], model=model))
        assert rec.requests == []

    @pytest.mark.parametrize("model", ["jev-latest", "jev_2.1", "JEV", "0"])
    def test_a_model_id_is_sent_as_given(self, model):
        rec = _Recorder(body=_ok_body({"is_urgent": _yes()}))
        asyncio.run(_run(rec, [URGENT], model=model))
        assert rec.requests[0]["model"] == model

    def test_state_and_model_are_top_level(self):
        rec = _Recorder(body=_ok_body({"is_urgent": _yes()}))
        asyncio.run(_run(rec, [URGENT], state={"messages": ["hello"]}))
        sent = rec.requests[0]
        assert sent["model"] == "jev-latest"
        assert sent["state"] == {"messages": ["hello"]}

    def test_choice_criteria_is_a_map_of_option_to_null(self):
        """Per the API a Choice's ``criteria`` is a MAP; a list would be rejected."""
        rec = _Recorder(
            body=_ok_body(
                {
                    "department": {
                        "type": "choice",
                        "choice": "sales",
                        "probabilities": {"sales": 1.0},
                        "confidence": 0.9,
                    }
                }
            )
        )
        asyncio.run(_run(rec, [CHOICE]))
        q = rec.requests[0]["questions"]["department"]
        assert q["type"] == "choice"
        assert q["instructions"] == CHOICE.prompt
        assert q["criteria"] == {"billing": None, "technical": None, "sales": None}

    def test_an_object_of_no_question_type_is_refused(self):
        """The wire speaks the three question types; any other object is a caller bug."""
        from kiro_crew.decisions.impl_jev import _to_wire

        with pytest.raises(JevProtocolError, match="unsupported question type"):
            _to_wire("hi", "jev-latest", [object()])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# 200: parsing each answer type back into the question's own units
# ---------------------------------------------------------------------------


class TestSuccessfulParse:
    def test_choice_answer(self):
        rec = _Recorder(
            body=_ok_body(
                {
                    "department": {
                        "type": "choice",
                        "choice": "technical",
                        "probabilities": {"billing": 0.08, "technical": 0.85, "sales": 0.07},
                        "confidence": 0.82,
                    }
                }
            )
        )
        answer = asyncio.run(_run(rec, [CHOICE]))["department"]
        assert answer.value == "technical"
        assert answer.p == pytest.approx(0.85), "p is the mass behind the chosen option"
        assert answer.confidence == pytest.approx(0.82)

    def test_confidence_is_optional_and_absent_means_none(self):
        """``confidence`` is provider metadata; missing must not read as 0.0."""
        rec = _Recorder(body=_ok_body({"is_urgent": _yes(0.92)}))
        answer = asyncio.run(_run(rec, [URGENT]))["is_urgent"]
        assert answer.value == "yes"
        assert answer.p == pytest.approx(0.92)
        assert answer.confidence is None

    def test_a_usage_block_is_read_past_rather_than_parsed(self):
        """``ask`` answers with the answers alone -- no spend column to fill.

        The wire still carries ``usage``; a body that omits it must parse exactly
        the same, which is what says the field is not on the read path at all.
        """
        with_usage = _Recorder(body=_ok_body({"is_urgent": _yes(0.1)}, input_tokens=1900))
        without = _Recorder(body={"model": "jev-latest", "answers": {"is_urgent": _yes(0.1)}})
        first = asyncio.run(_run(with_usage, [URGENT]))
        second = asyncio.run(_run(without, [URGENT]))
        assert set(first) == set(second) == {"is_urgent"}
        assert first["is_urgent"].p == second["is_urgent"].p == pytest.approx(0.1)

    def test_a_2xx_other_than_200_is_accepted(self):
        rec = _Recorder(status=202, body=_ok_body({"is_urgent": _yes(0.3)}))
        assert asyncio.run(_run(rec, [URGENT]))["is_urgent"].p == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Failures: each raises, so the gate can convert it into None + a logged reason
# ---------------------------------------------------------------------------


class TestLocalModelServer:
    """A literal loopback endpoint is a local model server: it gets no credential."""

    def test_no_authorization_header_is_sent(self, fake_vault):
        fake_vault("sk-live-abc")
        rec = _Recorder(body=_ok_body({"is_urgent": _yes()}))
        answers = asyncio.run(_run(rec, [URGENT], host="127.0.0.1"))
        assert answers["is_urgent"].value == "yes"
        assert "Authorization" not in rec.headers[0]

    def test_the_vault_is_never_read(self, monkeypatch):
        """Not merely left out of the header: the key is never fetched at all."""
        import kiro_crew.decisions.impl_jev as mod

        def _boom(_raw):
            raise AssertionError("the vault was read for a local server")

        monkeypatch.setattr(mod, "resolve_api_key", _boom)
        rec = _Recorder(body=_ok_body({"is_urgent": _yes()}))
        asyncio.run(_run(rec, [URGENT], host="127.0.0.1"))
        assert rec.requests, "the request still went out"

    def test_no_key_is_not_a_refusal_locally(self):
        rec = _Recorder(body=_ok_body({"is_urgent": _yes()}))
        asyncio.run(_run(rec, [URGENT], host="127.0.0.1", api_key=""))
        assert rec.requests

    def test_the_name_localhost_still_needs_the_key(self):
        """A name can resolve anywhere, so it is treated as a remote provider."""
        rec = _Recorder(body=_ok_body({"is_urgent": _yes()}))
        with pytest.raises(JevProtocolError, match="no api key"):
            asyncio.run(_run(rec, [URGENT], host="localhost", api_key=""))
        assert rec.requests == []

    def test_a_hand_written_loopback_address_says_once_that_no_key_is_sent(
        self, monkeypatch, caplog
    ):
        """A tunnel to hosted Jev would get 401s; the log names the withheld key, once."""
        import kiro_crew.decisions.impl_jev as mod

        monkeypatch.setattr(mod, "_keyless_loopback_warned", set())
        mod._warn_keyless_custom_loopback("http://127.0.0.1:9001/v1/systemone", "jev-latest")
        mod._warn_keyless_custom_loopback("http://127.0.0.1:9001/v1/systemone", "jev-latest")
        hits = [r for r in caplog.records if "no Jev API key is sent" in r.getMessage()]
        assert len(hits) == 1

    def test_query_variants_of_one_address_warn_once_and_the_set_stays_bounded(
        self, monkeypatch, caplog
    ):
        import kiro_crew.decisions.impl_jev as mod

        monkeypatch.setattr(mod, "_keyless_loopback_warned", set())
        for n in range(200):
            mod._warn_keyless_custom_loopback(
                f"http://127.0.0.1:9001/v1/systemone?n={n}", "jev-latest"
            )
        hits = [r for r in caplog.records if "no Jev API key is sent" in r.getMessage()]
        assert len(hits) == 1
        for port in range(9002, 9200):
            mod._warn_keyless_custom_loopback(f"http://127.0.0.1:{port}/v1/systemone", "jev-latest")
        assert len(mod._keyless_loopback_warned) <= mod._KEYLESS_WARNED_MAX

    def test_a_local_preset_is_not_warned_about(self, monkeypatch, caplog):
        import kiro_crew.decisions.impl_jev as mod
        from kiro_crew.decisions import local_models

        monkeypatch.setattr(mod, "_keyless_loopback_warned", set())
        preset = local_models.LOCAL_MODELS[0]
        mod._warn_keyless_custom_loopback(
            local_models.endpoint_for(preset.default_port), preset.model
        )
        assert not [r for r in caplog.records if "no Jev API key is sent" in r.getMessage()]


class TestFailures:
    @pytest.mark.parametrize("status", [429, 500, 502, 529, 401, 422])
    def test_a_non_2xx_raises_with_its_status(self, status):
        rec = _Recorder(status=status, body={"error": "nope"})
        with pytest.raises(JevHttpError) as caught:
            asyncio.run(_run(rec, [URGENT]))
        assert str(status) in str(caught.value)

    def test_the_error_body_never_reaches_the_message(self):
        """A provider's error text can echo the request; only the status is kept."""
        rec = _Recorder(status=422, raw='{"detail":"questions.is_urgent.criteria invalid"}')
        with pytest.raises(JevHttpError) as caught:
            asyncio.run(_run(rec, [URGENT]))
        assert "criteria invalid" not in str(caught.value)

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_a_redirect_is_refused_and_never_followed(self, status):
        """Consent is bound to the endpoint asked; a 3xx must not replay the body
        (conversation text, skill descriptions) to whatever Location the server names."""
        followed: list[str] = []

        async def _sink(request: web.Request) -> web.Response:
            followed.append(request.path)
            return web.json_response(_ok_body({"is_urgent": _yes()}))

        class _Redirecting(_Recorder):
            def app(self) -> web.Application:
                app = super().app()
                app.router.add_post("/elsewhere", _sink)
                return app

            async def handle(self, request: web.Request) -> web.Response:
                self.requests.append(await request.json())
                return web.Response(status=status, headers={"Location": "/elsewhere"})

        rec = _Redirecting()
        with pytest.raises(JevHttpError) as caught:
            asyncio.run(_run(rec, [URGENT]))
        assert str(status) in str(caught.value)
        assert followed == [], "the redirect target must never receive the body"
        assert len(rec.requests) == 1

    def test_a_body_that_is_not_json_raises(self):
        rec = _Recorder(raw="<html>502 Bad Gateway</html>")
        with pytest.raises(JevProtocolError, match="not JSON"):
            asyncio.run(_run(rec, [URGENT]))

    def test_a_body_with_no_answers_object_raises(self):
        rec = _Recorder(body={"model": "jev-latest", "usage": {"input_tokens": 1}})
        with pytest.raises(JevProtocolError, match="no 'answers' object"):
            asyncio.run(_run(rec, [URGENT]))

    def test_a_json_array_body_raises(self):
        rec = _Recorder(raw="[1, 2, 3]")
        with pytest.raises(JevProtocolError, match="not an object"):
            asyncio.run(_run(rec, [URGENT]))

    def test_an_unanswered_question_raises_rather_than_returning_a_partial(self):
        """All-or-nothing: a point file has no way to ask which half it got."""
        rec = _Recorder(body=_ok_body({"is_urgent": _yes(0.5)}))
        with pytest.raises(JevProtocolError, match="no answer for question"):
            asyncio.run(_run(rec, [URGENT, CHOICE]))

    def test_a_choice_answer_without_a_choice_string_raises(self):
        rec = _Recorder(body=_ok_body({"department": {"type": "choice", "probabilities": {}}}))
        with pytest.raises(JevProtocolError, match="no 'choice' string"):
            asyncio.run(_run(rec, [CHOICE]))

    @pytest.mark.parametrize(
        "probabilities",
        [
            {"yes": True, "no": 0.1},  # isinstance(True, int) is True; a bool is not a score
            {"yes": "0.9", "no": 0.1},
            {"yes": 1.4, "no": 0.1},
            {"yes": float("nan"), "no": 0.1},
            {"no": 1.0},  # the chosen option carries no mass at all
            None,
        ],
        ids=["bool", "string", "over-one", "nan", "missing-choice", "absent"],
    )
    def test_a_choice_without_a_valid_chosen_probability_raises(self, probabilities):
        raw = {"type": "choice", "choice": "yes"}
        if probabilities is not None:
            raw["probabilities"] = probabilities
        # Sent as text: Python's encoder emits a bare NaN token, which the reader parses.
        rec = _Recorder(raw=json.dumps(_ok_body({"is_urgent": raw})))
        with pytest.raises(JevProtocolError, match="chosen probability"):
            asyncio.run(_run(rec, [URGENT]))

    def test_an_answer_whose_type_disagrees_with_the_question_raises(self):
        rec = _Recorder(body=_ok_body({"is_urgent": {"type": "noul", "noul": 0.9}}))
        with pytest.raises(JevProtocolError, match="type does not match"):
            asyncio.run(_run(rec, [URGENT]))

    def test_a_slow_server_times_out(self):
        rec = _Recorder(body=_ok_body({"is_urgent": _yes(0.1)}), delay=2.0)
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            asyncio.run(_run(rec, [URGENT], timeout_ms=100))

    def test_no_api_key_raises_before_any_request(self):
        """An empty bearer would come back 401 and be indistinguishable from a bad key."""
        rec = _Recorder(body=_ok_body({"is_urgent": _yes(0.1)}))
        with pytest.raises(JevProtocolError, match="no api key"):
            asyncio.run(_run(rec, [URGENT], api_key=""))
        assert rec.requests == [], "nothing may reach the network without a key"

    def test_no_questions_raises(self):
        rec = _Recorder(body=_ok_body({}))
        with pytest.raises(JevProtocolError, match="no questions"):
            asyncio.run(_run(rec, []))


# ---------------------------------------------------------------------------
# api_key resolution
# ---------------------------------------------------------------------------


class TestResponseBound:
    """A provider cannot make the reader allocate an unbounded body."""

    def test_an_oversized_response_is_refused_not_allocated(self):
        from kiro_crew.decisions.impl_jev import _MAX_RESPONSE_BYTES

        # One byte past the cap, and valid JSON so nothing else can be blamed:
        # the padding sits inside a real field, so a reader without the cap would
        # parse this happily and only the allocation would be unbounded.
        padding = "x" * (_MAX_RESPONSE_BYTES + 1)
        recorder = _Recorder(raw='{"model": "jev-latest", "answers": {}, "pad": "' + padding + '"}')

        with pytest.raises(JevProtocolError) as caught:
            asyncio.run(_run(recorder, [Choice(id="verdict", prompt="?", options=["A", "B"])]))

        assert "exceeded" in str(caught.value)

    def test_a_normal_response_is_unaffected_by_the_bound(self):
        """The cap must be generous next to a real answer set, not near it."""
        recorder = _Recorder(
            body=_ok_body(
                {"verdict": {"type": "choice", "choice": "A", "probabilities": {"A": 0.9}}}
            )
        )
        result = asyncio.run(_run(recorder, [Choice(id="verdict", prompt="?", options=["A", "B"])]))
        assert result["verdict"].value == "A"


class TestResolveApiKey:
    @pytest.mark.parametrize("raw", ["sk-plain-value", "  sk-plain  ", "Bearer x"])
    def test_a_literal_value_is_not_a_key(self, raw, caplog):
        """The field is agent-writable: any literal could be a secret the agent read
        and wants sent as a bearer credential. Refused with a warning that names the
        fix and never the value."""
        with caplog.at_level("WARNING", logger="kiro_crew.decisions.impl_jev"):
            assert resolve_api_key(raw) == ""
        assert "literal value, which is not used" in caplog.text
        assert raw.strip() not in caplog.text

    def test_whitespace_around_the_reference_is_stripped(self):
        assert resolve_api_key("  secret://TYPESAFE_API_KEY  ") == VAULT_KEY

    @pytest.mark.parametrize("raw", ["", "   ", "secret://", "secret://   "])
    def test_nothing_usable_resolves_to_empty(self, raw):
        assert resolve_api_key(raw) == ""

    def test_a_secret_reference_reads_the_vault(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        import kiro_crew.secrets.vault as vault_mod

        class _Value:
            def reveal(self):
                return "sk-from-vault"

        class _Vault:
            def __init__(self, *_a, **_k):
                pass

            def get(self, name):
                assert name == "TYPESAFE_API_KEY"
                return _Value()

        monkeypatch.setattr(vault_mod, "SecretVault", _Vault)
        assert resolve_api_key("secret://TYPESAFE_API_KEY") == "sk-from-vault"

    def test_a_literal_never_reaches_the_vault(self, monkeypatch):
        """Observed by whether the vault is CONSTRUCTED: ``resolve_api_key`` catches
        ``Exception`` around the vault call, so a raising double could not tell a
        guarded path from an unguarded one."""
        import kiro_crew.secrets.vault as vault_mod

        constructed: list[int] = []

        class _Vault:
            def __init__(self, *_a, **_k):
                constructed.append(1)

            def get(self, name):  # pragma: no cover - reached only on regression
                return None

        monkeypatch.setattr(vault_mod, "SecretVault", _Vault)
        assert resolve_api_key("literal-operator-key") == ""
        assert constructed == []

    def test_the_destination_is_not_this_functions_concern(self):
        """WHERE the vault key goes is bound by consent in the gate (the owner
        reviewed and recorded the endpoint), so the reference resolves the same for
        a custom endpoint; ``test_decisions_gate.py`` pins that an unconsented
        endpoint never reaches ``ask`` at all."""
        assert resolve_api_key(VAULT_REF) == VAULT_KEY

    def test_a_missing_vault_entry_resolves_to_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        import kiro_crew.secrets.vault as vault_mod

        class _Vault:
            def __init__(self, *_a, **_k):
                pass

            def get(self, name):
                return None

        monkeypatch.setattr(vault_mod, "SecretVault", _Vault)
        assert resolve_api_key("secret://TYPESAFE_API_KEY") == ""

    @pytest.mark.parametrize(
        "name", ["SLACK_SIGNING_SECRET", "typesafe_api_key", "TYPESAFE_API_KEY2", " ANY"]
    )
    def test_a_reference_to_any_other_vault_entry_never_reaches_the_vault(self, monkeypatch, name):
        """`provider.api_key` is in agent-writable config: a free name would let an
        agent choose which operator secret is sent as the bearer credential."""
        import kiro_crew.secrets.vault as vault_mod

        constructed: list[int] = []

        class _Vault:
            def __init__(self, *_a, **_k):
                constructed.append(1)

            def get(self, name):  # pragma: no cover - reached only on regression
                return None

        monkeypatch.setattr(vault_mod, "SecretVault", _Vault)
        assert resolve_api_key(f"secret://{name}") == ""
        assert (
            constructed == []
        ), "only TYPESAFE_API_KEY may be read; nothing else touches the vault"

    def test_a_vault_error_resolves_to_empty_rather_than_raising(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        import kiro_crew.secrets.vault as vault_mod

        class _Vault:
            def __init__(self, *_a, **_k):
                raise OSError("vault key unreadable")

        monkeypatch.setattr(vault_mod, "SecretVault", _Vault)
        assert resolve_api_key("secret://ANY") == ""


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestProviderDefaults:
    def test_an_empty_provider_falls_back_to_the_documented_endpoint_and_model(self):
        class _Bare:
            endpoint = ""
            model = ""
            api_key = "k"
            timeout_ms = 1000

        oracle = JevOracle(_Bare())
        assert oracle._endpoint == "https://api.typesafe.ai/v1/systemone"
        assert oracle._model == "jev-latest"

    def test_the_shipped_config_default_matches_the_documented_endpoint(self):
        """A drift guard: the config default and this module must not diverge."""
        assert DecisionProviderConfig.endpoint == "https://api.typesafe.ai/v1/systemone"
        assert DecisionProviderConfig.model == "jev-latest"

    def test_request_body_is_json_serialisable_as_sent(self):
        """Pins that nothing in ``_to_wire`` needs a custom encoder."""
        from kiro_crew.decisions.impl_jev import _to_wire

        json.dumps(_to_wire("hi", "jev-latest", [CHOICE, URGENT]))


# ---------------------------------------------------------------------------
# Noul and Score: the provider's other two question types
# ---------------------------------------------------------------------------

IS_URGENT = Noul(
    id="is_urgent",
    prompt="Does this convey urgency?",
    true_means="Explicitly time-sensitive",
    false_means="No urgency expressed",
)
FRUSTRATION = Score(
    id="frustration",
    prompt="How frustrated is the customer?",
    levels=["Calm", "Frustrated", "Very angry"],
)


def _score_body(score=1.05, probabilities=None, confidence=0.92):
    return {
        "type": "score",
        "score": score,
        "legend": {"0": "Calm", "1": "Frustrated", "2": "Very angry"},
        "probabilities": (
            {"0": 0.0, "1": 0.95, "2": 0.05} if probabilities is None else probabilities
        ),
        "confidence": confidence,
    }


class TestNoulAndScoreRequestShape:
    def test_noul_and_score_go_out_in_the_documented_shape(self):
        rec = _Recorder(
            body=_ok_body(
                {"is_urgent": {"type": "noul", "noul": 0.95}, "frustration": _score_body()}
            )
        )
        asyncio.run(_run(rec, [IS_URGENT, FRUSTRATION]))
        questions = rec.requests[0]["questions"]
        assert questions["is_urgent"] == {
            "type": "noul",
            "instructions": IS_URGENT.prompt,
            "criteria": {"true": "Explicitly time-sensitive", "false": "No urgency expressed"},
        }
        assert questions["frustration"] == {
            "type": "score",
            "instructions": FRUSTRATION.prompt,
            "criteria": ["Calm", "Frustrated", "Very angry"],
        }

    def test_a_noul_with_no_rubric_sends_no_criteria(self):
        """``criteria`` is optional for a Noul; an empty object is not sent in its place."""
        wire = _to_wire("hi", "jev-latest", [Noul(id="n", prompt="Yes?")])
        assert wire["questions"]["n"] == {"type": "noul", "instructions": "Yes?"}

    def test_a_noul_with_one_side_sends_only_that_side(self):
        wire = _to_wire("hi", "jev-latest", [Noul(id="n", prompt="Yes?", true_means="it is")])
        assert wire["questions"]["n"]["criteria"] == {"true": "it is"}

    @pytest.mark.parametrize("levels", [[], ["only"], [str(i) for i in range(11)]])
    def test_a_score_outside_two_to_ten_levels_is_refused_before_sending(self, levels):
        with pytest.raises(JevProtocolError, match="number of levels"):
            _to_wire("hi", "jev-latest", [Score(id="s", prompt="?", levels=levels)])


class TestNoulAndScoreParse:
    def test_noul_value_is_the_probability_of_yes(self):
        rec = _Recorder(body=_ok_body({"is_urgent": {"type": "noul", "noul": 0.2}}))
        answer = asyncio.run(_run(rec, [IS_URGENT]))["is_urgent"]
        assert answer.value == pytest.approx(0.2)
        assert answer.p == pytest.approx(0.8), "p is the more likely side's probability"
        assert answer.confidence is None

    def test_score_value_is_the_weighted_level_and_p_the_likeliest_level(self):
        rec = _Recorder(body=_ok_body({"frustration": _score_body()}))
        answer = asyncio.run(_run(rec, [FRUSTRATION]))["frustration"]
        assert answer.value == pytest.approx(1.05)
        assert answer.p == pytest.approx(0.95)
        assert answer.confidence == pytest.approx(0.92)

    @pytest.mark.parametrize(
        "raw",
        [
            {"type": "choice", "choice": "yes", "probabilities": {"yes": 1.0}},
            {"type": "noul"},
            {"type": "noul", "noul": 1.5},
            {"type": "noul", "noul": -0.1},
            {"type": "noul", "noul": True},
            {"type": "noul", "noul": "0.9"},
            {"type": "noul", "noul": float("nan")},
        ],
        ids=["wrong-type", "missing", "above-1", "below-0", "bool", "string", "nan"],
    )
    def test_a_malformed_noul_answer_raises(self, raw):
        rec = _Recorder(raw=json.dumps(_ok_body({"is_urgent": raw}), allow_nan=True))
        with pytest.raises(JevProtocolError):
            asyncio.run(_run(rec, [IS_URGENT]))

    @pytest.mark.parametrize(
        "raw",
        [
            {"type": "noul", "noul": 0.5},
            _score_body(score=2.5),
            _score_body(score=-0.1),
            _score_body(score=True),
            _score_body(probabilities={}),
            _score_body(probabilities={"3": 1.0}),
            _score_body(probabilities={"0": 1.2}),
            {k: v for k, v in _score_body().items() if k != "probabilities"},
        ],
        ids=[
            "wrong-type",
            "above-top-level",
            "below-0",
            "bool",
            "empty-distribution",
            "unknown-level",
            "probability-above-1",
            "no-probabilities",
        ],
    )
    def test_a_malformed_score_answer_raises(self, raw):
        rec = _Recorder(body=_ok_body({"frustration": raw}))
        with pytest.raises(JevProtocolError):
            asyncio.run(_run(rec, [FRUSTRATION]))

    def test_a_partial_response_across_types_raises(self):
        """One answer missing from a mixed request is a failure, never a partial result."""
        rec = _Recorder(body=_ok_body({"is_urgent": {"type": "noul", "noul": 0.9}}))
        with pytest.raises(JevProtocolError, match="no answer for question"):
            asyncio.run(_run(rec, [IS_URGENT, FRUSTRATION, CHOICE]))


# ---------------------------------------------------------------------------
# Cloudflare Workers AI envelope (Clef)
# ---------------------------------------------------------------------------


def _cf_envelope(answers, *, success=True, model="clef-flash"):
    """The body Cloudflare's REST endpoint returns around a System One response."""
    return {
        "result": {"model": model, "answers": answers, "usage": {"input_tokens": 312}},
        "success": success,
        "errors": [],
        "messages": [],
    }


class TestCloudflareEnvelope:
    def test_a_wrapped_success_parses_like_the_unwrapped_shape(self):
        rec = _Recorder(body=_cf_envelope({"is_urgent": _yes(0.92)}))
        answer = asyncio.run(_run(rec, [URGENT], model="clef-flash"))["is_urgent"]
        assert answer.value == "yes"
        assert answer.p == pytest.approx(0.92)
        assert rec.requests[0]["model"] == "clef-flash", "the model id goes out unchanged"

    def test_a_wrapped_mixed_request_parses_every_type(self):
        answers = {
            "department": {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.8, "technical": 0.1, "sales": 0.1},
            },
            "frustration": _score_body(),
            "is_urgent": {"type": "noul", "noul": 0.25},
        }
        rec = _Recorder(body=_cf_envelope(answers))
        got = asyncio.run(_run(rec, [CHOICE, FRUSTRATION, IS_URGENT], model="clef"))
        assert got["department"].value == "billing"
        assert got["frustration"].value == pytest.approx(1.05)
        assert got["is_urgent"].value == pytest.approx(0.25)
        assert got["is_urgent"].p == pytest.approx(0.75)

    def test_a_failed_envelope_never_echoes_provider_text(self):
        body = {
            "result": None,
            "success": False,
            "errors": [{"code": 10000, "message": "secret-state-echo"}],
            "messages": [],
        }
        rec = _Recorder(body=body)
        with pytest.raises(JevProtocolError) as caught:
            asyncio.run(_run(rec, [URGENT], model="clef-flash"))
        assert "secret-state-echo" not in str(caught.value)

    @pytest.mark.parametrize(
        "body",
        [
            {"result": {"model": "clef-flash"}, "success": True},
            {"result": {"answers": []}, "success": True},
            {"result": {"answers": None}, "success": True},
            {"result": [], "success": True},
            {"result": "x", "success": True},
            {"success": True},
        ],
        ids=[
            "no-answers",
            "answers-list",
            "answers-null",
            "result-list",
            "result-str",
            "no-result",
        ],
    )
    def test_an_envelope_without_an_answers_object_is_refused(self, body):
        rec = _Recorder(body=body)
        with pytest.raises(JevProtocolError, match="no 'answers' object"):
            asyncio.run(_run(rec, [URGENT], model="clef-flash"))

    def test_the_unwrapped_jev_shape_is_unchanged(self):
        rec = _Recorder(body=_ok_body({"is_urgent": _yes(0.4)}))
        assert asyncio.run(_run(rec, [URGENT]))["is_urgent"].p == pytest.approx(0.4)

    def test_a_top_level_answers_object_wins_over_a_result_sibling(self):
        """Jev's own shape is read first, so a stray ``result`` key cannot redirect it."""
        body = _ok_body({"is_urgent": _yes(0.4)})
        body["result"] = {"answers": {"is_urgent": _yes(0.99)}}
        body["success"] = False
        rec = _Recorder(body=body)
        assert asyncio.run(_run(rec, [URGENT]))["is_urgent"].p == pytest.approx(0.4)

    def test_answers_are_still_validated_strictly_inside_the_envelope(self):
        rec = _Recorder(body=_cf_envelope({"is_urgent": {"type": "noul", "noul": 0.9}}))
        with pytest.raises(JevProtocolError, match="type does not match"):
            asyncio.run(_run(rec, [URGENT], model="clef-flash"))

    def test_an_unanswered_question_inside_the_envelope_is_still_a_failure(self):
        rec = _Recorder(body=_cf_envelope({"is_urgent": _yes(0.5)}))
        with pytest.raises(JevProtocolError, match="no answer for question"):
            asyncio.run(_run(rec, [URGENT, CHOICE], model="clef-flash"))

    @pytest.mark.parametrize("model", ["clef", "clef-flash"])
    def test_the_clef_model_ids_are_valid_wire_model_ids(self, model):
        from kiro_crew.decisions.types import is_model_id

        assert is_model_id(model)
        assert _to_wire("hi", model, [URGENT])["model"] == model


# ---------------------------------------------------------------------------
# Connection reuse: one keep-alive session per loop, shared by every oracle
# ---------------------------------------------------------------------------


class _PeerRecorder(_Recorder):
    """A :class:`_Recorder` that also notes which client socket sent each request."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.peers: list[tuple] = []
        self.cookies: list[str | None] = []
        self.set_cookie = False

    async def handle(self, request: web.Request) -> web.Response:
        self.peers.append(request.transport.get_extra_info("peername"))
        self.cookies.append(request.headers.get("Cookie"))
        response = await super().handle(request)
        if self.set_cookie:
            response.set_cookie("session", "abc")
        return response


class _Serving:
    """Serve *recorder* on loopback for several asks made inside ONE running loop."""

    def __init__(self, recorder: _Recorder):
        self.recorder = recorder
        self.server = TestServer(recorder.app(), host="127.0.0.1")

    async def __aenter__(self):
        await self.server.start_server()
        return self

    async def __aexit__(self, *exc):
        await close_sessions()
        await self.server.close()

    def oracle(self, *, timeout_ms=5000, model="jev-latest", api_key=VAULT_REF):
        provider = DecisionProviderConfig(
            endpoint=f"http://localhost:{self.server.port}/v1/systemone",
            api_key=api_key,
            model=model,
            timeout_ms=timeout_ms,
        )
        return JevOracle(provider)


def _ok(p=0.9):
    return _Recorder(body=_ok_body({"is_urgent": _yes(p)}))


class TestConnectionReuse:
    def test_two_asks_share_one_session_connector_and_socket(self):
        async def scenario():
            rec = _PeerRecorder(body=_ok_body({"is_urgent": _yes()}))
            async with _Serving(rec) as serving:
                await serving.oracle().ask("hi", [URGENT])
                first = impl_jev._session_for_running_loop()
                connector = first.connector
                await serving.oracle().ask("hi", [URGENT])
                second = impl_jev._session_for_running_loop()
                same_connector = second.connector is connector
                return rec, first, second, same_connector

        rec, first, second, same_connector = asyncio.run(scenario())
        assert first is second, "a new oracle per decision must not mean a new session"
        assert same_connector
        assert len(rec.peers) == 2
        assert rec.peers[0] == rec.peers[1], "both requests must ride one TCP connection"

    def test_a_closed_session_is_recreated_and_works(self):
        async def scenario():
            rec = _ok()
            async with _Serving(rec) as serving:
                await serving.oracle().ask("hi", [URGENT])
                first = impl_jev._session_for_running_loop()
                await first.close()
                answers = await serving.oracle().ask("hi", [URGENT])
                second = impl_jev._session_for_running_loop()
                return first, second, second.closed, answers

        first, second, second_closed, answers = asyncio.run(scenario())
        assert second is not first
        assert not second_closed
        assert answers["is_urgent"].p == pytest.approx(0.9)

    def test_a_new_event_loop_gets_its_own_session_and_the_dead_one_is_dropped(self):
        async def grab():
            rec = _ok()
            async with _Serving(rec) as serving:
                await serving.oracle().ask("hi", [URGENT])
                session = impl_jev._session_for_running_loop()
            # ``_Serving`` closed it; keep an open one on this loop to be left behind.
            left = impl_jev._session_for_running_loop()
            return session, left

        _, left = asyncio.run(grab())
        rec = _ok()

        async def later():
            async with _Serving(rec) as serving:
                await serving.oracle().ask("hi", [URGENT])
                return impl_jev._session_for_running_loop(), len(impl_jev._sessions)

        fresh, held = asyncio.run(later())
        assert fresh is not left
        assert held == 1, "the session of the closed loop is dropped, not kept forever"

    def test_close_sessions_closes_and_forgets_them(self):
        async def scenario():
            rec = _ok()
            async with _Serving(rec) as serving:
                await serving.oracle().ask("hi", [URGENT])
                session = impl_jev._session_for_running_loop()
                await close_sessions()
                await close_sessions()  # idempotent, nothing open
                return session, dict(impl_jev._sessions)

        session, held = asyncio.run(scenario())
        assert session.closed
        assert held == {}

    def test_the_timeout_is_per_request_and_does_not_poison_the_session(self):
        async def scenario():
            rec = _Recorder(body=_ok_body({"is_urgent": _yes(0.2)}), delay=0.4)
            async with _Serving(rec) as serving:
                with pytest.raises((asyncio.TimeoutError, TimeoutError)):
                    await serving.oracle(timeout_ms=100).ask("hi", [URGENT])
                session = impl_jev._session_for_running_loop()
                # A later call with a larger budget succeeds on the SAME session: the
                # 100 ms of the call before it was not inherited.
                answers = await serving.oracle(timeout_ms=5000).ask("hi", [URGENT])
                # And the short budget is still enforced on the reused session.
                with pytest.raises((asyncio.TimeoutError, TimeoutError)):
                    await serving.oracle(timeout_ms=100).ask("hi", [URGENT])
                after = impl_jev._session_for_running_loop()
                return session, after, after.closed, answers

        before, after, after_closed, answers = asyncio.run(scenario())
        assert before is after and not after_closed
        assert answers["is_urgent"].p == pytest.approx(0.2)

    def test_a_redirect_is_still_refused_on_a_reused_session(self):
        followed: list[str] = []

        async def _sink(request: web.Request) -> web.Response:
            followed.append(request.path)
            return web.json_response(_ok_body({"is_urgent": _yes()}))

        class _Flaky(_Recorder):
            redirect = False

            def app(self) -> web.Application:
                app = super().app()
                app.router.add_post("/elsewhere", _sink)
                return app

            async def handle(self, request: web.Request) -> web.Response:
                if self.redirect:
                    return web.Response(status=307, headers={"Location": "/elsewhere"})
                return await super().handle(request)

        async def scenario():
            rec = _Flaky(body=_ok_body({"is_urgent": _yes()}))
            async with _Serving(rec) as serving:
                await serving.oracle().ask("hi", [URGENT])
                rec.redirect = True
                with pytest.raises(JevHttpError) as caught:
                    await serving.oracle().ask("hi", [URGENT])
                rec.redirect = False
                await serving.oracle().ask("hi", [URGENT])  # still usable afterwards
                return str(caught.value)

        assert "307" in asyncio.run(scenario())
        assert followed == []

    def test_an_oversized_body_is_still_refused_and_the_session_survives(self):
        from kiro_crew.decisions.impl_jev import _MAX_RESPONSE_BYTES

        class _Big(_Recorder):
            big = True

            async def handle(self, request: web.Request) -> web.Response:
                if self.big:
                    return web.Response(
                        body=b" " * (_MAX_RESPONSE_BYTES + 1), content_type="application/json"
                    )
                return await super().handle(request)

        async def scenario():
            rec = _Big(body=_ok_body({"is_urgent": _yes()}))
            async with _Serving(rec) as serving:
                with pytest.raises(JevProtocolError, match="exceeded"):
                    await serving.oracle().ask("hi", [URGENT])
                rec.big = False
                return await serving.oracle().ask("hi", [URGENT])

        assert asyncio.run(scenario())["is_urgent"].value == "yes"

    def test_a_cookie_the_provider_sets_is_never_replayed(self):
        async def scenario():
            rec = _PeerRecorder(body=_ok_body({"is_urgent": _yes()}))
            rec.set_cookie = True
            async with _Serving(rec) as serving:
                await serving.oracle().ask("hi", [URGENT])
                await serving.oracle().ask("hi", [URGENT])
                return rec

        rec = asyncio.run(scenario())
        assert rec.cookies == [None, None]

    def test_each_request_carries_the_key_it_resolved_not_a_stale_one(self, fake_vault):
        async def scenario():
            rec = _Recorder(body=_ok_body({"is_urgent": _yes()}))
            async with _Serving(rec) as serving:
                fake_vault("key-one")
                await serving.oracle().ask("hi", [URGENT])
                fake_vault("key-two")
                await serving.oracle().ask("hi", [URGENT])
                return rec

        rec = asyncio.run(scenario())
        assert [h.get("Authorization") for h in rec.headers] == [
            "Bearer key-one",
            "Bearer key-two",
        ]
