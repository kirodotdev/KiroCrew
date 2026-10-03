"""The display-redaction cache keys an entry on the host set the battery judges under.

``_redact_for_display`` memoizes the exfiltration + credential battery, keyed on
the text and the exempt-host set. The set is read from the active
``PlatformContext`` and can move between two reads: a companion loads, a policy
tightens, or one read fails and degrades to the empty set. Reading it once for
the key and again inside the battery would store output computed under one set
under another set's key. These tests pin the contract: ONE snapshot, taken
through ``_exfil_exempt_hosts`` (the accessor the battery itself reads), feeds
both the key and the battery, and the battery performs no live read of its own.
"""

from __future__ import annotations

import pytest

import kiro_crew.dashboard.chat_utils as chat_utils
from kiro_crew.security import exfil, redact_credentials

TENANT = "docs.contoso.sharepoint.com"
# A long query trips ``exfil_query_length`` on every host that is not exempt,
# and carries nothing the credential remover rewrites, so the battery's output
# depends on the exempt-host set and on nothing else.
TEXT = f"see https://{TENANT}/sites/x/doc.docx?id={'a' * 250}"
ALLOWED = frozenset({TENANT})


def _truth(hosts: frozenset[str]) -> str:
    """The battery's output under a STABLE host set, computed outside the cache."""
    with exfil.scoped_exempt_hosts(hosts):
        cleaned, _ = exfil.redact_exfiltration_urls(TEXT)
    cleaned, _ = redact_credentials(cleaned)
    return cleaned


class _HostSource:
    """A host source whose answer moves mid-call, then settles.

    Until :meth:`settle` is called, the FIRST read answers ``first`` and every
    later read answers ``then`` -- the shape of a set that moves between two reads
    inside one call, whatever the number of reads. After ``settle(hosts)`` every
    read answers ``hosts``. Installed at the root both accessors resolve through
    (``exfil._exempt_exact_hosts``) and, where the consumer still holds its own
    binding of that name, there too.
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
    monkeypatch.setattr(exfil, "_exempt_exact_hosts", source)
    monkeypatch.setattr(chat_utils, "_exempt_exact_hosts", source, raising=False)
    return source


@pytest.fixture(autouse=True)
def _cold_cache() -> None:
    chat_utils._clear_display_redaction_cache()


def test_an_entry_is_keyed_on_the_set_the_battery_judged_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A set that moves mid-call never stores one set's output under another's key.

    During the first call the first read answers ``ALLOWED`` and any later read
    the empty set -- the shape of a transient failure after the first read, which
    degrades to maximum redaction. If the key and the battery read independently,
    the cache holds the REDACTED text under the ``ALLOWED`` key, and every later
    render under the stable ``ALLOWED`` set serves a ``[REDACTED]`` link for a
    host the policy exempts.
    """
    kept = _truth(ALLOWED)
    redacted = _truth(frozenset())
    assert kept == TEXT and redacted != TEXT, "the fixture must turn on the host set"

    source = _install(monkeypatch, _HostSource(first=ALLOWED, then=frozenset()))
    chat_utils._redact_for_display(TEXT)

    # The set is now stably ALLOWED: whatever the cache holds under that key is
    # what every subsequent render shows.
    source.settle(ALLOWED)
    served = chat_utils._redact_for_display(TEXT)
    assert served == kept, "the cache served output computed under a different host set"


def test_the_host_set_is_read_exactly_once_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """One read per call, cold or warm: the key's snapshot IS the battery's set."""
    source = _install(monkeypatch, _HostSource(first=ALLOWED, then=ALLOWED))
    chat_utils._redact_for_display(TEXT)
    assert len(source.seen) == 1, "a cold call reads the host set once, for key and battery alike"
    chat_utils._redact_for_display(TEXT)
    assert len(source.seen) == 2, "a warm call reads the set once, to derive the key"


def test_the_snapshot_carries_the_scoped_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The snapshot composes with ``scoped_exempt_hosts``: the battery relaxes the scoped hosts.

    ``_prepare_messages`` wraps the render in the reader's allowed hosts for the
    slot's workspace. Those hosts reach the battery through the same snapshot the
    key is derived from, so an allowed link is kept inside the scope, redacted
    outside it, and the two renders live under different keys.
    """
    _install(monkeypatch, _HostSource(first=frozenset(), then=frozenset()))
    with exfil.scoped_exempt_hosts(ALLOWED):
        inside = chat_utils._redact_for_display(TEXT)
    outside = chat_utils._redact_for_display(TEXT)
    assert inside == TEXT
    assert outside == _truth(frozenset()) != TEXT
    entries, _ = chat_utils._display_redaction_cache_info()
    assert entries == 2


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

    # A scoped override in effect is NOT consulted on top of a snapshot: the
    # snapshot is the whole set, taken by the caller inside that same scope.
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
