"""The persisted revocation counter that ``kirocrew logout`` bumps.

The counter's fail-closed read and write rules are pinned in ``test_refresh_tokens.py``
(TR-U-28..37). This file pins that the event loop never waits on a bump's write, that
only that write leaves the loop, and that a logout keeps every peer pin in force until
the write lands.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Callable

import pytest
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _session_token,
    clear_nonces,
)

import kiro_crew.dashboard.revocation_gen as rg
import kiro_crew.dashboard.token_auth_tokens as tat
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout, finished


def _stuck_replace(in_write: threading.Event, release: threading.Event) -> Callable[..., None]:
    """A ``replace_with_retry`` that signals *in_write* and holds the rename until *release*."""
    real_replace = rg.replace_with_retry

    def _replace(src: Path, dst: Path) -> None:
        in_write.set()
        release.wait(default_timeout())
        real_replace(src, dst)

    return _replace


def test_validation_reads_the_counter_while_a_bump_write_is_stuck(
    tmp_path: Path, monkeypatch
) -> None:
    """A slow counter write never holds up token validation, and publishes only once it lands.

    Every token validation calls ``current_revocation_gen_or_none`` on the event loop,
    while the logout's counter write runs on a worker thread. Where on-access antivirus
    scans the new temp file, that write takes seconds, and a lock held across it would
    park the loop until the loop watchdog kills the gateway. Here the bump is held
    before its rename on an Event, so the read either answers while the write is stuck
    or it does not. It must answer with the old value: the new one is not durable yet.
    """
    # The read gets half the lost-run bound and the stuck write all of it, so a
    # read that waits on the write fails while the write is still held.
    read_bound = default_timeout() / 2
    in_write = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(rg, "_gen", 4)
    monkeypatch.setattr(rg, "replace_with_retry", _stuck_replace(in_write, release))

    bumped: list[int] = []
    writer = threading.Thread(target=lambda: bumped.append(rg.bump_revocation_gen()), daemon=True)
    writer.start()
    try:
        assert in_write.wait(default_timeout()), "the bump never reached its write"
        seen: list[int | None] = []
        reader = threading.Thread(
            target=lambda: seen.append(rg.current_revocation_gen_or_none()), daemon=True
        )
        reader.start()
        reader.join(read_bound)
        assert not reader.is_alive(), "the counter read waited for the stuck bump write"
        assert seen == [4]
    finally:
        release.set()
        writer.join(default_timeout())

    assert not writer.is_alive()
    assert bumped == [5]
    assert rg.current_revocation_gen_or_none() == 5
    assert (tmp_path / rg._REVOCATION_FILE).read_text(encoding="utf-8") == "5"


@pytest.mark.asyncio
async def test_a_pin_stays_in_force_while_the_logout_write_is_stuck(monkeypatch) -> None:
    """A logout whose counter write is stuck still checks a pre-logout cookie's pin.

    ``POST /api/logout`` awaits ``revoke_all_sessions_async``, so the loop serves
    requests while the counter write is slow. Until the new generation is published a
    pre-logout cookie still validates, and ``check_peer`` accepts a token with no
    binding, so clearing the pins first would let a stolen cookie be replayed from any
    address for as long as the write takes.
    """
    in_write = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(rg, "_gen", 4)
    monkeypatch.setattr(rg, "replace_with_retry", _stuck_replace(in_write, release))

    token = _session_token("owner")
    tat.bind_token_peer(token, "ip:1.1.1.1")
    assert tat.check_token_peer(token, "ip:2.2.2.2") == (False, "IP mismatch")

    logout = asyncio.create_task(tat.revoke_all_sessions_async())
    try:
        await async_wait_until(in_write.is_set, describe=lambda: "the write was never reached")
        assert rg.current_revocation_gen_or_none() == 4
        assert tat.check_token_peer(token, "ip:2.2.2.2") == (False, "IP mismatch")
    finally:
        release.set()
        await await_until(logout, what="the logout")

    assert rg.current_revocation_gen_or_none() == 5
    assert tat.validate_token(token, use_session_exp=True) == (False, "", "session revoked")
    assert tat.check_token_peer(token, "ip:2.2.2.2") == (True, "")


@pytest.mark.asyncio
async def test_only_the_logout_write_leaves_the_event_loop(monkeypatch) -> None:
    """The counter write runs on a worker thread; publishing and clearing run on the loop.

    A loop handler checks a token's generation and then mints its replacement with no
    await between. A generation published, or pins cleared, from another thread could
    land inside that gap and let a pre-logout link or cookie mint a session that
    survives the logout, so both stay on the loop, where they are atomic for it.
    """
    ran_on: dict[str, int] = {}
    real_replace = rg.replace_with_retry
    real_publish = tat.publish_revocation_gen
    real_clear = tat._state.clear_all

    def _replace(src: Path, dst: Path) -> None:
        ran_on["write"] = threading.get_ident()
        real_replace(src, dst)

    def _publish(new_gen: int) -> bool:
        ran_on["publish"] = threading.get_ident()
        return real_publish(new_gen)

    def _clear() -> None:
        ran_on["clear"] = threading.get_ident()
        real_clear()

    monkeypatch.setattr(rg, "_gen", 4)
    monkeypatch.setattr(rg, "replace_with_retry", _replace)
    monkeypatch.setattr("kiro_crew.dashboard.token_auth.publish_revocation_gen", _publish)
    monkeypatch.setattr(tat._state, "clear_all", _clear)

    await finished(tat.revoke_all_sessions_async(), "the logout")

    loop_thread = threading.get_ident()
    assert ran_on["write"] != loop_thread
    assert ran_on["publish"] == loop_thread
    assert ran_on["clear"] == loop_thread
    assert rg.current_revocation_gen_or_none() == 5


@pytest.mark.asyncio
async def test_a_superseded_logout_keeps_the_pins_bound_after_the_newer_one(
    monkeypatch,
) -> None:
    """A logout that resumes after a newer one published leaves the newer state alone.

    Two awaited logouts can resume out of order: the one that wrote 5 can reach the
    loop after the one that wrote 6 has published and cleared. A session minted at 6
    in between still validates, and ``check_peer`` accepts a token with no binding,
    so clearing its pin then would let that cookie be replayed from any address.
    """
    first_in = threading.Event()
    release_first = threading.Event()
    gens = iter([5, 6])
    gens_lock = threading.Lock()

    def _persist() -> int:
        with gens_lock:
            new_gen = next(gens)
        if new_gen == 5:
            first_in.set()
            release_first.wait(default_timeout())
        return new_gen

    monkeypatch.setattr(rg, "_gen", 4)
    monkeypatch.setattr(tat, "persist_next_revocation_gen", _persist)

    older = asyncio.create_task(tat.revoke_all_sessions_async())
    try:
        await async_wait_until(first_in.is_set, describe=lambda: "the first write never began")
        await finished(tat.revoke_all_sessions_async(), "the newer logout")
        assert rg.current_revocation_gen_or_none() == 6

        token = _session_token("owner")
        tat.bind_token_peer(token, "ip:1.1.1.1")
    finally:
        release_first.set()
        await await_until(older, what="the superseded logout")

    assert rg.current_revocation_gen_or_none() == 6
    assert tat.validate_token(token, use_session_exp=True)[0] is True
    assert tat.check_token_peer(token, "ip:2.2.2.2") == (False, "IP mismatch")


def test_a_bump_builds_on_a_value_persisted_but_not_yet_published(
    tmp_path: Path, monkeypatch
) -> None:
    """A logout's write never repeats the value an overlapping logout already wrote.

    ``revoke_all_sessions_async`` publishes only after its write lands, so a second
    logout whose write starts in between still reads the old live value. Building on
    it would write and publish the same value again, and a session minted between the
    two publishes would survive the second logout with its pin cleared.
    """
    monkeypatch.setattr(rg, "_gen", 4)

    assert rg.persist_next_revocation_gen() == 5
    assert rg.persist_next_revocation_gen() == 6
    assert rg.current_revocation_gen_or_none() == 4
    assert (tmp_path / rg._REVOCATION_FILE).read_text(encoding="utf-8") == "6"


def test_a_bump_never_writes_below_its_own_unpublished_write_when_the_file_cannot_be_read(
    tmp_path: Path, monkeypatch
) -> None:
    """A bump whose read of the counter file fails still builds on what this process wrote.

    Two overlapping logouts can have written 5 and 6 while 4 is still live. A third
    whose read of the file fails, and that built on the live 4, would write 5 over
    the 6: a session minted at 5 between the first two publishes is rejected at 6,
    then authenticates again after a restart reloads 5.
    """
    monkeypatch.setattr(rg, "_gen", 4)
    assert rg.persist_next_revocation_gen() == 5
    assert rg.persist_next_revocation_gen() == 6
    assert rg.current_revocation_gen_or_none() == 4

    monkeypatch.setattr(rg, "_load_revocation_gen_or_none", lambda: None)

    assert rg.persist_next_revocation_gen() == 7
    assert (tmp_path / rg._REVOCATION_FILE).read_text(encoding="utf-8") == "7"


@pytest.mark.asyncio
async def test_a_failed_logout_write_raises_and_changes_nothing(monkeypatch) -> None:
    """A logout whose counter write fails reports it, and leaves the generation and pins.

    ``api_logout`` turns the ``OSError`` into its 500, so the async form must let it
    through, and the live value must stay the old one: a generation that is not on
    disk would be reloaded lower after a restart. The old generation still accepts a
    pre-logout cookie, and ``check_peer`` accepts a token with no binding, so clearing
    its pin would let that cookie be replayed from any address. The sync form holds to
    the same rule.
    """

    def _fail(src: Path, dst: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rg, "_gen", 4)
    monkeypatch.setattr(rg, "replace_with_retry", _fail)
    token = _session_token("owner")
    tat.bind_token_peer(token, "ip:1.1.1.1")

    with pytest.raises(OSError, match="disk full"):
        await tat.revoke_all_sessions_async()
    assert rg.current_revocation_gen_or_none() == 4
    assert tat.check_token_peer(token, "ip:2.2.2.2") == (False, "IP mismatch")

    with pytest.raises(OSError, match="disk full"):
        tat.revoke_all_sessions()
    assert rg.current_revocation_gen_or_none() == 4
    assert tat.check_token_peer(token, "ip:2.2.2.2") == (False, "IP mismatch")
