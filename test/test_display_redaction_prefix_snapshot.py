"""The display-redaction cache keys an entry on the prefix set the battery judges under.

``_redact_for_display`` memoizes the display battery, keyed on the text, the
exempt-host set and the pass-4 token-parameter prefix set
(``CredentialPolicy.token_param_exempt_url_prefixes``). The prefix set is read
from the active ``PlatformContext`` and can move between two reads: a companion
loads, a policy tightens, or one read fails and degrades to the empty set.
Reading it once for the key and again inside ``redact_credentials`` would store
output computed under one set under another set's key. These tests pin the
contract: ONE snapshot feeds both the key and the battery, and pass 4 performs
no live read of its own when handed one.

NEUTRAL PLACEHOLDER PREFIX ONLY: a companion's real approval host never appears
in the public repo.
"""

from __future__ import annotations

import contextvars
import zlib
from collections.abc import Iterator

import pytest

import kiro_crew.dashboard.chat_utils as chat_utils
from kiro_crew.dashboard.display_battery import display_battery
from kiro_crew.security import redaction

PREFIX = "https://approve.example.com/auth"
# An opaque value no earlier credential pass claims, so pass 4 is the only pass
# that can touch it and the battery's output turns on the prefix set alone.
VALUE = "Zq7vK2mP9xR4tL8nW3cY6bH1"
TEXT = f"Approve the request here: {PREFIX}?token={VALUE} and then reply."
ALLOWED: tuple[str, ...] = (PREFIX,)
NONE: tuple[str, ...] = ()


class _PrefixSource:
    """A prefix source whose answer moves mid-call, then settles.

    Until :meth:`settle` is called, the FIRST read answers ``first`` and every
    later read answers ``then``: the shape of a set that moves between two reads
    inside one call. After ``settle(prefixes)`` every read answers ``prefixes``.
    """

    def __init__(self, first: tuple[str, ...], then: tuple[str, ...]) -> None:
        self.first = first
        self.then = then
        self.stable: tuple[str, ...] | None = None
        self.seen: list[tuple[str, ...]] = []

    def __call__(self) -> tuple[str, ...]:
        if self.stable is not None:
            value = self.stable
        elif not self.seen:
            value = self.first
        else:
            value = self.then
        self.seen.append(value)
        return value

    def settle(self, prefixes: tuple[str, ...]) -> None:
        self.stable = prefixes


def _install(monkeypatch: pytest.MonkeyPatch, source: _PrefixSource) -> _PrefixSource:
    # Both bindings: the display owner's (through the facade mirror) and the
    # one pass 4 reads when it is not handed a snapshot.
    monkeypatch.setattr(redaction, "_token_param_exempt_prefixes", source)
    monkeypatch.setattr(chat_utils, "_token_param_exempt_prefixes", source)
    return source


def _truth(monkeypatch: pytest.MonkeyPatch, prefixes: tuple[str, ...]) -> str:
    """The battery's output under a STABLE prefix set, computed outside the cache."""
    with monkeypatch.context() as patched:
        patched.setattr(redaction, "_token_param_exempt_prefixes", lambda: prefixes)
        return display_battery(TEXT)


@pytest.fixture(autouse=True)
def _cold_cache() -> Iterator[None]:
    chat_utils._clear_display_redaction_cache()
    yield
    chat_utils._clear_display_redaction_cache()


def test_the_fixture_turns_on_the_prefix_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The text is kept under ``ALLOWED`` and redacted under the empty set."""
    assert _truth(monkeypatch, ALLOWED) == TEXT
    redacted = _truth(monkeypatch, NONE)
    assert redacted != TEXT and VALUE not in redacted


def test_an_entry_is_keyed_on_the_prefix_set_the_battery_judged_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A set that moves mid-call never stores one set's output under another's key.

    During the first call the first read answers ``ALLOWED`` and any later read
    the empty set: a transient failure after the first read, which degrades to
    maximum redaction. If the key and pass 4 read independently, the cache holds
    the REDACTED text under the ``ALLOWED`` key, and every later render under the
    stable ``ALLOWED`` set serves ``[REDACTED]`` for an exempt approval link.
    """
    kept = _truth(monkeypatch, ALLOWED)
    source = _install(monkeypatch, _PrefixSource(first=ALLOWED, then=NONE))
    chat_utils._redact_for_display(TEXT)

    source.settle(ALLOWED)
    served = chat_utils._redact_for_display(TEXT)
    assert served == kept, "the cache served output computed under a different prefix set"


