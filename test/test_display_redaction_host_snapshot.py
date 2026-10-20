"""The display-redaction cache keys an entry on the host set the battery judges under.

``_redact_for_display`` memoizes the display battery, keyed on the text and the
exempt-host set. The set is read from the active ``PlatformContext`` and can
move between two reads: a companion loads, a policy tightens, or one read fails
and degrades to the empty set. Reading it once for the key and again inside the
battery would store output computed under one set under another set's key.
These tests pin the contract: ONE snapshot feeds both the key and the battery,
and the battery performs no live read of its own when handed one.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import kiro_crew.dashboard.chat_utils as chat_utils
from kiro_crew.dashboard.display_battery import display_battery
from kiro_crew.security import exfil

TENANT = "docs.contoso.sharepoint.com"
# A long query trips the query-length rule on every host that is not exempt and
# carries nothing the credential remover rewrites, so the battery's output
# depends on the exempt-host set and on nothing else.
TEXT = f"see https://{TENANT}/sites/x/doc.docx?id={'a' * 250}"
ALLOWED = frozenset({TENANT})


def _truth(hosts: frozenset[str]) -> str:
    """The battery's output under a STABLE host set, computed outside the cache."""
    return display_battery(TEXT, exempt_hosts=hosts)


class _HostSource:
    """A host source whose answer moves mid-call, then settles.

    Until :meth:`settle` is called, the FIRST read answers ``first`` and every
    later read answers ``then``: the shape of a set that moves between two reads
    inside one call. After ``settle(hosts)`` every read answers ``hosts``.
    """

    def __init__(self, first: frozenset[str], then: frozenset[str]) -> None:
        self.first = first
        self.then = then
        self.stable: frozenset[str] | None = None
        self.seen: list[frozenset[str]] = []

    def __call__(self) -> frozenset[str]:
        if self.stable is not None:
            value = self.stable
        elif not self.seen:
            value = self.first
        else:
            value = self.then
        self.seen.append(value)
        return value

    def settle(self, hosts: frozenset[str]) -> None:
        self.stable = hosts


def _install(monkeypatch: pytest.MonkeyPatch, source: _HostSource) -> _HostSource:
    # Both bindings: the display owner's (through the facade mirror) and the
    # one the battery reads when it is not handed a snapshot.
    monkeypatch.setattr(exfil, "_exempt_exact_hosts", source)
    monkeypatch.setattr(chat_utils, "_exempt_exact_hosts", source)
    return source


@pytest.fixture(autouse=True)
def _cold_cache() -> Iterator[None]:
    chat_utils._clear_display_redaction_cache()
    yield
    chat_utils._clear_display_redaction_cache()


def test_an_entry_is_keyed_on_the_set_the_battery_judged_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A set that moves mid-call never stores one set's output under another's key.

    During the first call the first read answers ``ALLOWED`` and any later read
    the empty set: a transient failure after the first read, which degrades to
    maximum redaction. If the key and the battery read independently, the cache
    holds the REDACTED text under the ``ALLOWED`` key, and every later render
    under the stable ``ALLOWED`` set serves ``[REDACTED]`` for an exempt host.
    """
    kept = _truth(ALLOWED)
    redacted = _truth(frozenset())
    assert kept == TEXT and redacted != TEXT, "the fixture must turn on the host set"

    source = _install(monkeypatch, _HostSource(first=ALLOWED, then=frozenset()))
    chat_utils._redact_for_display(TEXT)

    source.settle(ALLOWED)
    served = chat_utils._redact_for_display(TEXT)
    assert served == kept, "the cache served output computed under a different host set"


def test_a_tightened_set_mid_call_never_serves_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reverse order: a set that empties after the key read stays redacted."""
    source = _install(monkeypatch, _HostSource(first=frozenset(), then=ALLOWED))
    chat_utils._redact_for_display(TEXT)
    source.settle(frozenset())
    assert chat_utils._redact_for_display(TEXT) == _truth(frozenset()) != TEXT


def test_the_host_set_is_read_exactly_once_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """One read per call, cold or warm: the key's snapshot IS the battery's set."""
    source = _install(monkeypatch, _HostSource(first=ALLOWED, then=ALLOWED))
    chat_utils._redact_for_display(TEXT)
    assert len(source.seen) == 1, "a cold call reads the host set once, for key and battery"
    chat_utils._redact_for_display(TEXT)
    assert len(source.seen) == 2, "a warm call reads the set once, to derive the key"


