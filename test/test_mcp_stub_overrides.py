"""``stub_servers`` is a roster; ``stub_overrides`` is what the operator changed.

Splitting them is what makes the stub set shippable. An edition that wants its
known servers stubbed out of the box puts them in the roster and keeps adding to
it; the operator turns any single one off without restating the survivors. A flat
resulting list cannot do both: unstubbing one name out of a shipped roster means
writing back the ones that remain, and that written list then answers the
question forever — so the next name the edition adds never arrives.

The decisive property is that SILENCE FOLLOWS THE ROSTER. An override exists only
for a server the operator actually spoke about, which is why one that agrees with
the roster is pruned rather than stored: identical in effect today, and the
difference tomorrow is whether a roster change reaches that server at all.
"""

import json
import unittest.mock
from pathlib import Path

from kiro_crew.config.loader import (
    KiroCrewConfig,
    _resolve_stub_overrides,
    _resolve_stub_roster,
    _resolve_stub_servers,
)


def _load_from_dict(data: object, tmp_path: Path) -> KiroCrewConfig:
    """Write *data* into the test's own tmp_path and load through the real loader.

    Kept under ``tmp_path`` rather than the shared temp dir so concurrent workers
    cannot see each other's config.
    """
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(data), encoding="utf-8")
    with unittest.mock.patch(
        "kiro_crew.config.loader.config_path",
        return_value=cfg,
    ):
        return KiroCrewConfig.load()


class TestTheRosterIsFollowedWhenTheOperatorIsSilent:
    """No overrides means the roster is the answer, unchanged."""

    def test_a_roster_with_no_overrides_resolves_to_itself(self) -> None:
        assert _resolve_stub_servers({"stub_servers": ["a-mcp", "b-mcp"]}) == [
            "a-mcp",
            "b-mcp",
        ]

    def test_an_empty_override_map_is_the_same_as_none(self) -> None:
        """``{}`` and absent are one statement: "I have not spoken about any"."""
        assert _resolve_stub_servers({"stub_servers": ["a-mcp"], "stub_overrides": {}}) == ["a-mcp"]

    def test_roster_order_survives(self) -> None:
        """The resolver has always handed back the file's own order; normalizing
        is the write path's job, so a read must not reorder."""
        assert _resolve_stub_servers({"stub_servers": ["z-mcp", "a-mcp"]}) == [
            "z-mcp",
            "a-mcp",
        ]


class TestOverridesDeviateFromTheRoster:
    def test_false_removes_a_rostered_server(self) -> None:
        assert _resolve_stub_servers(
            {"stub_servers": ["a-mcp", "b-mcp"], "stub_overrides": {"a-mcp": False}}
        ) == ["b-mcp"]

    def test_true_adds_a_server_the_roster_omits(self) -> None:
        assert _resolve_stub_servers(
            {"stub_servers": ["a-mcp"], "stub_overrides": {"z-mcp": True}}
        ) == ["a-mcp", "z-mcp"]

    def test_true_for_an_already_rostered_server_does_not_duplicate_it(self) -> None:
        assert _resolve_stub_servers(
            {"stub_servers": ["a-mcp"], "stub_overrides": {"a-mcp": True}}
        ) == ["a-mcp"]

    def test_added_servers_are_appended_in_a_stable_order(self) -> None:
        """They have no position in the roster to preserve, so the order must come
        from somewhere deterministic or two identical configs resolve differently."""
        assert _resolve_stub_servers(
            {"stub_servers": ["m-mcp"], "stub_overrides": {"z-mcp": True, "a-mcp": True}}
        ) == ["m-mcp", "a-mcp", "z-mcp"]

    def test_an_override_naming_an_unrostered_server_false_is_inert(self) -> None:
        """Turning off something that was never on changes nothing — it must not
        remove an unrelated entry or fail."""
        assert _resolve_stub_servers(
            {"stub_servers": ["a-mcp"], "stub_overrides": {"ghost-mcp": False}}
        ) == ["a-mcp"]


