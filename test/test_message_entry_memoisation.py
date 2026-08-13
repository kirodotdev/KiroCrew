"""Memoisation of the persisted-message entry builder.

The save path re-serialises the whole in-memory window on every flush, so
redaction re-runs for messages that have not changed. The builder is therefore
split into a content-keyed cache in front of an unchanged implementation.

Four properties matter, and they fail for different reasons:

  equivalence  -- the cached result must equal what the UNCACHED function
                  computes, not merely equal itself on a second call. Asserting
                  self-consistency would pass with a key derived from ``id(m)``;
                  asserting against the uncached function is what catches a
                  broken key derivation.
  invalidation -- the slot mutates messages in place (a stop event resolving, a
                  banner completing), and there is no explicit invalidation
                  hook, so a changed message MUST produce a changed key.
  redaction    -- the cached value must be the post-redaction entry. This is the
                  one property whose failure is a security regression rather
                  than a missed optimisation.
  transient    -- roles that build no entry cache a legitimate ``None``, so a
                  ``None`` hit must not be re-treated as a miss.
"""

from __future__ import annotations

import json
import threading

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_persistence
from kiro_crew.dashboard.chat_persistence import (
    _build_message_entry,
    _build_message_entry_uncached,
    _entry_cache,
)

# Shapes verified against the live detector rather than assumed: each of these
# is redacted by ``redact_credentials``. Kept in one place so a detector change
# surfaces as one failure.
AWS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
BEARER = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefghij.klmnopqrst"
TOKEN = "ghp_16C7e42F292c6912E7710c838347Ae178B4a"
SECRETS = (AWS_KEY_ID, AWS_SECRET, BEARER, TOKEN)

TRANSIENT_ROLES = ("chunk", "done", "streaming", "queued", "permission")

# The cache and its byte counter are reset between tests by the autouse
# ``_isolate_message_entry_cache`` fixture in conftest, which covers every module
# rather than only this one -- the module that pollutes the cache is not
# necessarily the one that misreads it.


def _corpus() -> list[dict]:
    """Mixed roles, with and without variants/meta, plus every secret shape."""
    return [
        {"role": "assistant", "content": f"key {AWS_KEY_ID} here", "ts": "t1"},
        {"role": "user", "content": "just typing", "ts": "t2"},
        {"role": "system", "content": f"auth {BEARER}", "ts": "t3", "cls": "msg"},
        {
            "role": "tool",
            "content": f"secret {AWS_SECRET}",
            "ts": "t4",
            "meta": {"mid": "m-4", "note": f"token {TOKEN}"},
        },
        {
            "role": "assistant",
            "content": "with variants",
            "ts": "t5",
            "variants": [
                {"content": f"v1 {AWS_KEY_ID}", "model": "m1"},
                {"content": f"v2 {TOKEN}", "model": "m2"},
            ],
            "variant_idx": 0,
        },
        *({"role": r, "content": "transient", "ts": f"t-{r}"} for r in TRANSIENT_ROLES),
    ]


# ── 1. equivalence: cached == uncached, asserted against the uncached function ─


@pytest.mark.parametrize("message", _corpus(), ids=lambda m: m["role"] + "-" + m["ts"])
def test_cached_entry_equals_the_uncached_computation(message: dict) -> None:
    expected = _build_message_entry_uncached(dict(message))
    assert _build_message_entry(message) == expected


def test_cached_entry_equals_uncached_across_the_whole_corpus() -> None:
    """Whole-window form: this is the shape the save path actually drives."""
    corpus = _corpus()
    expected = [_build_message_entry_uncached(dict(m)) for m in corpus]
    assert [_build_message_entry(m) for m in corpus] == expected
    # Second pass is served from cache and must still agree.
    assert [_build_message_entry(m) for m in corpus] == expected


def test_a_repeat_call_is_served_from_the_cache() -> None:
    """Without this the suite could pass with the cache never being consulted,
    making every other assertion here vacuous."""
    m = {"role": "assistant", "content": "hello", "ts": "t"}
    first = _build_message_entry(m)
    assert len(_entry_cache) == 1
    assert _build_message_entry(m) is first  # same object -> genuine hit


# ── 2. invalidation: in-place mutation must re-run the build ──────────────────