def test_the_snapshot_carries_the_scoped_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts relaxed by ``scoped_exempt_hosts`` reach the battery through the snapshot."""
    _install(monkeypatch, _HostSource(first=frozenset(), then=frozenset()))
    with exfil.scoped_exempt_hosts(ALLOWED):
        inside = chat_utils._redact_for_display(TEXT)
    outside = chat_utils._redact_for_display(TEXT)
    assert inside == TEXT
    assert outside == _truth(frozenset()) != TEXT
    entries, _ = chat_utils._display_redaction_cache_info()
    assert entries == 1, "only the redacted output is stored; the kept one is a clean verdict"


def test_the_key_digests_the_snapshot_it_is_handed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A supplied snapshot is the key's whole host input; no live read is taken."""

    def forbidden() -> frozenset[str]:
        raise AssertionError("the key must digest the snapshot it is handed")

    monkeypatch.setattr(chat_utils, "_exempt_exact_hosts", forbidden)
    with_hosts = chat_utils._display_redaction_cache_key(TEXT, None, ALLOWED)
    without = chat_utils._display_redaction_cache_key(TEXT, None, frozenset())
    assert with_hosts != without
    monkeypatch.setattr(chat_utils, "_exempt_exact_hosts", lambda: ALLOWED)
    assert chat_utils._display_redaction_cache_key(TEXT, None) == with_hosts


def test_the_battery_takes_the_snapshot_and_performs_no_live_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``exempt_hosts`` stands in for the platform set AND the scoped override."""

    def forbidden() -> frozenset[str]:
        raise AssertionError("a caller that supplied a snapshot must not trigger a live read")

    monkeypatch.setattr(exfil, "_exfil_exempt_hosts", forbidden)
    monkeypatch.setattr(exfil, "_exempt_exact_hosts", forbidden)

    cleaned, warnings, records = exfil.redact_exfiltration_urls_with_records(
        TEXT, exempt_hosts=ALLOWED
    )
    assert (cleaned, warnings, records) == (TEXT, [], [])

    cleaned, warnings, records = exfil.redact_exfiltration_urls_with_records(
        TEXT, exempt_hosts=frozenset()
    )
    assert cleaned != TEXT and warnings and [r["domain"] for r in records] == [TENANT]

    # The snapshot is the whole set, taken by the caller inside the same scope,
    # so a scoped override in effect is not consulted on top of it.
    with exfil.scoped_exempt_hosts(ALLOWED):
        cleaned, _, _ = exfil.redact_exfiltration_urls_with_records(TEXT, exempt_hosts=frozenset())
    assert cleaned != TEXT

    cleaned, warnings = exfil.redact_exfiltration_urls(TEXT, exempt_hosts=ALLOWED)
    assert (cleaned, warnings) == (TEXT, [])


def test_the_snapshot_is_lowercased_like_the_live_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """A snapshot is matched the way the live read is: hosts compare case-insensitively."""
    monkeypatch.setattr(exfil, "_exfil_exempt_hosts", lambda: frozenset())
    cleaned, _, _ = exfil.redact_exfiltration_urls_with_records(
        TEXT, exempt_hosts=frozenset({TENANT.upper()})
    )
    assert cleaned == TEXT


def test_extra_exempt_hosts_still_joins_a_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-call additive set is orthogonal to the snapshot and keeps its meaning."""
    monkeypatch.setattr(exfil, "_exfil_exempt_hosts", lambda: frozenset())
    cleaned, _, _ = exfil.redact_exfiltration_urls_with_records(
        TEXT, exempt_hosts=frozenset(), extra_exempt_hosts=ALLOWED
    )
    assert cleaned == TEXT


def test_the_default_path_still_reads_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other caller is untouched: no snapshot means the live read, plus the scope."""
    monkeypatch.setattr(exfil, "_exfil_exempt_hosts", lambda: ALLOWED)
    cleaned, _, _ = exfil.redact_exfiltration_urls_with_records(TEXT)
    assert cleaned == TEXT
    monkeypatch.setattr(exfil, "_exfil_exempt_hosts", lambda: frozenset())
    cleaned, _, _ = exfil.redact_exfiltration_urls_with_records(TEXT)
    assert cleaned != TEXT
    with exfil.scoped_exempt_hosts(ALLOWED):
        cleaned, _, _ = exfil.redact_exfiltration_urls_with_records(TEXT)
    assert cleaned == TEXT
