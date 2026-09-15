"""Contract between a channel's ``boot_keys`` and the schema's ``restart=True`` marks.

Two places declare "this channel field cannot be adopted by the running
transport", and nothing in the code ties them together:

* the RUNTIME path -- each :class:`~kiro_crew.messaging.registry.ChannelDescriptor`
  in ``kiro_crew.channels`` declares ``boot_keys``, which
  ``registry.changed_boot_keys`` reads to decide an IN-PROCESS channel restart
  (``GatewayOrchestrator._on_channel_config_change`` -> ``restart_channel``);
* the UI path -- ``dashboard.channel_folders.channel_restart_required`` answers a
  channel save's ``restart_required`` from ``config.schema.requires_restart``, the
  ``restart=True`` marks in ``config/sections.py``.

They are DELIBERATELY different sets, because they mean different things
(docs/system-specs/modules/messaging.md, "Connection fields restart ONE channel,
in process" and "``restart_required`` is answered from schema metadata";
docs/system-specs/modules/config.md, "``restart=True`` is the single source of
restart truth"):

* a ``boot_key`` is a CONNECTION parameter -- the enable flag, the token, the
  endpoint, the store -- that the gateway applies by reconnecting that one channel.
  From the operator's seat it is hot: the settings page must NOT promise a gateway
  restart for it, or the panel trains people to restart for everything;
* a ``restart=True`` channel field is one even a channel reconnect cannot adopt, so
  the UI promises a PROCESS restart. The documented exceptions are the whole list:
  ``slack.command`` (registered with Slack's app manifest) and ``whatsapp.db_path``
  (the device store the ``neonize`` client opens once).

So the relationship pinned here is disjointness plus each side's well-formedness,
per channel so a failure names the channel:

1. every boot key names a field the schema knows under ``<channel>.`` -- a
   changed path is matched on its first segment under the section, so a key the
   schema never emits can never appear in a diff and the channel would silently
   never reconnect for it;
2. no boot key is restart-marked, and ``channel_restart_required`` says so over the
   whole set -- the UI answer for a connection-field save is "applied";
3. the restart-marked leaves under a channel section lie outside ``boot_keys`` AND
   equal the documented list above, in both directions -- a new mark on a channel
   field extends that list in the doc first, and a dropped mark means the field
   now needs an applier;
4. the section container itself is never marked -- the restart applier subscribes
   to the bare section name (``live.subscribe("whatsapp", ...)``), which
   ``config.live._refuse_restart_marked`` would refuse at construction if the
   section were marked, and a section mark would also turn every live field of
   the channel into a promised restart.

Credentials in ``.env`` are outside both sets on purpose: the watcher does not
read that file, so an ``env_updates`` save reports a restart regardless of the
schema. That branch is pinned in ``test_channel_boot_hot_reload.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.channels import builtin_channel_descriptors
from kiro_crew.config import schema
from kiro_crew.dashboard.channel_folders import channel_restart_required
from kiro_crew.messaging import registry
from kiro_crew.messaging.registry import ChannelDescriptor

#: The channel fields the docs mark ``restart=True`` -- messaging.md,
#: "``restart_required`` is answered from schema metadata": "The exceptions, and
#: they are the whole list." A channel absent here has NO restart-marked field.
GATEWAY_RESTART_LEAVES: dict[str, frozenset[str]] = {
    "slack": frozenset({"slack.command"}),
    "whatsapp": frozenset({"whatsapp.db_path"}),
}

DESCRIPTORS: tuple[ChannelDescriptor, ...] = builtin_channel_descriptors()

_PER_CHANNEL = pytest.mark.parametrize(
    "desc", DESCRIPTORS, ids=[d.channel_type for d in DESCRIPTORS]
)


def _section_paths(channel: str) -> frozenset[str]:
    """Every schema path under ``<channel>.``, containers and leaves alike."""
    prefix = channel + "."
    return frozenset(e.path for e in schema.SCHEMA_REGISTRY if e.path.startswith(prefix))


def _first_segment(channel: str, path: str) -> str:
    """The key ``changed_boot_keys`` would match *path* on: its first segment
    under the section (``telegram.accounts.main.token`` -> ``accounts``)."""
    return path[len(channel) + 1 :].split(".", 1)[0]


def _restart_marked_under(channel: str) -> frozenset[str]:
    """The schema paths under ``<channel>.`` that ``requires_restart`` answers True
    for. Resolved through :func:`schema.requires_restart` rather than read off
    the entries so a mark on a CONTAINER (the section, a dict field) shows up on
    everything beneath it, exactly as the UI would see it."""
    return frozenset(p for p in _section_paths(channel) if schema.requires_restart(p))


def test_the_documented_exception_table_names_only_registered_channels() -> None:
    """Guards this file's own data: a typo'd channel key here would otherwise be
    a documented exception nothing compares against."""
    assert set(GATEWAY_RESTART_LEAVES) <= set(registry.governed_members(DESCRIPTORS))


class TestBootKeysAreWellFormed:
    @_PER_CHANNEL
    def test_every_boot_key_is_a_field_the_schema_knows_under_the_section(
        self, desc: ChannelDescriptor
    ) -> None:
        """``changed_boot_keys`` matches a changed path on its first segment under
        the section, so a key the schema does not emit can never be matched: the
        channel would silently never reconnect for it."""
        known = {_first_segment(desc.channel_type, p) for p in _section_paths(desc.channel_type)}
        unknown = sorted(desc.boot_keys - known)
        assert not unknown, (
            f"{desc.channel_type}: boot_keys name no field of the {desc.channel_type!r} "
            f"config section: {unknown} (known: {sorted(known)})"
        )

    @_PER_CHANNEL
    def test_a_bootable_channel_gates_on_enabled_and_a_host_managed_one_has_no_boot_keys(
        self, desc: ChannelDescriptor
    ) -> None:
        """``boot_keys`` is 'the enable flag, the token, the endpoint, the store'
        (``ChannelDescriptor.boot_keys``): every channel the registry boots must
        reconnect on ``enabled``, or switching it off leaves the transport up.
        Slack's lifecycle is host-managed, so its set is empty by design."""
        if desc.start is None:
            assert desc.boot_keys == frozenset(), (
                f"{desc.channel_type}: host-managed lifecycle, but boot_keys="
                f"{sorted(desc.boot_keys)} -- the registry restart loop skips it"
            )
        else:
            assert "enabled" in desc.boot_keys, (
                f"{desc.channel_type}: bootable, but `enabled` is not a boot key, so "
                "disabling the channel would not stop its transport"
            )


