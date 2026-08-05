"""PoolKey.from_register security-boundary validation.

``trust_all_tools`` and ``os_uid`` are part of the security partition of the
PoolKey. They must be type-checked, not coerced: ``bool("false")`` is ``True``
and ``int`` on a bool silently passes, so a stub sending a JSON string/number
for these could land in the wrong trust/uid partition and share a backend it
should not.
"""

from __future__ import annotations

import pytest

from kiro_crew.mcp_gateway.pool import PoolKey

_VALID = {
    "server_name": "slack-mcp",
    "agent_name": "agent-a",
    "command_args_hash": "h1",
    "effective_env_hash": "h2",
    "work_dir": "/tmp/wd",
    "binary_version": "1.0",
    "os_uid": 1000,
    "sandbox_mode": "auto",
    "autoapprove_set_hash": "h3",
    "approval_mode": "interactive",
    "trust_all_tools": False,
    "user_identity": "u",
    "channel_id": None,
    "config_snapshot_hash": "h4",
}


def test_valid_register_roundtrips() -> None:
    key = PoolKey.from_register(dict(_VALID))
    assert key.os_uid == 1000
    assert key.trust_all_tools is False


def test_string_trust_all_tools_is_rejected_not_coerced() -> None:
    # bool("false") == True — coercion would wrongly key this as trusted.
    with pytest.raises(ValueError, match="trust_all_tools must be bool"):
        PoolKey.from_register({**_VALID, "trust_all_tools": "false"})


def test_bool_os_uid_is_rejected() -> None:
    # isinstance(True, int) is True; a bool must not pass as a uid.
    with pytest.raises(ValueError, match="os_uid must be int"):
        PoolKey.from_register({**_VALID, "os_uid": True})


def test_string_os_uid_is_rejected_not_coerced() -> None:
    with pytest.raises(ValueError, match="os_uid must be int"):
        PoolKey.from_register({**_VALID, "os_uid": "1000"})


class TestChannelIsNotAPoolDimension:
    """A channel does not partition the pool.

    It was never a usable trust boundary: on Slack a channel is a room shared
    by several people (so two humans in one channel shared a backend anyway),
    while on Telegram the same field carried a per-user id. The channel is
    delivered to backends PER CALL via ``_meta.kirocrew.caller`` instead, so a
    channel-aware server does not need a process to itself.
    """

    def test_two_channels_share_one_backend(self) -> None:
        a = PoolKey.from_register({**_VALID, "channel_id": "C_AAA"})
        b = PoolKey.from_register({**_VALID, "channel_id": "C_BBB"})
        assert a.stable_hash() == b.stable_hash()
        assert a == b

    def test_channel_and_no_channel_share_one_backend(self) -> None:
        with_chan = PoolKey.from_register({**_VALID, "channel_id": "C_AAA"})
        without = PoolKey.from_register({**_VALID, "channel_id": None})
        assert with_chan.stable_hash() == without.stable_hash()

    def test_payload_without_channel_id_is_accepted(self) -> None:
        """It is no longer a special-cased optional field — it is simply not a
        field, so a payload omitting it is complete rather than tolerated."""
        payload = {k: v for k, v in _VALID.items() if k != "channel_id"}
        key = PoolKey.from_register(payload)
        assert key.stable_hash() == PoolKey.from_register(dict(_VALID)).stable_hash()

    def test_unknown_channel_id_shape_does_not_break_register(self) -> None:
        """Forward/backward compat: an older stub still reports ``channel_id``
        (gatewayd threads it into caller identity), and a malformed value must
        not fail a register that no longer depends on it."""
        for bogus in (123, {"a": 1}, ["x"], ""):
            key = PoolKey.from_register({**_VALID, "channel_id": bogus})
            assert key.stable_hash() == PoolKey.from_register(dict(_VALID)).stable_hash()

    def test_channel_absent_from_repr(self) -> None:
        assert "chan=" not in str(PoolKey.from_register({**_VALID, "channel_id": "C_X"}))

    def test_security_dimensions_still_partition(self) -> None:
        """Negative control: dropping the channel dimension must not have made
        the key permissive — the real boundaries still split."""
        base = PoolKey.from_register(dict(_VALID))
        for field, other in (
            ("user_identity", "someone-else"),
            ("os_uid", 1001),
            ("sandbox_mode", "none"),
            ("effective_env_hash", "different"),
            ("work_dir", "/tmp/other"),
        ):
            variant = PoolKey.from_register({**_VALID, field: other})
            assert variant.stable_hash() != base.stable_hash(), field
