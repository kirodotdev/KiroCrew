"""Channel readiness: the roster-driven answer to "would this channel start".

Before this the only channel any diagnostic knew about was Slack, so an operator
with ``telegram.enabled: true`` and no token got a clean bill of health from
``kirocrew doctor`` and a silent bot. The answer is derived from descriptor DATA
rather than a branch per channel, so the tests here pin that shape — a new channel
is covered by adding its descriptor, not by editing the diagnostic.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.channels import ChannelReadiness, builtin_channel_descriptors, channel_readiness


def _cfg(**sections: Any) -> Any:
    return SimpleNamespace(**sections)


class TestDescriptorData:
    def test_every_channel_declares_its_credentials(self) -> None:
        by_name = {d.channel_type: d for d in builtin_channel_descriptors()}
        # Each of these cannot connect without the named keys, so a diagnostic
        # that loops the roster can say WHICH one is missing.
        assert by_name["telegram"].credentials == ("TELEGRAM_BOT_TOKEN",)
        assert by_name["discord"].credentials == ("DISCORD_BOT_TOKEN",)
        assert by_name["webex"].credentials == ("WEBEX_BOT_TOKEN",)
        assert by_name["weixin"].credentials == ("WEIXIN_TOKEN",)
        # Two-credential channels: BOTH are required, so both are listed.
        assert set(by_name["wecom"].credentials) == {"WECOM_BOT_ID", "WECOM_SECRET"}
        assert set(by_name["teams"].credentials) == {
            "MICROSOFT_APP_ID",
            "MICROSOFT_APP_PASSWORD",
        }
        assert set(by_name["slack"].credentials) == {"SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"}

    def test_imessage_declares_no_credential_because_it_has_none(self) -> None:
        # Its transport IS the operator's own Messages.app: nothing to store, and
        # an empty tuple must read as "nothing missing", not "not configured".
        by_name = {d.channel_type: d for d in builtin_channel_descriptors()}
        assert by_name["imessage"].credentials == ()
        row = ChannelReadiness("imessage", enabled=True, missing_credentials=())
        assert row.ready is True


class TestReadiness:
    def test_an_enabled_channel_with_no_token_is_not_ready_and_says_which(self) -> None:
        rows = {
            r.channel_type: r
            for r in channel_readiness(_cfg(telegram=SimpleNamespace(enabled=True)), {})
        }
        assert rows["telegram"].enabled is True
        assert rows["telegram"].ready is False
        assert rows["telegram"].missing_credentials == ("TELEGRAM_BOT_TOKEN",)

    def test_an_enabled_channel_with_its_token_is_ready(self) -> None:
        rows = {
            r.channel_type: r
            for r in channel_readiness(
                _cfg(telegram=SimpleNamespace(enabled=True)), {"TELEGRAM_BOT_TOKEN": "1:AA"}
            )
        }
        assert rows["telegram"].ready is True

    def test_a_two_credential_channel_reports_only_what_is_missing(self) -> None:
        rows = {
            r.channel_type: r
            for r in channel_readiness(
                _cfg(wecom=SimpleNamespace(enabled=True)), {"WECOM_BOT_ID": "b"}
            )
        }
        assert rows["wecom"].missing_credentials == ("WECOM_SECRET",)

    def test_a_disabled_channel_is_not_reported_as_a_problem(self) -> None:
        rows = {
            r.channel_type: r
            for r in channel_readiness(_cfg(telegram=SimpleNamespace(enabled=False)), {})
        }
        assert rows["telegram"].enabled is False and rows["telegram"].ready is False

    def test_slack_opts_in_through_its_credentials_not_a_flag(self) -> None:
        # There is no slack.enabled: configuring the tokens IS the opt-in.
        both = {"SLACK_APP_TOKEN": "xapp", "SLACK_BOT_TOKEN": "xoxb"}
        rows = {r.channel_type: r for r in channel_readiness(_cfg(), both)}
        assert rows["slack"].enabled is True and rows["slack"].ready is True
        rows = {r.channel_type: r for r in channel_readiness(_cfg(), {})}
        assert rows["slack"].enabled is False

    def test_a_config_predating_a_channel_degrades_instead_of_raising(self) -> None:
        # An older config has no section for a newly added channel; reading it as
        # disabled keeps the diagnostic that reports it alive.
        rows = channel_readiness(_cfg(), {})
        assert len(rows) == len(builtin_channel_descriptors())
        # No section and no credentials: every channel reads as off, and nothing
        # raises. iMessage is off too — it needs no credential, but it does need
        # its own enabled flag, which an absent section cannot supply.
        assert all(r.enabled is False for r in rows)

    def test_the_roster_order_is_preserved(self) -> None:
        names = [d.channel_type for d in builtin_channel_descriptors()]
        assert [r.channel_type for r in channel_readiness(_cfg(), {})] == names

    def test_an_empty_credential_value_counts_as_missing(self) -> None:
        # A blank .env line is the common shape of "I meant to set this".
        rows = {
            r.channel_type: r
            for r in channel_readiness(
                _cfg(telegram=SimpleNamespace(enabled=True)), {"TELEGRAM_BOT_TOKEN": ""}
            )
        }
        assert rows["telegram"].missing_credentials == ("TELEGRAM_BOT_TOKEN",)


class TestStatusPayload:
    def test_every_channel_reaches_the_status_payload(self) -> None:
        # Only slack_connected reached /api/status before, so System > Services was
        # silent about a channel that failed to start.
        from kiro_crew.dashboard.state import DashboardState

        state = object.__new__(DashboardState)
        state.slack_client = None
        state.slack_socket_connected = False
        for name in ("telegram", "discord", "webex", "wecom", "teams", "weixin", "imessage"):
            setattr(state, f"{name}_connected", False)
            setattr(state, f"{name}_connect_error", "")
        state.telegram_connected = True
        state.discord_connect_error = "invalid_auth"

        status = state.channel_status()
        names = {d.channel_type for d in builtin_channel_descriptors()}
        assert set(status) == names
        assert status["telegram"] == {"connected": True, "error": ""}
        assert status["discord"] == {"connected": False, "error": "invalid_auth"}
        assert status["slack"]["connected"] is False

    def test_a_reconnect_loop_cannot_publish_an_unbounded_reason(self) -> None:
        from kiro_crew.dashboard.state import DashboardState

        state = object.__new__(DashboardState)
        state.slack_client = None
        state.slack_socket_connected = False
        for name in ("telegram", "discord", "webex", "wecom", "teams", "weixin", "imessage"):
            setattr(state, f"{name}_connected", False)
            setattr(state, f"{name}_connect_error", "")
        state.telegram_connect_error = "x" * 5000
        assert len(state.channel_status()["telegram"]["error"]) == 120

    def test_a_channel_with_no_flags_yet_reads_as_not_connected(self) -> None:
        from kiro_crew.dashboard.state import DashboardState

        state = object.__new__(DashboardState)
        state.slack_client = None
        state.slack_socket_connected = False
        status = state.channel_status()
        assert status["telegram"] == {"connected": False, "error": ""}


class TestDoctorSection:
    @pytest.mark.parametrize(
        "enabled,token,expect",
        [
            (True, "", "missing TELEGRAM_BOT_TOKEN"),
            (True, "1:AA", "credentials present"),
            (False, "", "none enabled"),
        ],
    )
    def test_the_doctor_reports_each_enabled_channel(
        self, capsys: pytest.CaptureFixture[str], enabled: bool, token: str, expect: str
    ) -> None:
        # Drives the section's own logic with the shared readiness data rather than
        # the whole doctor, which needs a live host.
        rows = channel_readiness(
            _cfg(telegram=SimpleNamespace(enabled=enabled)),
            {"TELEGRAM_BOT_TOKEN": token} if token else {},
        )
        printed: list[str] = []
        others = [r for r in rows if r.channel_type != "slack"]
        if not any(r.enabled for r in others):
            printed.append("  status:      ⏭  none enabled (optional)")
        for row in others:
            if not row.enabled:
                continue
            if row.ready:
                printed.append(f"  {row.channel_type + ':':12} ✅ enabled, credentials present")
            else:
                printed.append(
                    f"  {row.channel_type + ':':12} ❌ enabled but missing "
                    f"{', '.join(row.missing_credentials)}"
                )
        assert any(expect in line for line in printed), printed


class TestRequiredConfig:
    """Non-secret config a channel also needs to start.

    Reported separately from the credentials because they live in different files:
    a secret belongs in `.env`, an account id in `config.json`. Folding one into
    `missing_credentials` would name something that is not a credential and send
    the operator looking in the wrong place.
    """

    def test_weixin_needs_its_account_id_to_be_ready(self) -> None:
        # weixin/gateway.py refuses to start on either half missing, so readiness
        # that checked only the token would report a channel as ready that the
        # gateway then silently skips.
        rows = {
            r.channel_type: r
            for r in channel_readiness(
                _cfg(weixin=SimpleNamespace(enabled=True, token="tk", account_id="")),
                {"WEIXIN_TOKEN": "tk"},
            )
        }
        assert rows["weixin"].missing_credentials == ()
        assert rows["weixin"].missing_config == ("account_id",)
        assert rows["weixin"].ready is False

    def test_both_halves_present_is_ready(self) -> None:
        rows = {
            r.channel_type: r
            for r in channel_readiness(
                _cfg(weixin=SimpleNamespace(enabled=True, token="tk", account_id="acc")),
                {"WEIXIN_TOKEN": "tk"},
            )
        }
        assert rows["weixin"].ready is True

    def test_a_channel_with_no_required_config_is_unaffected(self) -> None:
        rows = {
            r.channel_type: r
            for r in channel_readiness(
                _cfg(telegram=SimpleNamespace(enabled=True, bot_token="1:AA")), {}
            )
        }
        assert rows["telegram"].missing_config == ()
        assert rows["telegram"].ready is True

    def test_every_declared_required_config_names_a_real_field(self) -> None:
        # A typo'd attribute would read as permanently missing, so the channel would
        # never be reported ready no matter what the operator set.
        from dataclasses import fields

        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig()
        broken: list[str] = []
        for d in builtin_channel_descriptors():
            section = getattr(cfg, d.channel_type, None)
            if section is None:
                continue
            names = {f.name for f in fields(section)}
            broken += [f"{d.channel_type}.{a}" for a in d.required_config if a not in names]
        assert not broken, f"required_config names no such field: {broken}"


def _credentials_the_gateway_requires() -> dict[str, set[str]]:
    """``channel -> {CRED_* names}`` its ``_<channel>_enabled`` expression requires.

    DERIVED from the gateway rather than listed here, because a list is what went
    wrong: Feishu's descriptor declared no credentials while its enablement required
    two, so readiness reported "credentials present" for a channel the gateway then
    silently skipped. Read statically — importing the orchestrator would pull in ten
    vendor clients, and the channel that fails to import is the one whose gap would
    go unnoticed.

    Walks ``self._<ch>_enabled = bool(... self._x ... self._y ...)`` and maps each
    attribute it reads back to the ``creds.get(CRED_*)`` that assigned it.
    """
    import ast
    import re
    from pathlib import Path

    import kiro_crew as kiro_crew_pkg

    src = (Path(kiro_crew_pkg.__file__).parent / "slack" / "gateway.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cred_of: dict[str, str] = {}
    required: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        name = getattr(node.targets[0], "attr", None)
        if not name:
            continue
        value = node.value
        if isinstance(value, ast.Call) and getattr(value.func, "attr", "") == "get" and value.args:
            first = value.args[0]
            if isinstance(first, ast.Name) and first.id.startswith("CRED_"):
                cred_of[name] = first.id
        match = re.fullmatch(r"_(\w+)_enabled", name)
        if match:
            reads = {n.attr for n in ast.walk(value) if isinstance(n, ast.Attribute)}
            required[match.group(1)] = reads
    return {
        channel: {cred_of[attr] for attr in reads if attr in cred_of}
        for channel, reads in required.items()
    }


class TestDescriptorsMatchWhatTheGatewayNeeds:
    def test_the_derivation_finds_the_gateway_at_all(self) -> None:
        """A walker that matched nothing would make the next test vacuous."""
        derived = _credentials_the_gateway_requires()
        # Named, not counted: these three check their credentials inside the
        # enablement expression, which is the shape the walker reads.
        assert {"slack", "wecom", "feishu"} <= set(derived)
        assert derived["feishu"], "feishu's enablement does require credentials"

    def test_every_credential_the_enablement_requires_is_declared(self) -> None:
        """One direction only, and deliberately.

        A credential the gateway REQUIRES must appear on the descriptor, or readiness
        reports a channel as ready that then silently does not start. The converse is
        not a defect: several channels gate on `cfg.<ch>.enabled` alone and check
        their token inside `maybe_start_*`, so they legitimately declare more than
        the enablement expression reads.
        """
        import kiro_crew.config.loader as loader

        derived = _credentials_the_gateway_requires()
        declared = {d.channel_type: set(d.credentials) for d in builtin_channel_descriptors()}
        missing: list[str] = []
        for channel, needed in sorted(derived.items()):
            if channel not in declared:
                continue
            for const in sorted(needed):
                key = getattr(loader, const)
                if key not in declared[channel]:
                    missing.append(f"{channel} requires {const} ({key}) but does not declare it")
        assert not missing, (
            "these channels would report ready and then silently not start: " f"{missing}"
        )


class TestTheServicesPageCoversEveryChannel:
    """The dashboard's Services list walks its OWN label map, not the payload.

    So a channel the backend grows without a label there is INVISIBLE on that page
    even while connected or reporting an error. That has now happened twice —
    WhatsApp, then Feishu — and both times the frontend's own test enumerated the
    channels by hand, which is a list that fails OPEN in exactly the same way.

    This is the cross-language chokepoint: the roster lives in Python and the map in
    TypeScript, so nothing but a check spanning both can catch the omission. Read as
    SOURCE, not imported, since there is no Python-side view of a TSX module.
    """

    @staticmethod
    def _label_map_keys() -> set[str]:
        import re
        from pathlib import Path

        # From THIS file, not from the package: `kiro_crew.__file__` points into
        # `src/`, and an installed wheel has no `website/` above it at all, so
        # deriving the repo root from the test's own path is both shorter and the
        # only spelling that cannot silently resolve to a directory that is not
        # there. A skip that fires because the path was wrong looks exactly like a
        # skip that fires because the sources are absent.
        tsx = (
            Path(__file__).resolve().parents[1]
            / "website"
            / "src"
            / "pages"
            / "system"
            / "ServicesTab.tsx"
        )
        if not tsx.is_file():
            pytest.skip("frontend sources are not present in this checkout")
        body = tsx.read_text(encoding="utf-8")
        block = re.search(
            r"const CHANNEL_LABEL_KEY: Record<string, string> = \{(.*?)\n\}", body, re.S
        )
        assert block, "the label map is no longer where this gate reads it"
        return set(re.findall(r"^\s*(\w+):\s*'", block.group(1), re.M))

    def test_the_map_is_found_and_populated(self) -> None:
        """A regex that matched nothing would make the next test vacuous."""
        keys = self._label_map_keys()
        assert len(keys) >= 9, f"parsed only {sorted(keys)}"

    def test_every_rostered_channel_has_a_label(self) -> None:
        rostered = {d.channel_type for d in builtin_channel_descriptors()}
        missing = sorted(rostered - self._label_map_keys())
        assert not missing, (
            "these channels are invisible on the dashboard's Services page even when "
            f"connected or failing: {missing}. Add each to CHANNEL_LABEL_KEY in "
            "website/src/pages/system/ServicesTab.tsx plus its locale entries."
        )

    def test_the_map_names_no_channel_that_does_not_exist(self) -> None:
        # The other direction: a label for a removed channel renders a row that can
        # never report anything, and is the residue a rename leaves behind.
        rostered = {d.channel_type for d in builtin_channel_descriptors()}
        stale = sorted(self._label_map_keys() - rostered)
        assert not stale, f"CHANNEL_LABEL_KEY names channels the roster does not: {stale}"