class TestTheTwoDeclarationsAreDisjoint:
    @_PER_CHANNEL
    def test_no_boot_key_is_restart_marked(self, desc: ChannelDescriptor) -> None:
        """A connection field is applied by reconnecting the channel in process, so
        the settings page must answer "applied", not "restart the gateway"."""
        channel = desc.channel_type
        marked = sorted(k for k in desc.boot_keys if schema.requires_restart(f"{channel}.{k}"))
        assert not marked, (
            f"{channel}: boot_keys {marked} are marked restart=True in config/sections.py, "
            "but restart_channel already applies them in process -- drop the mark, or "
            "drop the key from boot_keys if the transport really cannot adopt it"
        )
        # The UI's own consumer, over the whole set at once -- the shape of a save
        # that touches every connection field of the channel.
        assert channel_restart_required(channel, sorted(desc.boot_keys)) is False

    @_PER_CHANNEL
    def test_restart_marked_leaves_are_exactly_the_documented_ones_and_outside_boot_keys(
        self, desc: ChannelDescriptor
    ) -> None:
        """Both directions: a mark the docs do not list is a new boot-only field
        (extend messaging.md's "exceptions" list and this table together); a listed
        field that lost its mark now needs an applier, and must not silently start
        promising "applied"."""
        channel = desc.channel_type
        marked = _restart_marked_under(channel)
        expected = GATEWAY_RESTART_LEAVES.get(channel, frozenset())
        assert marked == expected, (
            f"{channel}: restart=True fields in the schema are {sorted(marked)}, the "
            f"documented exceptions are {sorted(expected)} -- update "
            "docs/system-specs/modules/messaging.md and GATEWAY_RESTART_LEAVES with the "
            "sections.py change, or revert the mark"
        )
        overlap = sorted(
            _first_segment(channel, p)
            for p in marked
            if _first_segment(channel, p) in desc.boot_keys
        )
        assert not overlap, (
            f"{channel}: {overlap} are both boot_keys (reconnect in process) and "
            "restart=True (needs a gateway restart) -- one of the two is wrong"
        )
        for path in sorted(marked):
            field = path[len(channel) + 1 :]
            assert channel_restart_required(channel, [field]) is True, path

    @_PER_CHANNEL
    def test_the_section_container_itself_is_never_marked(self, desc: ChannelDescriptor) -> None:
        """The restart applier registers on the bare section name; a section-level
        mark would make that registration raise at construction and would turn
        every live field of the channel into a promised gateway restart."""
        assert schema.requires_restart(desc.channel_type) is False, (
            f"{desc.channel_type}: the whole section is marked restart=True, so every "
            "field beneath it -- allow-lists and thresholds included -- promises a restart"
        )