def test_in_place_content_mutation_invalidates() -> None:
    m = {"role": "assistant", "content": "before", "ts": "t"}
    assert _build_message_entry(m)["content"] == "before"
    m["content"] = "after"
    assert _build_message_entry(m)["content"] == "after"


def test_in_place_meta_mutation_invalidates() -> None:
    """Meta is where the in-place edits actually happen (a stop event resolving,
    a file-change chip landing), so keying on content alone would serve stale."""
    m = {"role": "assistant", "content": "same", "ts": "t", "meta": {"mid": "m-1"}}
    _build_message_entry(m)
    m["meta"]["stopped"] = True
    assert _build_message_entry(m)["meta"].get("stopped") is True


def test_in_place_variant_mutation_invalidates() -> None:
    m = {
        "role": "assistant",
        "content": "c",
        "ts": "t",
        "variants": [{"content": "one", "model": "m1"}],
        "variant_idx": 0,
    }
    _build_message_entry(m)
    m["variants"].append({"content": "two", "model": "m2"})
    entry = _build_message_entry(m)
    assert [v["content"] for v in entry["variants"]] == ["one", "two"]


def test_mutation_matches_a_freshly_computed_entry() -> None:
    """Stronger than "changed": the post-mutation entry must equal what the
    uncached function computes for the mutated message."""
    m = {"role": "assistant", "content": f"first {AWS_KEY_ID}", "ts": "t"}
    _build_message_entry(m)
    m["content"] = f"second {TOKEN}"
    assert _build_message_entry(m) == _build_message_entry_uncached(dict(m))


# ── 3. redaction: the cached value is the redacted entry (security gate) ──────


def _flatten(entry: dict | None) -> str:
    """Every string anywhere in the entry, so a secret cannot hide in a variant
    or in meta."""
    if entry is None:
        return ""
    import json

    return json.dumps(entry, default=str)


@pytest.mark.parametrize("role", ["assistant", "system", "tool"])
@pytest.mark.parametrize("secret", SECRETS)
def test_cached_value_is_redacted_not_raw(role: str, secret: str) -> None:
    m = {"role": role, "content": f"here it is {secret}", "ts": "t", "cls": "msg"}
    first = _build_message_entry(m)
    assert secret not in _flatten(first)
    # And the cached read is redacted too -- the property that would fail if the
    # raw input were stored instead of the built entry.
    assert secret not in _flatten(_build_message_entry(m))


@pytest.mark.parametrize("secret", SECRETS)
def test_secrets_in_variants_and_meta_are_redacted_through_the_cache(
    secret: str,
) -> None:
    m = {
        "role": "assistant",
        "content": "clean",
        "ts": "t",
        "variants": [{"content": f"leak {secret}", "model": "m1"}],
        "variant_idx": 0,
        "meta": {"mid": "m-1", "tool_input": f"leak {secret}"},
    }
    _build_message_entry(m)
    assert secret not in _flatten(_build_message_entry(m))


def test_no_secret_survives_anywhere_in_the_cached_corpus() -> None:
    """Sweep: after building the whole corpus, no secret shape may appear in any
    cached value."""
    for m in _corpus():
        _build_message_entry(m)
    blob = "".join(_flatten(v) for v, _size in _entry_cache.values())
    for secret in SECRETS:
        assert secret not in blob


# ── 4. transient roles: a cached None is a value, not a miss ──────────────────


@pytest.mark.parametrize("role", TRANSIENT_ROLES)
def test_transient_roles_return_none_through_the_cache(role: str) -> None:
    m = {"role": role, "content": "x", "ts": "t"}
    assert _build_message_entry(m) is None
    assert _build_message_entry(m) is None


@pytest.mark.parametrize("role", TRANSIENT_ROLES)
def test_a_cached_none_is_a_hit_not_a_miss(role: str) -> None:
    """A ``None`` hit must be distinguished by MEMBERSHIP, not truthiness. Keyed
    on truthiness the entry would be rebuilt on every call and the cache would
    silently do nothing for these roles."""
    m = {"role": role, "content": "x", "ts": "t"}
    _build_message_entry(m)
    assert len(_entry_cache) == 1
    key = next(iter(_entry_cache))
    assert _entry_cache[key][0] is None
    _build_message_entry(m)
    assert len(_entry_cache) == 1  # no duplicate insert -> it was a hit


def test_transient_none_agrees_with_the_uncached_function() -> None:
    for role in TRANSIENT_ROLES:
        m = {"role": role, "content": "x", "ts": "t"}
        assert _build_message_entry(m) == _build_message_entry_uncached(dict(m))


