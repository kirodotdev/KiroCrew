"""Contract tests: the Slack manifest grants every scope/event the code uses.

The structural tests elsewhere (deep-link signature, alias substitution) check
that the manifest template renders and validates consistently — they say nothing
about whether the granted surface matches what ``slack/client.py`` and
``slack/handler.py`` actually call. That gap is how the manifest drifted: code
grew ``users.info`` lookups and private-channel thread reads while the template
kept the original scope list, leaving tracked private channels silently dead.

These tests parse the packaged template and pin the API-surface contract in
both directions: every Slack Web API method the runtime calls must have its
required bot scope granted (under-grant), and the full grant set must equal the
reviewed list (over-grant) — a scope no code path calls widens the OAuth
consent screen for every future install, so widening must be a deliberate,
visible edit to this file rather than a one-line YAML addition.
"""

from __future__ import annotations

import yaml

from kiro_crew import slack_manifest


def _manifest(features: frozenset[str] = frozenset()) -> dict:
    parsed = yaml.safe_load(slack_manifest.template(features))
    assert isinstance(parsed, dict)
    return parsed


_BASE_BOT_SCOPES = [
    "app_mentions:read",
    "channels:history",
    "channels:read",
    "chat:write",
    "commands",
    "files:read",
    "files:write",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "im:write",
    "reactions:write",
    "users:read",
]
#: Granted only with ``slack.code_channels``: agents.conversations.* plus the
#: session identity override and the channel canvas.
_CODE_CHANNEL_SCOPES = [
    "canvases:write",
    "channels:write.invites",
    "chat:write.customize",
    "code_channels:manage",
    "groups:write.invites",
]

_BASE_BOT_EVENTS = [
    "app_home_opened",
    "app_mention",
    "file_change",
    "member_joined_channel",
    "message.channels",
    "message.groups",
    "message.im",
]
#: A code channel shows Slack's Stop button only when the app subscribes to it,
#: and its dashboard badge follows the channel's archive state and name.
_CODE_CHANNEL_EVENTS = [
    "agent_session_stopped",
    "channel_archive",
    "channel_rename",
    "channel_unarchive",
    "group_archive",
    "group_rename",
    "group_unarchive",
]

_AGENT_SESSION_SCOPES = ["assistant:write"]

_AGENT_SESSION_EVENTS = ["agent_session_stopped", "agent_session_title_changed"]

_COMBOS = slack_manifest.feature_sets()


def _bot_scopes(manifest: dict) -> list[str]:
    return manifest["oauth_config"]["scopes"]["bot"]


def _bot_events(manifest: dict) -> list[str]:
    return manifest["settings"]["event_subscriptions"]["bot_events"]


class TestManifestGrantsWhatTheCodeUses:
    def test_private_channel_history_scope_granted(self) -> None:
        # conversations.replies on a private channel (slack/handler.py thread
        # fallback) and message.groups event delivery both require it.
        assert "groups:history" in _bot_scopes(_manifest())

    def test_users_read_scope_granted(self) -> None:
        # users.info is called from get_user_info and get_user_profile in
        # slack/client.py.
        assert "users:read" in _bot_scopes(_manifest())

    def test_private_channel_message_event_subscribed(self) -> None:
        # The handler routes `message` events from any channel type; without
        # this subscription Slack never delivers private-channel messages, so
        # a tracked private channel is silently dead.
        assert "message.groups" in _bot_events(_manifest())

    def test_public_and_dm_message_events_still_subscribed(self) -> None:
        events = _bot_events(_manifest())
        assert "message.channels" in events
        assert "message.im" in events

    def test_stripped_template_carries_the_same_grants(self) -> None:
        # security.py validates deep links against stripped_template(), so the
        # comment-stripped variant must carry the identical grant surface —
        # otherwise the deep-link import path would create a narrower app than
        # the documented manifest.
        for combo in _COMBOS:
            stripped = yaml.safe_load(slack_manifest.stripped_template(combo))
            full = _manifest(combo)
            assert _bot_scopes(stripped) == _bot_scopes(full), combo
            assert _bot_events(stripped) == _bot_events(full), combo
            assert stripped.get("features") == full.get("features"), combo

    def test_raw_template_parses_with_every_switch_on(self) -> None:
        # The gates are YAML comments, so the raw file is a valid manifest that
        # reads as "everything on"; a reader of the file sees the whole surface.
        raw = yaml.safe_load(slack_manifest.raw_template())
        assert _bot_scopes(raw) == _bot_scopes(_manifest(frozenset(slack_manifest.FEATURES)))


class TestManifestGrantsNothingMore:
    """Over-grant ratchet: widening the consent surface must edit THIS list.

    A membership check cannot fail when a scope is *added*, so presence tests
    alone would let a scope no code path calls ship silently. Equality pins
    the reviewed set: any widening shows up in review as a deliberate change
    to this file, with a justification alongside it.
    """

    def test_bot_scopes_are_exactly_the_reviewed_set(self) -> None:
        for combo in _COMBOS:
            want = set(_BASE_BOT_SCOPES)
            if slack_manifest.CODE_CHANNELS in combo:
                want |= set(_CODE_CHANNEL_SCOPES)
            if slack_manifest.AGENT_SESSIONS in combo:
                want |= set(_AGENT_SESSION_SCOPES)
            assert sorted(_bot_scopes(_manifest(combo))) == sorted(want), combo

    def test_bot_events_are_exactly_the_reviewed_set(self) -> None:
        for combo in _COMBOS:
            want = set(_BASE_BOT_EVENTS)
            if slack_manifest.CODE_CHANNELS in combo:
                want |= set(_CODE_CHANNEL_EVENTS)
            if slack_manifest.AGENT_SESSIONS in combo:
                want |= set(_AGENT_SESSION_EVENTS)
            events = _bot_events(_manifest(combo))
            assert len(events) == len(set(events)), combo  # no duplicate subscription
            assert sorted(events) == sorted(want), combo

    def test_feature_blocks_follow_the_switches(self) -> None:
        for combo in _COMBOS:
            feats = _manifest(combo)["features"]
            # Only the Agent sessions switch declares the app a Slack agent
            # (one-way in Slack), and nothing ever declares assistant_view.
            if slack_manifest.AGENT_SESSIONS in combo:
                assert set(feats["agent_view"]) == {"agent_description"}, combo
            else:
                assert "agent_view" not in feats, combo
            assert "assistant_view" not in feats, combo
            if slack_manifest.CODE_CHANNELS in combo:
                # Socket Mode delivers channel commands: no slash_command_url.
                assert feats["code_channels"] == {"enabled": True}, combo
            else:
                assert "code_channels" not in feats, combo

    def test_user_token_scopes_are_exactly_the_reviewed_set(self) -> None:
        # The gateway itself runs on the bot token and calls nothing with a
        # user token. The user block exists for a separately configured Slack
        # MCP/search integration (documented in docs/guides/slack-setup.md
        # Step 4); it still widens the OAuth consent screen, so it is pinned
        # exactly — widening it must be a deliberate, visible edit here.
        assert sorted(
            _manifest(frozenset(slack_manifest.FEATURES))["oauth_config"]["scopes"]["user"]
        ) == [
            "channels:history",
            "channels:read",
            "groups:history",
            "groups:read",
            "im:history",
            "im:read",
            "mpim:history",
            "mpim:read",
            "search:read",
            "users:read",
        ]
