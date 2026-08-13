"""Reusable Block Kit builders for KiroCrew slash commands.

All functions return raw Block Kit dicts (no slack_sdk dependency).
Action IDs follow the mc_<command>_<action>[_<id>] convention.
"""

from __future__ import annotations

import json

_MAX_MSG_CHARS = 4000
_MAX_MESSAGES = 5


def _msg_elements(messages: list[dict]) -> list[dict]:
    """Build rich_text_list elements from session messages."""
    from kiro_crew.security import redact_and_truncate

    items: list[dict] = []
    for msg in messages[-_MAX_MESSAGES:]:
        role = msg.get("role", "user")
        emoji = "robot_face" if role == "assistant" else "bust_in_silhouette"
        text = redact_and_truncate(msg.get("content") or "", max_chars=_MAX_MSG_CHARS)
        items.append(
            {
                "type": "rich_text_section",
                "elements": [
                    {"type": "emoji", "name": emoji},
                    {"type": "text", "text": f" {text}"},
                ],
            }
        )
    return items


def session_task_card(
    idx: int,
    key: str,
    title: str,
    agent: str,
    status: str,
    messages: list[dict],
) -> list[dict]:
    """Task card block + resume button for a session."""
    if status == "active":
        emoji, card_status = "🟢", "in_progress"
    elif status == "paused":
        emoji, card_status = "⏸️", "complete"
    else:
        emoji, card_status = "⚫", "complete"

    items = _msg_elements(messages)
    card: dict = {
        "type": "task_card",
        "task_id": f"session_{idx}",
        "title": f"{emoji} {title} — {agent or 'kirocrew'} agent",
        "status": card_status,
    }
    if items:
        card["details"] = {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_list",
                    "style": "bullet",
                    "elements": items,
                }
            ],
        }
    blocks: list[dict] = [
        card,
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "▶️ Resume"},
                    "action_id": f"mc_session_resume_{key}",
                    "value": json.dumps({"key": key, "title": title}),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⏹️ End"},
                    "action_id": f"mc_session_end_{key}",
                    "value": key,
                    "style": "danger",
                }
            ],
        },
    ]
    return blocks


def config_panel(settings: dict) -> list[dict]:
    """Toggle and select blocks for each setting in the dict.

    Supported value types: bool → toggle, list → static_select, str → plain display.
    """
    blocks: list[dict] = []
    for key, val in settings.items():
        if isinstance(val, bool):
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{key}*"},
                    "accessory": {
                        "type": "checkboxes",
                        "action_id": f"mc_config_{key}",
                        "options": [
                            {
                                "text": {"type": "plain_text", "text": "Enabled"},
                                "value": key,
                            }
                        ],
                        "initial_options": (
                            [
                                {
                                    "text": {"type": "plain_text", "text": "Enabled"},
                                    "value": key,
                                }
                            ]
                            if val
                            else []
                        ),
                    },
                }
            )
        elif isinstance(val, list):
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{key}*"},
                    "accessory": {
                        "type": "static_select",
                        "action_id": f"mc_config_{key}",
                        "options": [
                            {"text": {"type": "plain_text", "text": str(v)}, "value": str(v)}
                            for v in val
                        ],
                    },
                }
            )
        else:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{key}:* {val}"},
                }
            )
    return blocks


def confirmation_dialog(title: str, text: str, confirm_text: str, deny_text: str, action_prefix: str = "mc_stop") -> list[dict]:
    """Section with confirm/deny action buttons."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{title}*\n{text}"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": confirm_text},
                    "action_id": f"{action_prefix}_confirm",
                    "style": "danger",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": deny_text},
                    "action_id": f"{action_prefix}_cancel",
                },
            ],
        },
    ]


def command_hint_block(command: str, description: str) -> dict:
    """Single section block showing a command and its description."""
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"`{command}` — {description}"},
    }


def dashboard_link_block(url: str, link_mins: int, session_mins: int) -> list[dict]:
    """Section with clickable dashboard link and expiry info."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🔗 <{url}|*Open Dashboard*>\n"
                    f"⏱ Click within {link_mins}m · session lasts {session_mins}m"
                ),
            },
        },
    ]


def agent_buttons(agents: list[str], current: str) -> list[dict]:
    """Buttons for each available agent, current one marked with ✅."""
    buttons = []
    for name in agents:
        label = f"✅ {name}" if name == current else name
        btn: dict = {
            "type": "button",
            "text": {"type": "plain_text", "text": label[:75]},
            "action_id": f"mc_agent_select_{name}",
            "value": name,
        }
        if name == current:
            btn["style"] = "primary"
        buttons.append(btn)
    # "Off" button to reset to default
    off_label = "✅ off" if not current else "off"
    buttons.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": off_label},
            "action_id": "mc_agent_select_off",
            "value": "off",
        }
    )
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Current agent:* {current or 'default (kirocrew)'}",
            },
        },
    ]
    # Slack limits 5 buttons per actions block
    for i in range(0, len(buttons), 5):
        blocks.append({"type": "actions", "elements": buttons[i : i + 5]})
    return blocks


