"""PoolKey.from_register security-boundary validation.

``os_uid`` is the one security dimension of the PoolKey. It must be
type-checked, not coerced: ``int`` on a bool silently passes, so a stub
sending a JSON number or bool could land in the wrong uid partition and share
a backend it should not.

The approval and sandbox fields a stub also reports (``sandbox_mode``,
``autoapprove_set_hash``, ``approval_mode``, ``trust_all_tools``) are NOT
dimensions. ``TestApprovalAndSandboxAreNotPoolDimensions`` below is the pin on
that, with the reasoning in the ``pool`` module docstring.
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
    "channel_id": None,
    "config_snapshot_hash": "h4",
}


def test_pool_key_field_set_is_exactly_the_six_dimensions() -> None:
    """The key's field set is asserted EXPLICITLY so adding or removing a
    pool dimension has to be a deliberate test change, never a silent one.

    Intentionally absent. ``user_identity``: nothing populates its
    ``KIROCREW_PRINCIPAL`` source, so it always collapses to the OS user and
    never isolates anything -- adding it must come with a real multi-principal
    design, not just a field. The four approval/sandbox fields
    (``sandbox_mode``, ``autoapprove_set_hash``, ``approval_mode``,
    ``trust_all_tools``): absent for the reasons in the ``pool`` module
    docstring. ``agent_name``: the agent never reaches the backend process, so
    two agents declaring one server identically asked for the same process.
    ``config_snapshot_hash``: the stub always sent 64 zeros, so it partitioned
    nothing at all.
    """
    assert set(PoolKey.__dataclass_fields__) == {
        # identity
        "server_name",
        # execution shape
        "command_args_hash",
        "effective_env_hash",
        "work_dir",
        "binary_version",
        # security boundary
        "os_uid",
    }


def test_valid_register_roundtrips() -> None:
    key = PoolKey.from_register(dict(_VALID))
    assert key.os_uid == 1000
    assert key.server_name == "slack-mcp"


def test_bool_os_uid_is_rejected() -> None:
    # isinstance(True, int) is True; a bool must not pass as a uid.
    with pytest.raises(ValueError, match="os_uid must be int"):
        PoolKey.from_register({**_VALID, "os_uid": True})


def test_string_os_uid_is_rejected_not_coerced() -> None:
    with pytest.raises(ValueError, match="os_uid must be int"):
        PoolKey.from_register({**_VALID, "os_uid": "1000"})


class TestApprovalAndSandboxAreNotPoolDimensions:
    """The four fields labeled "security boundary" do not partition the pool.

    None of them changes how a pooled backend behaves:

    * ``gatewayd`` spawns backends outside any mount namespace, so two
      sessions configured for different sandbox tiers are confined
      identically — splitting them buys a second unsandboxed process.
    * kiro-cli decides tool visibility and approval per agent, against that
      agent's own overlay entry, BEFORE a ``tools/call`` reaches the stub. A
      backend never reads these values, so it cannot act on them.

    The fields are still accepted on a register payload and ignored, the same
    wire-compat treatment ``user_identity`` and ``channel_id`` get.
    """

    _FIELDS = (
        ("sandbox_mode", "none"),
        ("autoapprove_set_hash", "a-completely-different-hash"),
        ("approval_mode", "yolo"),
        ("trust_all_tools", True),
    )

    def test_each_field_alone_shares_one_backend(self) -> None:
        base = PoolKey.from_register(dict(_VALID))
        for field, other in self._FIELDS:
            variant = PoolKey.from_register({**_VALID, field: other})
            assert variant.stable_hash() == base.stable_hash(), field
            assert variant == base, field

    def test_all_four_differing_together_share_one_backend(self) -> None:
        """One agent on one server, whose approval posture and sandbox tier
        both change -- every other dimension held equal, including
        ``agent_name``, which partitions on its own."""
        permissive = PoolKey.from_register(
            {
                **_VALID,
                "agent_name": "agent-a",
                "sandbox_mode": "off",
                "autoapprove_set_hash": "wide-open",
                "approval_mode": "yolo",
                "trust_all_tools": True,
            }
        )
        strict = PoolKey.from_register(
            {
                **_VALID,
                "agent_name": "agent-a",
                "sandbox_mode": "strict",
                "autoapprove_set_hash": "nothing-approved",
                "approval_mode": "interactive",
                "trust_all_tools": False,
            }
        )
        assert permissive.stable_hash() == strict.stable_hash()

    def test_payload_omitting_all_four_is_accepted(self) -> None:
        """They are not fields at all — not special-cased optional ones — so a
        current stub's payload is complete rather than tolerated."""
        payload = {k: v for k, v in _VALID.items() if k not in {f for f, _ in self._FIELDS}}
        key = PoolKey.from_register(payload)
        assert key.stable_hash() == PoolKey.from_register(dict(_VALID)).stable_hash()

    def test_malformed_values_do_not_break_register(self) -> None:
        """An older stub still reports all four, and a value of any shape must
        not fail a register that does not read it. A string ``trust_all_tools``
        is the pointed case: type-checking it guarded against ``bool("false")``
        keying a session as trusted, and with the field out of the key there is
        no trust partition left for any value to land in."""
        base = PoolKey.from_register(dict(_VALID))
        for field, _ in self._FIELDS:
            for bogus in ("false", 123, {"a": 1}, ["x"], None, ""):
                variant = PoolKey.from_register({**_VALID, field: bogus})
                assert variant.stable_hash() == base.stable_hash(), (field, bogus)

    def test_absent_from_repr(self) -> None:
        text = str(PoolKey.from_register(dict(_VALID)))
        for field, _ in self._FIELDS:
            assert field not in text, field

    def test_absent_from_the_log_label(self) -> None:
        """The stub and the daemon match pool identities by eye off this
        label, so it names what actually partitions the pool."""
        label = PoolKey.from_register(dict(_VALID)).human_readable()
        assert "sbx=" not in label
        assert "uid=1000" in label

    def test_os_uid_still_partitions(self) -> None:
        """Negative control: the one real security dimension still splits, so
        dropping the four has not made the key permissive."""
        base = PoolKey.from_register(dict(_VALID))
        variant = PoolKey.from_register({**_VALID, "os_uid": 1001})
        assert variant.stable_hash() != base.stable_hash()


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
        """It is not a field at all — not a special-cased optional one — so a
        payload omitting it is complete rather than tolerated."""
        payload = {k: v for k, v in _VALID.items() if k != "channel_id"}
        key = PoolKey.from_register(payload)
        assert key.stable_hash() == PoolKey.from_register(dict(_VALID)).stable_hash()

    def test_unknown_channel_id_shape_does_not_break_register(self) -> None:
        """Forward/backward compat: an older stub still reports ``channel_id``
        (gatewayd threads it into caller identity), and a malformed value must
        not fail a register that does not depend on it."""
        for bogus in (123, {"a": 1}, ["x"], ""):
            key = PoolKey.from_register({**_VALID, "channel_id": bogus})
            assert key.stable_hash() == PoolKey.from_register(dict(_VALID)).stable_hash()

    def test_channel_absent_from_repr(self) -> None:
        assert "chan=" not in str(PoolKey.from_register({**_VALID, "channel_id": "C_X"}))

    def test_legacy_user_identity_is_not_a_pool_dimension(self) -> None:
        """An older stub still sends ``user_identity`` in its register
        payload. The field was deleted from the key (it never isolated
        anything — nothing populated ``KIROCREW_PRINCIPAL``, so it always
        collapsed to the OS user), so the payload key must be ignored, not
        rejected, and must not partition the pool."""
        base = PoolKey.from_register(dict(_VALID))
        for legacy in ("someone-else", "", "unknown"):
            variant = PoolKey.from_register({**_VALID, "user_identity": legacy})
            assert variant.stable_hash() == base.stable_hash()
            assert variant == base

    def test_security_dimensions_still_partition(self) -> None:
        """Negative control: dropping the channel dimension must not have made
        the key permissive — the real boundaries still split."""
        base = PoolKey.from_register(dict(_VALID))
        for field, other in (
            ("os_uid", 1001),
            ("binary_version", "2.0"),
            ("effective_env_hash", "different"),
            ("work_dir", "/tmp/other"),
        ):
            variant = PoolKey.from_register({**_VALID, field: other})
            assert variant.stable_hash() != base.stable_hash(), field