class TestTheRosterCanGrowUnderADeviation:
    """The property the whole split exists for.

    An operator who turned ONE server off must still receive the next server the
    edition adds. Under a flat list this is the case that fails: their written
    list has no opinion to distinguish "I do not want b-mcp" from "I have never
    heard of c-mcp", so it silently answers no to both.
    """

    def test_a_new_roster_entry_reaches_an_operator_who_deviated_elsewhere(
        self,
    ) -> None:
        shipped = {
            "stub_servers": ["a-mcp", "b-mcp"],
            "stub_overrides": {"b-mcp": False},
        }
        assert _resolve_stub_servers(shipped) == ["a-mcp"]

        # The edition ships a third server. Only the roster changes.
        shipped["stub_servers"] = ["a-mcp", "b-mcp", "c-mcp"]
        assert _resolve_stub_servers(shipped) == ["a-mcp", "c-mcp"], (
            "a roster addition did not reach an operator who had deviated on a "
            "DIFFERENT server -- their one decision is shadowing the whole roster, "
            "which is the failure the override map exists to prevent"
        )

    def test_the_deviation_itself_survives_the_roster_change(self) -> None:
        """The other half: growing the roster must not quietly re-enable what the
        operator turned off."""
        shipped = {
            "stub_servers": ["a-mcp", "b-mcp", "c-mcp"],
            "stub_overrides": {"b-mcp": False},
        }
        assert "b-mcp" not in _resolve_stub_servers(shipped)


class TestJunkIsDroppedRatherThanGuessed:
    def test_a_non_bool_value_is_ignored(self) -> None:
        """A truthy string is an operator's typo. Coercing it would decide a
        server's topology from a guess; leaving it drops to the roster's answer."""
        assert _resolve_stub_overrides({"stub_overrides": {"a-mcp": "yes"}}) == {}
        assert _resolve_stub_servers(
            {"stub_servers": ["a-mcp"], "stub_overrides": {"a-mcp": "no"}}
        ) == ["a-mcp"]

    def test_a_blank_or_non_string_name_is_dropped(self) -> None:
        assert _resolve_stub_overrides({"stub_overrides": {"": True, "ok-mcp": True}}) == {
            "ok-mcp": True
        }

    def test_a_non_dict_value_is_not_trusted(self) -> None:
        assert _resolve_stub_overrides({"stub_overrides": ["a-mcp"]}) == {}
        assert _resolve_stub_servers({"stub_servers": ["a-mcp"], "stub_overrides": "nonsense"}) == [
            "a-mcp"
        ]


class TestTheRosterResolverIgnoresOverrides:
    """``_resolve_stub_roster`` answers "what does the roster say", which is what
    the write path compares a click against. Folding overrides in here would make
    a deviation indistinguishable from a shipped name."""

    def test_the_roster_resolver_does_not_apply_overrides(self) -> None:
        data = {"stub_servers": ["a-mcp"], "stub_overrides": {"a-mcp": False, "z-mcp": True}}
        assert _resolve_stub_roster(data) == ["a-mcp"]
        assert _resolve_stub_servers(data) == ["z-mcp"]

    def test_the_roster_resolver_still_honours_the_legacy_alias(self) -> None:
        """The migration lives on this layer, so it must survive the split."""
        assert _resolve_stub_roster({"enabled": True, "poolable_servers": ["legacy-mcp"]}) == [
            "legacy-mcp"
        ]