# ── bounds: entry count, total bytes, and per-entry rejection ─────────────────


def test_cache_is_bounded_by_entry_count() -> None:
    for i in range(chat_persistence._ENTRY_CACHE_MAX + 50):
        _build_message_entry({"role": "assistant", "content": f"m{i}", "ts": f"t{i}"})
    assert len(_entry_cache) == chat_persistence._ENTRY_CACHE_MAX


def test_byte_counter_tracks_the_stored_entries() -> None:
    """The counter is what enforces the memory ceiling, so it must equal the sum
    of the stored sizes -- a counter that drifts upward would evict a healthy
    cache down to nothing, and one that drifts downward stops bounding at all."""
    for i in range(50):
        _build_message_entry({"role": "assistant", "content": f"m{i}" * 10, "ts": f"t{i}"})
    assert chat_persistence._entry_cache_bytes == sum(s for _e, s in _entry_cache.values())


def test_a_concurrent_double_insert_does_not_inflate_the_byte_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two threads can both MISS on the same key -- the hit-check releases the
    lock before the build -- and both then insert. The insert must replace rather
    than add, or the counter would double-count and evict a healthy cache.

    A plain repeated call cannot test this: the second call returns early on the
    hit and never reaches the insert. The barrier is what holds both threads past
    the hit-check so the race is deterministic rather than incidental.
    """
    m = {"role": "assistant", "content": "raced", "ts": "t"}
    barrier = threading.Barrier(2)
    real = chat_persistence._build_message_entry_uncached

    def blocking_build(msg: dict):
        barrier.wait(timeout=10)
        return real(msg)

    monkeypatch.setattr(chat_persistence, "_build_message_entry_uncached", blocking_build)
    threads = [threading.Thread(target=_build_message_entry, args=(m,)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive()

    assert len(_entry_cache) == 1
    assert chat_persistence._entry_cache_bytes == sum(s for _e, s in _entry_cache.values())


def test_total_byte_ceiling_evicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bounding entries alone does not bound memory: an entry is as large as its
    message. With a low byte ceiling the cache must evict on bytes even though
    the entry count is nowhere near its own bound."""
    monkeypatch.setattr(chat_persistence, "_ENTRY_CACHE_MAX_BYTES", 4000)
    for i in range(40):
        _build_message_entry({"role": "assistant", "content": "x" * 500, "ts": f"t{i}"})
    assert len(_entry_cache) < 40
    assert chat_persistence._entry_cache_bytes <= 4000


def test_an_oversized_entry_is_returned_but_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One huge message must not be allowed to evict the whole cache, so it is
    computed and returned without being stored."""
    monkeypatch.setattr(chat_persistence, "_ENTRY_MAX_CACHEABLE_BYTES", 1000)
    small = {"role": "assistant", "content": "small", "ts": "t1"}
    _build_message_entry(small)
    assert len(_entry_cache) == 1
    huge = {"role": "assistant", "content": "y" * 5000, "ts": "t2"}
    entry = _build_message_entry(huge)
    assert entry == _build_message_entry_uncached(dict(huge))
    assert len(_entry_cache) == 1  # the small one survives; the huge one is absent


def test_an_oversized_entry_is_still_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The uncacheable path must not become a redaction bypass."""
    monkeypatch.setattr(chat_persistence, "_ENTRY_MAX_CACHEABLE_BYTES", 100)
    entry = _build_message_entry(
        {"role": "assistant", "content": f"{'p' * 500} {AWS_KEY_ID}", "ts": "t"}
    )
    assert AWS_KEY_ID not in _flatten(entry)


# ── flush site: a window past the bound cannot hit, so it goes uncached ───────


def test_a_window_longer_than_the_bound_bypasses_the_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Such a window evicts itself before the next save reaches it, so routing it
    through the cache would pay the hashing cost for a guaranteed 0% hit rate."""
    from kiro_crew.dashboard.chat import _save_slot_to_history

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(chat_persistence, "_ENTRY_CACHE_MAX", 3)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    for i in range(5):
        slot.append("assistant", f"message {i}")
    slot.drain()
    _entry_cache.clear()

    _save_slot_to_history(state, slot, closed=True)

    assert len(_entry_cache) == 0


def test_a_window_within_the_bound_still_uses_the_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the bypass: without this, the test above would pass even if
    the cache were never populated by a save at all."""
    from kiro_crew.dashboard.chat import _save_slot_to_history

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(chat_persistence, "_ENTRY_CACHE_MAX", 100)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    for i in range(5):
        slot.append("assistant", f"message {i}")
    slot.drain()
    _entry_cache.clear()

    _save_slot_to_history(state, slot, closed=True)

    assert len(_entry_cache) > 0