class TestAgentIsNotAPoolDimension:
    """Two agents with identical server config share ONE backend.

    The agent name never reaches the backend process. Every input that does --
    command, env, work dir, binary -- is its own dimension and already differs
    whenever two agents declare a server differently, so an agent dimension only
    ever duplicated processes: on a host running several agents a common server
    ran up to five indistinguishable backends.
    """

    def test_two_agents_with_identical_config_share_one_backend(self) -> None:
        a = PoolKey.from_register({**_VALID, "agent_name": "gpu-dev"})
        b = PoolKey.from_register({**_VALID, "agent_name": "kirocrew"})
        assert a.stable_hash() == b.stable_hash()
        assert a == b

    def test_payload_without_agent_name_is_accepted(self) -> None:
        """Not a field at all, so a payload omitting it is complete."""
        payload = {k: v for k, v in _VALID.items() if k != "agent_name"}
        assert (
            PoolKey.from_register(payload).stable_hash()
            == PoolKey.from_register(dict(_VALID)).stable_hash()
        )

    def test_agent_absent_from_the_log_label(self) -> None:
        """``human_readable`` labels a POOL IDENTITY. Keeping the agent in it
        would read as though the agent still split the pool -- on the very line
        an operator uses to check that."""
        assert "agent-a" not in PoolKey.from_register(dict(_VALID)).human_readable()

    def test_a_declared_difference_still_partitions(self) -> None:
        """Negative control: agents whose declarations DIFFER must still get
        their own backends. That is the whole basis for sharing when they agree,
        so it is the same test read from the other side."""
        base = PoolKey.from_register({**_VALID, "agent_name": "gpu-dev"})
        # Every REMAINING dimension, so the control cannot quietly shrink to a
        # subset if another field leaves the key later.
        for field, other in (
            ("server_name", "other-mcp"),
            ("command_args_hash", "other-cmd"),
            ("effective_env_hash", "other-env"),
            ("work_dir", "/tmp/other"),
            ("binary_version", "2.0"),
            ("os_uid", 1001),
        ):
            variant = PoolKey.from_register({**_VALID, "agent_name": "kirocrew", field: other})
            assert variant.stable_hash() != base.stable_hash(), field