def deprecation_warning_block(old_cmd: str, new_cmd: str) -> dict:
    """Context block warning that a bang command is deprecated."""
    return {
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f"⚠️ `{old_cmd}` is deprecated. Use `{new_cmd}` instead.",
            }
        ],
    }


def voice_config_modal(
    tts_enabled: bool = False,
    auto_speak: bool = False,
    voice: str = "Ruth",
    engine: str = "generative",
    speed: str = "100%",
    pitch: str = "+0%",
    aws_profile: str = "",
    region: str = "",
) -> dict:
    """Modal view for voice/TTS configuration (callback_id: mc_voice_config)."""
    voices = [
        ("Ruth", "Ruth (US F)"),
        ("Matthew", "Matthew (US M)"),
        ("Arthur", "Arthur (UK M)"),
        ("Brian", "Brian (UK M)"),
        ("Amy", "Amy (UK F)"),
        ("Joanna", "Joanna (US F)"),
        ("Stephen", "Stephen (US M)"),
        ("Gregory", "Gregory (US M)"),
        ("Danielle", "Danielle (US F)"),
    ]
    engines = [
        ("generative", "Generative"),
        ("neural", "Neural"),
        ("long-form", "Long-form"),
        ("standard", "Standard"),
    ]
    speeds = ["80%", "90%", "95%", "100%", "110%", "120%", "130%", "150%"]
    pitches = ["-20%", "-10%", "-5%", "+0%", "+5%", "+10%", "+20%"]

    def _opts(pairs: list) -> list[dict]:
        return [{"text": {"type": "plain_text", "text": l}, "value": v} for v, l in pairs]

    def _str_opts(vals: list[str]) -> list[dict]:
        return [{"text": {"type": "plain_text", "text": v}, "value": v} for v in vals]

    def _initial(opts: list[dict], val: str, fallback: int = 0) -> dict:
        return next((o for o in opts if o["value"] == val), opts[fallback])

    voice_opts = _opts(voices)
    engine_opts = _opts(engines)
    speed_opts = _str_opts(speeds)
    pitch_opts = _str_opts(pitches)

    _enabled_opt = {
        "text": {"type": "plain_text", "text": "Enable TTS voice replies"},
        "value": "enabled",
    }
    _auto_opt = {
        "text": {"type": "plain_text", "text": "Auto-speak every response"},
        "value": "auto_speak",
    }

    initial_checks = []
    if tts_enabled:
        initial_checks.append(_enabled_opt)
    if auto_speak:
        initial_checks.append(_auto_opt)

    blocks: list[dict] = [
        {
            "type": "input",
            "block_id": "tts_enabled_block",
            "optional": True,
            "element": {
                "type": "checkboxes",
                "action_id": "mc_voice_tts_enabled",
                "options": [_enabled_opt, _auto_opt],
                **({"initial_options": initial_checks} if initial_checks else {}),
            },
            "label": {"type": "plain_text", "text": "Text-to-Speech"},
        },
        {
            "type": "input",
            "block_id": "voice_block",
            "element": {
                "type": "static_select",
                "action_id": "mc_voice_voice",
                "options": voice_opts,
                "initial_option": _initial(voice_opts, voice),
            },
            "label": {"type": "plain_text", "text": "Voice"},
        },
        {
            "type": "input",
            "block_id": "engine_block",
            "element": {
                "type": "static_select",
                "action_id": "mc_voice_engine",
                "options": engine_opts,
                "initial_option": _initial(engine_opts, engine),
            },
            "label": {"type": "plain_text", "text": "Engine"},
        },
        {
            "type": "input",
            "block_id": "speed_block",
            "element": {
                "type": "static_select",
                "action_id": "mc_voice_speed",
                "options": speed_opts,
                "initial_option": _initial(speed_opts, speed, 3),
            },
            "label": {"type": "plain_text", "text": "Speed"},
        },
        {
            "type": "input",
            "block_id": "pitch_block",
            "element": {
                "type": "static_select",
                "action_id": "mc_voice_pitch",
                "options": pitch_opts,
                "initial_option": _initial(pitch_opts, pitch, 3),
            },
            "label": {"type": "plain_text", "text": "Pitch"},
        },
        {
            "type": "input",
            "block_id": "profile_block",
            "optional": True,
            "element": {
                "type": "plain_text_input",
                "action_id": "mc_voice_profile",
                "placeholder": {"type": "plain_text", "text": "default"},
                **({"initial_value": aws_profile} if aws_profile else {}),
            },
            "label": {"type": "plain_text", "text": "AWS Profile"},
        },
        {
            "type": "input",
            "block_id": "region_block",
            "optional": True,
            "element": {
                "type": "plain_text_input",
                "action_id": "mc_voice_region",
                "placeholder": {"type": "plain_text", "text": "us-east-1"},
                **({"initial_value": region} if region else {}),
            },
            "label": {"type": "plain_text", "text": "AWS Region"},
        },
    ]

    return {
        "type": "modal",
        "callback_id": "mc_voice_config",
        "title": {"type": "plain_text", "text": "Voice Settings"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def allowlist_list_block(user_ids: list[str]) -> list[dict]:
    """List allowed users with per-user remove buttons."""
    if not user_ids:
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": "_No users on the allowlist._"}}
        ]
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Allowlist* ({len(user_ids)} users)"},
        },
    ]
    for uid in sorted(user_ids):
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"<@{uid}>"},
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚫 Remove"},
                    "action_id": f"mc_allowlist_remove_{uid}",
                    "value": uid,
                    "style": "danger",
                },
            }
        )
    return blocks


