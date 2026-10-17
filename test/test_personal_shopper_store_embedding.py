"""Personal shopper preferences keep their vectors after an idle model unload.

The shared embedding model is unloaded after an idle window. A store that gated
on ``is_ready()`` alone would then write every new preference without a vector
and keep search on keywords until something else reloaded the model.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.builtins.personal_shopper.backend import store


class _Embedder:
    def __init__(self, *, ready: bool, loadable: bool) -> None:
        self.ready = ready
        self.loadable = loadable
        self.calls: list[str] = []

    def is_ready(self) -> bool:
        return self.ready

    def can_load_on_demand(self) -> bool:
        return self.loadable

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.5, 0.25]


@pytest.mark.parametrize(
    ("ready", "loadable", "embeds"),
    [
        (True, False, True),  # loaded now
        (False, True, True),  # unloaded when idle: the embed reloads it
        (False, False, False),  # no model to serve
    ],
)
def test_embed_serves_a_loaded_or_loadable_model(monkeypatch, ready, loadable, embeds) -> None:
    embedder = _Embedder(ready=ready, loadable=loadable)
    monkeypatch.setattr(store, "get_shared_embedder", lambda: embedder)
    blob = store._embed("blue running shoes")
    assert (blob is not None) is embeds
    assert embedder.calls == (["blue running shoes"] if embeds else [])


def test_a_backend_without_on_demand_loading_needs_to_be_ready(monkeypatch) -> None:
    class _Plain:
        def is_ready(self) -> bool:
            return False

        def embed(self, text: str) -> list[float]:
            raise AssertionError("must not embed on an unready backend")

    monkeypatch.setattr(store, "get_shared_embedder", lambda: _Plain())
    assert store._embed("anything") is None
