"""The display-redaction cache keys an entry on the prefix set the battery judged under.

``_redact_for_display`` memoizes the display battery, keyed on the text, the
exempt-host set and the pass-4 token-parameter prefix set
(``CredentialPolicy.token_param_exempt_url_prefixes``). The prefix set can move
between two reads (a companion loads, a policy tightens, a read fails and
degrades to the empty set), so the key and the battery, including the nested
scan of a PlantUML diagram's source, must share ONE read.

NEUTRAL PLACEHOLDER PREFIX ONLY: a companion's real approval host never appears
in the public repo.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from test_plantuml_link_redaction import _encode

import kiro_crew.dashboard.chat_utils as chat_utils
from kiro_crew.dashboard.display_battery import display_battery
from kiro_crew.security import redaction

PREFIX = "https://approve.example.com/auth"
# An opaque value no earlier credential pass claims, so the battery's output
# turns on the prefix set alone.
VALUE = "Zq7vK2mP9xR4tL8nW3cY6bH1"
LINK = f"Approve the request here: {PREFIX}?token={VALUE} and then reply."
DIAGRAM = (
    "see https://www.plantuml.com/plantuml/svg/"
    + _encode(f"@startuml\nUser -> Approver: {PREFIX}?token={VALUE}\n@enduml")
    + " here"
)
ALLOWED: tuple[str, ...] = (PREFIX,)
NONE: tuple[str, ...] = ()


class _MovingSet:
    """Answers ``first`` on the first read and ``then`` on every later one."""

    def __init__(self, first: tuple[str, ...], then: tuple[str, ...]) -> None:
        self.answers = [first]
        self.then = then

    def __call__(self) -> tuple[str, ...]:
        return self.answers.pop() if self.answers else self.then


@pytest.fixture(autouse=True)
def _cold_cache() -> Iterator[None]:
    chat_utils._clear_display_redaction_cache()
    yield
    chat_utils._clear_display_redaction_cache()


@pytest.mark.parametrize("text", [LINK, DIAGRAM], ids=["link", "diagram"])
@pytest.mark.parametrize(
    ("first", "then"), [(NONE, ALLOWED), (ALLOWED, NONE)], ids=["tightened", "loosened"]
)
def test_a_warm_render_equals_the_battery_under_the_settled_set(
    monkeypatch: pytest.MonkeyPatch, text: str, first: tuple[str, ...], then: tuple[str, ...]
) -> None:
    """The set moves after the first read of a cold render, then settles on either value.

    Each warm render must equal the battery's output under the settled set. Two
    independent reads would file the output computed under one set under the
    other set's key: in the ``tightened`` case a token the settled set redacts is
    then served in plaintext.
    """
    with monkeypatch.context() as patched:
        patched.setattr(redaction, "_token_param_exempt_prefixes", lambda: first)
        under_first = display_battery(text)
        patched.setattr(redaction, "_token_param_exempt_prefixes", lambda: then)
        under_then = display_battery(text)
    assert under_first != under_then, "the text must turn on the prefix set"

    source = _MovingSet(first, then)
    # Both bindings: the display owner's (through the facade mirror) and pass 4's.
    monkeypatch.setattr(redaction, "_token_param_exempt_prefixes", source)
    monkeypatch.setattr(chat_utils, "_token_param_exempt_prefixes", source)
    chat_utils._redact_for_display(text)

    for settled, expected in ((first, under_first), (then, under_then)):
        source.then = settled
        assert chat_utils._redact_for_display(text) == expected