def test_a_tightened_set_mid_call_never_serves_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reverse order: a set that empties after the key read stays redacted.

    The key reads the empty set and pass 4 then reads ``ALLOWED``. If the two
    reads are independent, the kept text is filed as a clean verdict under the
    empty-set key, and every later render under the empty set shows the token.
    """
    redacted = _truth(monkeypatch, NONE)
    source = _install(monkeypatch, _PrefixSource(first=NONE, then=ALLOWED))
    chat_utils._redact_for_display(TEXT)

    source.settle(NONE)
    served = chat_utils._redact_for_display(TEXT)
    assert served == redacted, "the cache served a token the settled prefix set redacts"
    assert VALUE not in served


def test_the_prefix_set_is_read_exactly_once_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """One read per call, cold or warm: the key's snapshot IS pass 4's set."""
    source = _install(monkeypatch, _PrefixSource(first=ALLOWED, then=ALLOWED))
    chat_utils._redact_for_display(TEXT)
    assert len(source.seen) == 1, "a cold call reads the prefix set once, for key and battery"
    chat_utils._redact_for_display(TEXT)
    assert len(source.seen) == 2, "a warm call reads the set once, to derive the key"


def test_the_key_digests_the_snapshot_it_is_handed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A supplied snapshot is the key's whole prefix input; no live read is taken."""

    def forbidden() -> tuple[str, ...]:
        raise AssertionError("the key must digest the snapshot it is handed")

    monkeypatch.setattr(chat_utils, "_token_param_exempt_prefixes", forbidden)
    key = chat_utils._display_redaction_cache_key
    with_prefixes = key(TEXT, None, token_param_prefixes=ALLOWED)
    without = key(TEXT, None, token_param_prefixes=NONE)
    assert with_prefixes != without
    monkeypatch.setattr(chat_utils, "_token_param_exempt_prefixes", lambda: ALLOWED)
    assert key(TEXT, None) == with_prefixes
    monkeypatch.setattr(chat_utils, "_token_param_exempt_prefixes", lambda: NONE)
    assert key(TEXT, None) == without


def test_the_battery_takes_the_snapshot_and_performs_no_live_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``token_param_prefixes`` stands in for the platform read in pass 4."""

    def forbidden() -> tuple[str, ...]:
        raise AssertionError("a caller that supplied a snapshot must not trigger a live read")

    monkeypatch.setattr(redaction, "_token_param_exempt_prefixes", forbidden)
    assert redaction.redact_credentials(TEXT, token_param_prefixes=ALLOWED) == (TEXT, [])
    cleaned, warnings = redaction.redact_credentials(TEXT, token_param_prefixes=NONE)
    assert VALUE not in cleaned and warnings
    assert display_battery(TEXT, token_param_prefixes=ALLOWED) == TEXT
    assert VALUE not in display_battery(TEXT, token_param_prefixes=NONE)


def test_a_snapshot_passes_the_live_reads_shape_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A snapshot cannot exempt more than the live read could: malformed members drop.

    The live read keeps only ``https://host/path`` members with no whitespace.
    This member ends exactly at the ``?token=`` and would be honoured were it
    let through (the ungated control below), so it is the gate that drops it.
    """
    malformed = (f"here: {PREFIX}",)
    monkeypatch.setattr(redaction, "_token_param_exempt_prefixes", lambda: ALLOWED)
    cleaned, _ = redaction.redact_credentials(TEXT, token_param_prefixes=malformed)
    assert VALUE not in cleaned
    assert VALUE not in display_battery(TEXT, token_param_prefixes=malformed)

    monkeypatch.setattr(redaction, "_well_formed_token_param_prefixes", tuple)
    assert redaction.redact_credentials(TEXT, token_param_prefixes=malformed)[0] == TEXT