# ── a mutation racing the build must not be cached ────────────────────────────


def test_a_mutation_during_the_build_is_not_cached() -> None:
    """The key is read before the build, so a mutation between them would file the
    new entry under the old state's key.

    The flush thread shares message dicts with the event loop, so a variant switch
    can land in that gap. Storing the pair is what makes it durable: a switch
    restores content AND ts from the stored variant, so switching back reproduces
    the old key exactly and would be served the other variant's entry.
    """
    real = _build_message_entry_uncached
    m = {"role": "assistant", "content": "variant A", "ts": "t1", "variant_idx": 0}

    def mutating(msg: dict) -> dict | None:
        msg["content"] = "variant B"
        msg["variant_idx"] = 1
        return real(msg)

    chat_persistence._build_message_entry_uncached = mutating  # type: ignore[assignment]
    try:
        entry = _build_message_entry(m)
    finally:
        chat_persistence._build_message_entry_uncached = real  # type: ignore[assignment]

    assert entry is not None
    assert entry["content"] == "variant B"
    assert len(_entry_cache) == 0
    assert chat_persistence._entry_cache_bytes == 0


def test_switching_back_after_a_racing_mutation_recomputes() -> None:
    """The observable harm the refusal prevents: the wrong variant persisted.

    Without the refusal the first call leaves ``{key(A): entry(B)}`` behind, so
    reverting to A hits that pair and writes B to the transcript while the UI
    shows A -- and unlike the pre-existing torn write, it does not self-correct.
    """
    real = _build_message_entry_uncached
    m = {"role": "assistant", "content": "variant A", "ts": "t1", "variant_idx": 0}

    def mutating(msg: dict) -> dict | None:
        msg["content"] = "variant B"
        msg["variant_idx"] = 1
        return real(msg)

    chat_persistence._build_message_entry_uncached = mutating  # type: ignore[assignment]
    try:
        _build_message_entry(m)
    finally:
        chat_persistence._build_message_entry_uncached = real  # type: ignore[assignment]

    m["content"] = "variant A"
    m["variant_idx"] = 0
    assert _build_message_entry(m)["content"] == "variant A"  # type: ignore[index]


# ── flush site: a window past the BYTE ceiling also cannot hit ────────────────


def test_the_window_payload_estimate_is_a_lower_bound() -> None:
    """Gating on it is only sound because it under-counts: an estimate above the
    ceiling proves the real payload is above it too."""
    window = [
        {
            "role": "assistant",
            "content": "x" * 100,
            "ts": "t1",
            "variants": [{"content": "y" * 50}],
        }
    ]
    estimate = chat_persistence._approx_window_payload_bytes(window)
    actual = sum(len(json.dumps(m, sort_keys=True, default=str)) for m in window)

    assert estimate == 150
    assert estimate <= actual


def test_a_window_past_the_byte_ceiling_bypasses_the_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Such a window self-evicts through the byte ceiling at a message count well
    under the entry bound, so it hits 0% while still paying to hash every byte.

    The message count stays under ``_ENTRY_CACHE_MAX`` and each message stays
    under the per-entry cap, so neither of the other two bypasses can account for
    an empty cache here.
    """
    from kiro_crew.dashboard.chat import _save_slot_to_history

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(chat_persistence, "_ENTRY_CACHE_MAX", 100)
    monkeypatch.setattr(chat_persistence, "_ENTRY_CACHE_MAX_BYTES", 1000)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    for i in range(5):
        slot.append("assistant", f"{i}" + "z" * 500)
    slot.drain()
    _entry_cache.clear()

    _save_slot_to_history(state, slot, closed=True)

    assert len(slot.messages) <= chat_persistence._ENTRY_CACHE_MAX
    assert all(
        len(m.get("content") or "") < chat_persistence._ENTRY_MAX_CACHEABLE_BYTES
        for m in slot.messages
    )
    assert len(_entry_cache) == 0