def channel_list_block(channel_ids: list[str]) -> list[dict]:
    """List tracked channels with per-channel remove buttons."""
    if not channel_ids:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": "_No tracked channels._"}}]
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Tracked Channels* ({len(channel_ids)})"},
        },
    ]
    for cid in sorted(channel_ids):
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"<#{cid}>"},
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚫 Remove"},
                    "action_id": f"mc_channel_remove_{cid}",
                    "value": cid,
                    "style": "danger",
                },
            }
        )
    return blocks


# ---------------------------------------------------------------------------
# Channels management modal
# ---------------------------------------------------------------------------

_ACTIVATION_OPTIONS = [
    ("always", "⚡ always", "Respond to every message"),
    ("mention", "🔔 mention", "Only when @mentioned"),
    ("observe", "👀 observe", "Record all, respond when @mentioned"),
    ("review", "📋 review", "Draft for approval before posting"),
    ("off", "🔇 off", "Ignore completely"),
]


def channels_modal(
    channels: list[dict],
    agent_names: list[str] | None = None,
) -> dict:
    """Modal for managing tracked channels with per-channel activation and agent.

    *channels*: ``[{"channel_id": "C...", "activation": "mention", "agent": ""}, ...]``
    *agent_names*: available agent names from ~/.kiro/agents/
    """
    blocks: list[dict] = []
    agents = agent_names or []

    if not channels:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_No tracked channels yet._"},
        })
    else:
        for ch in channels:
            cid = ch["channel_id"]
            cur = ch.get("activation", "mention")
            cur_agent = ch.get("agent", "")
            # Row 1: channel name + remove button
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"<#{cid}>"},
                "accessory": {
                    "type": "button",
                    "action_id": f"mc_ch_remove_{cid}",
                    "text": {"type": "plain_text", "text": "✕ Remove"},
                    "style": "danger",
                    "value": cid,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Remove channel?"},
                        "text": {"type": "mrkdwn", "text": f"Stop tracking <#{cid}>?"},
                        "confirm": {"type": "plain_text", "text": "Remove"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
            })
            # Row 2: activation mode + agent selector
            opts = [
                {
                    "text": {"type": "plain_text", "text": label},
                    "description": {"type": "plain_text", "text": desc},
                    "value": val,
                }
                for val, label, desc in _ACTIVATION_OPTIONS
            ]
            initial = next((o for o in opts if o["value"] == cur), opts[1])
            elements: list[dict] = [{
                "type": "static_select",
                "action_id": f"mc_ch_activation_{cid}",
                "initial_option": initial,
                "options": opts,
            }]
            if agents:
                agent_opts = [
                    {"text": {"type": "plain_text", "text": "🤖 default"}, "value": "__default__"},
                ] + [
                    {"text": {"type": "plain_text", "text": n[:75]}, "value": n}
                    for n in agents
                ]
                agent_initial = next(
                    (o for o in agent_opts if o["value"] == (cur_agent or "__default__")),
                    agent_opts[0],
                )
                elements.append({
                    "type": "static_select",
                    "action_id": f"mc_ch_agent_{cid}",
                    "initial_option": agent_initial,
                    "options": agent_opts,
                })
            blocks.append({
                "type": "actions",
                "block_id": f"mc_ch_actions_{cid}",
                "elements": elements,
            })
            blocks.append({"type": "divider"})

    # Add channel picker at the bottom (conversations_select includes private channels)
    blocks.append({
        "type": "actions",
        "block_id": "mc_ch_add_block",
        "elements": [{
            "type": "conversations_select",
            "action_id": "mc_ch_add",
            "placeholder": {"type": "plain_text", "text": "➕ Add a channel…"},
            "filter": {
                "include": ["public", "private"],
                "exclude_bot_users": True,
                "exclude_external_shared_channels": True,
            },
        }],
    })

    return {
        "type": "modal",
        "callback_id": "mc_channels_modal",
        "title": {"type": "plain_text", "text": "Tracked Channels"},
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# Review mode: ephemeral draft blocks + edit modal
# ---------------------------------------------------------------------------

# Action ID prefix for review mode buttons
REVIEW_ACTION_APPROVE = "mc_review_approve"
REVIEW_ACTION_EDIT = "mc_review_edit"
REVIEW_ACTION_REVISE = "mc_review_revise"
REVIEW_ACTION_CANCEL = "mc_review_cancel"


def build_stopping_blocks(session_key: str) -> list[dict]:
    """Ephemeral 'Stopping…' message with a Kill Now escalation button."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "⏹  *Stopping…*\nCooperative cancel in progress."},
        },
        {
            "type": "actions",
            "block_id": f"stop-actions-{session_key}",
            "elements": [
                {
                    "type": "button",
                    "action_id": "stop_kill_now",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Kill Now"},
                    "value": session_key,
                }
            ],
        },
    ]


def build_stopped_blocks() -> list[dict]:
    """Resolved ephemeral: cooperative cancel succeeded."""
    return [{"type": "section", "text": {"type": "mrkdwn", "text": "⏹  *[Stopped]*"}}]


def build_stop_failed_blocks() -> list[dict]:
    """Resolved ephemeral: cooperative cancel failed, session was hard-killed."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "⛔  *[Stop Failed, Session Reset]*"},
        }
    ]