class TestConfigSnapshotIsNotAPoolDimension:
    """``config_snapshot_hash`` was inert from the start.

    The stub always sent a constant run of 64 zeros for it, so it never
    partitioned anything. Real config drift is carried by the execution-shape
    fields, which are recomputed from the spec the stub was launched with.
    """

    def test_differing_snapshot_hashes_share_one_backend(self) -> None:
        a = PoolKey.from_register({**_VALID, "config_snapshot_hash": "0" * 64})
        b = PoolKey.from_register({**_VALID, "config_snapshot_hash": "f" * 64})
        assert a.stable_hash() == b.stable_hash()
        assert a == b

    def test_payload_without_config_snapshot_hash_is_accepted(self) -> None:
        payload = {k: v for k, v in _VALID.items() if k != "config_snapshot_hash"}
        assert (
            PoolKey.from_register(payload).stable_hash()
            == PoolKey.from_register(dict(_VALID)).stable_hash()
        )


def test_an_old_stubs_full_payload_still_registers() -> None:
    """Wire compat. A stub predating these removals sends every legacy key at
    once; the daemon must ignore them rather than reject the register, or an
    upgraded daemon would un-pool every stub still on the old build."""
    legacy = {
        **_VALID,
        "user_identity": "mingweic",
        "agent_name": "gpu-dev",
        "config_snapshot_hash": "0" * 64,
        "channel_id": "C_AAA",
        "poolable": True,
    }
    assert (
        PoolKey.from_register(legacy).stable_hash()
        == PoolKey.from_register(dict(_VALID)).stable_hash()
    )
