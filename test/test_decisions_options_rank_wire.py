"""The full probability map on ``Answer``, read from a real loopback socket.

``options.rank`` measures the whole distribution Jev returns, not only the chosen
option's share, so ``impl_jev`` keeps that map on the answer. What it keeps is the
contract: the question's declared options, each a finite number in ``0..1``. Anything
else in the map is dropped without failing the answer, because the chosen probability
is validated separately and is the only field the gate consumes.
"""

from __future__ import annotations

import asyncio
import math

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kiro_crew.config.sections import DecisionProviderConfig
from kiro_crew.decisions.impl_jev import JevOracle, JevProtocolError
from kiro_crew.decisions.types import Answer, Choice

MENU = Choice(id="owner_pick", prompt="Which will the owner pick?", options=["a", "b", "/none"])


@pytest.fixture(autouse=True)
def fake_vault(monkeypatch):
    import kiro_crew.secrets.vault as vault_mod

    class _Value:
        def reveal(self):
            return "test-key"

    class _Vault:
        def __init__(self, *_a, **_k):
            pass

        def get(self, name):
            return _Value() if name == "TYPESAFE_API_KEY" else None

    monkeypatch.setattr(vault_mod, "SecretVault", _Vault)


def _ask(answer: dict) -> Answer:
    async def handle(_request: web.Request) -> web.Response:
        return web.json_response({"model": "jev-latest", "answers": {MENU.id: answer}})

    async def run():
        app = web.Application()
        app.router.add_post("/v1/systemone", handle)
        server = TestServer(app)
        await server.start_server()
        try:
            provider = DecisionProviderConfig(
                endpoint=str(server.make_url("/v1/systemone")),
                api_key="secret://TYPESAFE_API_KEY",
                model="jev-latest",
                timeout_ms=5000,
            )
            return (await JevOracle(provider).ask("hi", [MENU]))[MENU.id]
        finally:
            await server.close()

    return asyncio.run(run())


def _choice(probabilities, choice="a"):
    return {"type": "choice", "choice": choice, "probabilities": probabilities}


def test_the_full_map_rides_on_the_answer():
    answer = _ask(_choice({"a": 0.6, "b": 0.3, "/none": 0.1}))
    assert answer.value == "a"
    assert answer.p == pytest.approx(0.6)
    assert answer.probabilities == {"a": 0.6, "b": 0.3, "/none": 0.1}


@pytest.mark.parametrize(
    "extra",
    [
        {"undeclared": 0.2},
        {"b": 1.5},
        {"b": -0.1},
        {"b": "0.3"},
        {"b": True},
        {"b": None},
    ],
)
def test_a_bad_entry_is_dropped_and_the_answer_survives(extra):
    answer = _ask(_choice({"a": 0.7, **extra}))
    assert answer.p == pytest.approx(0.7)
    assert answer.probabilities == {"a": 0.7}


def test_a_non_finite_entry_is_dropped():
    # json.dumps writes NaN/Infinity literals, which the client's parser accepts.
    answer = _ask(_choice({"a": 0.7, "b": math.inf, "/none": math.nan}))
    assert answer.probabilities == {"a": 0.7}


def test_the_chosen_probability_is_still_required():
    """Keeping the rest of the map does not relax the check on the chosen share."""
    with pytest.raises(JevProtocolError):
        _ask(_choice({"b": 0.9}))


def test_a_constructed_answer_carries_no_map_by_default():
    """The LLM lane and every older caller build answers without one."""
    assert Answer(id="x", value="yes", p=0.5).probabilities is None