class TestSaveRoundTripPreservesTheRoster:
    """``save()`` must not flatten the roster to the effective set.

    ``stub_servers`` is the roster in the FILE but the effective set on the
    dataclass, and ``save()`` round-trips the dataclass through ``asdict``. Emitting
    the effective set would rewrite the file without the servers the operator opted
    out of -- a reversible deviation becoming a permanent deletion from a layer that
    is not the dashboard's to edit, triggered by any unrelated ``save()`` (an agent
    create/update, a settings write).
    """

    def _round_trip(self, data: dict, tmp_path: Path) -> dict:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(data), encoding="utf-8")
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path",
            return_value=cfg_file,
        ):
            cfg = KiroCrewConfig.load()
            cfg.save()
        return json.loads(cfg_file.read_text(encoding="utf-8")).get("mcp_gateway", {})

    def test_an_opted_out_server_survives_an_unrelated_save(self, tmp_path: Path) -> None:
        saved = self._round_trip(
            {
                "mcp_gateway": {
                    "stub_servers": ["a-mcp", "b-mcp", "c-mcp"],
                    "stub_overrides": {"b-mcp": False},
                }
            },
            tmp_path,
        )
        assert saved["stub_servers"] == ["a-mcp", "b-mcp", "c-mcp"], (
            "save() rewrote the roster as the effective set, so the operator's "
            "opt-out has become a permanent deletion and a later roster addition "
            "has nothing to attach to"
        )
        assert saved["stub_overrides"] == {"b-mcp": False}
        assert _resolve_stub_servers(saved) == ["a-mcp", "c-mcp"]

    def test_the_private_carrier_is_never_written_as_a_config_key(self, tmp_path: Path) -> None:
        """It exists to survive serialization, not to become a key an operator can
        write and watch be ignored."""
        saved = self._round_trip(
            {"mcp_gateway": {"stub_servers": ["a-mcp"], "stub_overrides": {"a-mcp": False}}},
            tmp_path,
        )
        assert "_stub_roster" not in saved
        assert "stub_roster" not in saved

    def test_a_legacy_alias_install_is_not_flattened_either(self, tmp_path: Path) -> None:
        """The same landmine on the migration path: the effective set here comes from
        the deprecated key, so emitting it would silently promote the alias."""
        saved = self._round_trip(
            {"mcp_gateway": {"enabled": True, "poolable_servers": ["legacy-mcp"]}},
            tmp_path,
        )
        assert saved["stub_servers"] == ["legacy-mcp"]


class TestThroughTheLoader:
    def test_the_shipped_default_is_an_empty_map(self, tmp_path: Path) -> None:
        cfg = _load_from_dict({}, tmp_path)
        assert cfg.mcp_gateway.stub_overrides == {}

    def test_the_effective_set_and_the_decisions_both_arrive(self, tmp_path: Path) -> None:
        """``stub_servers`` on the dataclass is the EFFECTIVE set, and the map is
        carried alongside it -- a writer needs to tell a decision from a roster
        entry to know whether a new click is a deviation."""
        cfg = _load_from_dict(
            {
                "mcp_gateway": {
                    "stub_servers": ["a-mcp", "b-mcp"],
                    "stub_overrides": {"b-mcp": False, "z-mcp": True},
                }
            },
            tmp_path,
        )
        assert cfg.mcp_gateway.stub_servers == ["a-mcp", "z-mcp"]
        assert cfg.mcp_gateway.stub_overrides == {"b-mcp": False, "z-mcp": True}

    def test_a_legacy_config_gains_no_overrides_of_its_own(self, tmp_path: Path) -> None:
        cfg = _load_from_dict(
            {"mcp_gateway": {"enabled": True, "poolable_servers": ["legacy-mcp"]}},
            tmp_path,
        )
        assert cfg.mcp_gateway.stub_servers == ["legacy-mcp"]
        assert cfg.mcp_gateway.stub_overrides == {}