def test_the_stream_suspension_still_wins_over_a_snapshot() -> None:
    """A context-incomplete streamed window pins pass 4's set empty, snapshot or not."""

    def suspended_window() -> str:
        # Set inside a copied context, so the suspension never outlives this call.
        redaction._TOKEN_PARAM_EXEMPT_SUSPENDED.set(True)
        return redaction.redact_credentials(TEXT, token_param_prefixes=ALLOWED)[0]

    cleaned = contextvars.copy_context().run(suspended_window)
    assert VALUE not in cleaned
    assert redaction._TOKEN_PARAM_EXEMPT_SUSPENDED.get() is False


def test_the_default_path_still_reads_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other caller is untouched: no snapshot means the live read."""
    monkeypatch.setattr(redaction, "_token_param_exempt_prefixes", lambda: ALLOWED)
    assert redaction.redact_credentials(TEXT) == (TEXT, [])
    assert display_battery(TEXT) == TEXT
    monkeypatch.setattr(redaction, "_token_param_exempt_prefixes", lambda: NONE)
    assert VALUE not in redaction.redact_credentials(TEXT)[0]
    assert VALUE not in display_battery(TEXT)


# A PlantUML link carries its diagram source, and the credential pass judges that
# source with a nested ``redact_credentials`` call before it lets the link stand.
# The source holds the approval link, so the nested call reads the prefix set too.
_PLANTUML_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"


def _plantuml_encode(source: str) -> str:
    """Encode *source* the way PlantUML servers expect: raw deflate, own base64."""
    deflater = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    data = deflater.compress(source.encode()) + deflater.flush()
    data += b"\0" * (-len(data) % 3)
    out = []
    for i in range(0, len(data), 3):
        word = int.from_bytes(data[i : i + 3], "big")
        out.extend(_PLANTUML_ALPHABET[word >> shift & 0x3F] for shift in (18, 12, 6, 0))
    return "".join(out)


DIAGRAM_SOURCE = f"@startuml\nUser -> Approver: {PREFIX}?token={VALUE}\n@enduml"
DIAGRAM = f"see https://www.plantuml.com/plantuml/svg/{_plantuml_encode(DIAGRAM_SOURCE)} here"


def test_a_diagram_scan_takes_the_snapshot_and_performs_no_live_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nested scan of a diagram's source judges under the caller's snapshot."""

    def forbidden() -> tuple[str, ...]:
        raise AssertionError("a diagram scan under a snapshot must not trigger a live read")

    monkeypatch.setattr(redaction, "_token_param_exempt_prefixes", forbidden)
    kept, _ = redaction.redact_credentials(DIAGRAM, token_param_prefixes=ALLOWED)
    masked, _ = redaction.redact_credentials(DIAGRAM, token_param_prefixes=NONE)
    assert kept == DIAGRAM, "the diagram's approval link is exempt under ALLOWED"
    assert masked != DIAGRAM, "the fixture must turn on the prefix set"


def test_a_diagram_entry_is_keyed_on_the_set_its_scan_judged_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A set that empties after the key read never leaves a diagram cached clean."""
    with monkeypatch.context() as patched:
        patched.setattr(redaction, "_token_param_exempt_prefixes", lambda: NONE)
        masked = display_battery(DIAGRAM)
    assert masked != DIAGRAM, "the fixture must turn on the prefix set"

    source = _install(monkeypatch, _PrefixSource(first=NONE, then=ALLOWED))
    chat_utils._redact_for_display(DIAGRAM)
    source.settle(NONE)
    assert chat_utils._redact_for_display(DIAGRAM) == masked