def build_working_blocks(session_key: str) -> list[dict]:
    """Inline 'working' message with a Stop button shown during execution."""
    return [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "⏳ _Working…_"}],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": f"mc_inline_stop_{session_key}",
                    "text": {"type": "plain_text", "text": "⏹ Stop"},
                    "value": session_key,
                    "style": "danger",
                }
            ],
        },
    ]


def review_draft_blocks(draft_text: str, draft_key: str) -> list[dict]:
    """Build Block Kit blocks for an ephemeral review-mode draft.

    The *draft_key* (channel|thread_ts|uuid) is encoded in button values so the
    interaction handler can look up the draft and know where to post.
    """
    # Slack blocks text limit is 3000 chars; truncate with indicator
    display = draft_text[:2950] + "…" if len(draft_text) > 3000 else draft_text
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "📝 *Draft response:*"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": display},
        },
        {"type": "divider"},
        {
            "type": "actions",
            "block_id": "mc_review_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": REVIEW_ACTION_APPROVE,
                    "text": {"type": "plain_text", "text": "✅ Approve"},
                    "style": "primary",
                    "value": draft_key,
                },
                {
                    "type": "button",
                    "action_id": REVIEW_ACTION_EDIT,
                    "text": {"type": "plain_text", "text": "✏️ Manual Override"},
                    "value": draft_key,
                },
                {
                    "type": "button",
                    "action_id": REVIEW_ACTION_REVISE,
                    "text": {"type": "plain_text", "text": "🤖 Refine with AI"},
                    "value": draft_key,
                },
                {
                    "type": "button",
                    "action_id": REVIEW_ACTION_CANCEL,
                    "text": {"type": "plain_text", "text": "❌ Cancel"},
                    "style": "danger",
                    "value": draft_key,
                },
            ],
        },
    ]


def review_edit_modal(draft_text: str, draft_key: str) -> dict:
    """Modal for editing a review-mode draft before posting."""
    return {
        "type": "modal",
        "callback_id": "mc_review_edit_submit",
        "private_metadata": draft_key,
        "title": {"type": "plain_text", "text": "Manual Override"},
        "submit": {"type": "plain_text", "text": "Post"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "mc_review_edit_block",
                "label": {"type": "plain_text", "text": "Response"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "mc_review_edit_input",
                    "multiline": True,
                    "initial_value": draft_text[:3000],
                },
            }
        ],
    }


def review_revise_modal(draft_key: str) -> dict:
    """Modal for providing LLM feedback to revise a review-mode draft."""
    return {
        "type": "modal",
        "callback_id": "mc_review_revise_submit",
        "private_metadata": draft_key,
        "title": {"type": "plain_text", "text": "Refine with AI"},
        "submit": {"type": "plain_text", "text": "Revise"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "mc_review_revise_block",
                "label": {"type": "plain_text", "text": "What should I change?"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "mc_review_revise_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. make it shorter, add timeline dates, softer tone...",
                    },
                },
            }
        ],
    }
