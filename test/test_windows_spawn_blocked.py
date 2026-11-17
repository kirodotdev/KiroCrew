"""A Windows spawn a host policy refused is one clear, non-retryable error (P1-10).

AppLocker, an EDR product or group policy answers ``CreateProcess`` with
``winerror`` 5 or 1260. After main's one retry, such a spawn becomes a
non-transient error and the chat card names the file in the reader's language.
Every other spawn error is unchanged.
"""

from __future__ import annotations

import functools

import pytest

import kiro_crew.acp.runtime as runtime_mod
from kiro_crew.acp.spawn_blocked import AcpSpawnBlocked, is_windows_spawn_blocked
from kiro_crew.dashboard.chat_turn.recovery import _terminal_error_meta

_KAS = r"C:\Users\me\AppData\Local\Kiro\kas.exe"


def _win_error(winerror: int | None, text: str) -> OSError:
    exc = PermissionError(13, text)
    if winerror is not None:
        # The attribute CPython sets from the Windows error; settable on any host.
        exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


@pytest.mark.parametrize("code", [5, 1260])
def test_a_policy_code_is_a_blocked_spawn(code):
    assert is_windows_spawn_blocked(_win_error(code, "anything"))


def test_another_code_is_not_blocked_even_with_the_policy_wording():
    # The code decides when present; the text is only a fallback.
    assert not is_windows_spawn_blocked(_win_error(2, "Access is denied"))


def test_no_code_is_never_blocked_whatever_the_wording():
    # Only the winerror code decides; message text is never read.
    assert not is_windows_spawn_blocked(_win_error(None, "Access is denied"))
    assert not is_windows_spawn_blocked(
        _win_error(None, "This program is blocked by group policy.")
    )
    assert not is_windows_spawn_blocked(ValueError("Access is denied"))


def _factory(*errors: BaseException):
    calls: list[tuple] = []

    async def create(*argv, **kwargs):
        calls.append(argv)
        raise errors[min(len(calls), len(errors)) - 1]

    return functools.partial(create, _KAS, "acp"), calls


@pytest.fixture
def no_backoff(monkeypatch):
    # Through the facade: the backoff constant is bound by more than one module
    # of the runtime family, and the facade's patch reaches every holder.
    monkeypatch.setattr(runtime_mod, "_ACP_RUNTIME_RESPAWN_BACKOFF_S", 0)


@pytest.mark.asyncio
async def test_a_spawn_still_blocked_after_the_retry_names_the_binary(no_backoff):
    blocked = _win_error(1260, "blocked")
    factory, calls = _factory(blocked, blocked)
    with pytest.raises(AcpSpawnBlocked) as raised:
        await runtime_mod._retrying_spawn_factory(factory)
    assert len(calls) == 2  # main's one retry still runs first
    assert raised.value.binary == _KAS
    assert raised.value.transient is False
    assert _KAS in str(raised.value) and "AppLocker" in str(raised.value)


@pytest.mark.asyncio
async def test_access_denied_inside_the_update_window_still_recovers(no_backoff):
    # A spawn that lands while the CLI replaces its executable can fail with
    # access denied too; the retry, not the classifier, answers the first one.
    process = object()
    calls: list[int] = []

    async def create(*argv, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise _win_error(5, "Access is denied")
        return process

    factory = functools.partial(create, _KAS, "acp")
    assert await runtime_mod._retrying_spawn_factory(factory) is process
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_any_other_spawn_error_keeps_mains_one_retry(no_backoff):
    other = _win_error(32, "The process cannot access the file")
    factory, calls = _factory(other, other)
    with pytest.raises(OSError) as raised:
        await runtime_mod._retrying_spawn_factory(factory)
    assert len(calls) == 2
    assert raised.value is other


def test_the_card_carries_the_token_and_the_file():
    meta = _terminal_error_meta(AcpSpawnBlocked(_KAS))
    assert meta == {"notice": "windows_spawn_blocked", "notice_args": {"binary": _KAS}}