class TestManagedServersFollowTheControlPlane:
    """Crew's own caller-aware servers are stubbed whenever ``kirocrew-core`` is.

    Without this an install that pools the control plane still paid one Python
    process per session for ``kirocrew-guide``, which every default spec declares.
    """

    def test_followers_join_when_core_is_rostered(self) -> None:
        from kiro_crew.config.integration_sections import MANAGED_STUB_FOLLOWERS

        resolved = _resolve_stub_servers({"stub_servers": ["builder-mcp", "kirocrew-core"]})
        assert resolved[:2] == ["builder-mcp", "kirocrew-core"]
        assert set(MANAGED_STUB_FOLLOWERS) <= set(resolved)
        assert len(resolved) == len(set(resolved))

    def test_followers_join_when_core_is_added_by_override(self) -> None:
        resolved = _resolve_stub_servers(
            {"stub_servers": [], "stub_overrides": {"kirocrew-core": True}}
        )
        assert "kirocrew-guide" in resolved

    def test_nothing_follows_without_core(self) -> None:
        assert _resolve_stub_servers({"stub_servers": ["builder-mcp"]}) == ["builder-mcp"]
        assert _resolve_stub_servers({}) == []

    def test_nothing_follows_when_core_is_overridden_off(self) -> None:
        assert (
            _resolve_stub_servers(
                {"stub_servers": ["kirocrew-core"], "stub_overrides": {"kirocrew-core": False}}
            )
            == []
        )

    def test_an_operator_can_turn_one_follower_off(self) -> None:
        resolved = _resolve_stub_servers(
            {"stub_servers": ["kirocrew-core"], "stub_overrides": {"kirocrew-guide": False}}
        )
        assert "kirocrew-guide" not in resolved
        assert "kirocrew-panel" in resolved

    def test_the_computer_server_never_follows(self) -> None:
        assert "kirocrew-computer" not in _resolve_stub_servers({"stub_servers": ["kirocrew-core"]})

    def test_the_roster_layer_is_unchanged(self) -> None:
        """Writers persist the roster; the followers must never leak into it."""
        assert _resolve_stub_roster({"stub_servers": ["kirocrew-core"]}) == ["kirocrew-core"]

    def test_followers_are_every_caller_aware_server_not_excluded(self) -> None:
        """Every caller-aware managed server is either a follower or excluded by name.

        Pinned as an equality so a new caller-aware server fails here until it
        is placed in one list or the other with a reason.
        """
        from kiro_crew.config.integration_sections import (
            MANAGED_STUB_FOLLOWERS,
            MANAGED_STUB_LEADER,
        )
        from kiro_crew.mcp_caller import POOLING_REQUIRES_TENANT_NONCE
        from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
        from kiro_crew.mcp_discovery import _MANAGED_SERVERS_CALLER_AWARE

        # computer: tenant-nonce gated. cron: outside this layer's measured scope.
        excluded = {"kirocrew-computer", "kirocrew-cron"}
        assert len(MANAGED_STUB_FOLLOWERS) == len(set(MANAGED_STUB_FOLLOWERS))
        assert set(MANAGED_STUB_FOLLOWERS) == (
            _MANAGED_SERVERS_CALLER_AWARE - {MANAGED_STUB_LEADER} - excluded
        )
        assert POOLING_REQUIRES_TENANT_NONCE <= excluded
        assert set(MANAGED_STUB_FOLLOWERS) <= set(KIROCREW_BIN_MCP_SERVERS)

    def test_the_cron_server_never_follows(self) -> None:
        assert "kirocrew-cron" not in _resolve_stub_servers({"stub_servers": ["kirocrew-core"]})

    def test_a_follower_turned_on_survives_core_turned_off_later(self) -> None:
        from kiro_crew.dashboard.handlers.mcp import _record_stub_decisions

        section = {"stub_servers": ["kirocrew-core"]}
        _record_stub_decisions(section, ["kirocrew-guide"], True)
        _record_stub_decisions(section, ["kirocrew-core"], False)
        resolved = _resolve_stub_servers(section)
        assert "kirocrew-core" not in resolved
        assert "kirocrew-guide" in resolved
        assert "kirocrew-panel" not in resolved

    def test_a_follower_toggled_with_core_in_one_call_survives(self) -> None:
        from kiro_crew.dashboard.handlers.mcp import _record_stub_decisions

        section = {"stub_servers": ["kirocrew-core"]}
        _record_stub_decisions(section, ["kirocrew-core", "kirocrew-guide"], True)
        _record_stub_decisions(section, ["kirocrew-core"], False)
        assert "kirocrew-guide" in _resolve_stub_servers(section)

    def test_dashboard_stores_a_follower_turned_off(self) -> None:
        """Toggling a follower off must persist, not be pruned as "agrees"."""
        from kiro_crew.dashboard.handlers.mcp import _record_stub_decisions

        section = {"stub_servers": ["kirocrew-core"]}
        _record_stub_decisions(section, ["kirocrew-guide"], False)
        assert section["stub_servers"] == ["kirocrew-core"]
        assert section["stub_overrides"] == {"kirocrew-guide": False}
        assert "kirocrew-guide" not in _resolve_stub_servers(section)
        _record_stub_decisions(section, ["kirocrew-guide"], True)
        assert section["stub_overrides"] == {"kirocrew-guide": True}
        assert "kirocrew-guide" in _resolve_stub_servers(section)
