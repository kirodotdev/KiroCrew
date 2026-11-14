"""ACP model reasoning selectors, separated from session transport/lifecycle.

The upstream model's declared capabilities control the UI; never create a
synthetic ON/OFF/effort state that a provider cannot express.
"""
from __future__ import annotations

from typing import Any

from .acp_session_state import Session
from .config import ModelSpec
from .protocol import THINKING_CONFIG_ID, EFFORT_CONFIG_ID
from .reasoning_control import transport_effort_level


class ReasoningOptionsMixin:
    @staticmethod
    def _thinking_choices(model: ModelSpec | None) -> tuple[str, ...]:
        """Deep-thinking modes that have an exact, safe upstream mapping.

        Thinking mode and thinking *effort* are independent capabilities.  In
        particular, an adaptive-only provider may legitimately expose
        ``off/auto`` without a forced ``on`` mode, while an always-on model may
        expose effort levels but no thinking selector at all.  Do not synthesize
        a universal on/off/auto trio for UI symmetry.
        """
        if model is None or model.reasoning_supported is not True or model.reasoning_can_disable is not True:
            return ()

        choices: list[str] = []
        # ``on`` is only meaningful if the adapter can request it explicitly, an
        # effort level itself enables reasoning, or omission is a verified
        # default-on contract and there is no distinct adaptive/auto wire mode.
        can_on = bool(
            model.reasoning_enable_value
            or model.effort_supported_levels
            or (model.reasoning_default_enabled is True and not model.reasoning_auto_value)
        )
        if can_on:
            choices.append("on")
        if model.reasoning_disable_value:
            choices.append("off")
        if model.reasoning_supports_auto is True and model.reasoning_auto_value:
            choices.append("auto")

        # A one-option selector is not a control. Fixed modes are shown as model
        # capability text rather than a meaningless dropdown.
        return tuple(choices) if len(choices) >= 2 else ()

    @classmethod
    def _thinking_default(cls, model: ModelSpec | None) -> str:
        if model is None or model.reasoning_supported is not True:
            return ""
        if model.reasoning_can_disable is not True:
            return "on"
        choices = cls._thinking_choices(model)
        if not choices:
            return ""
        if model.reasoning_default_enabled is False and "off" in choices:
            return "off"
        if model.reasoning_default_enabled is True:
            if "on" in choices:
                return "on"
            # Some APIs represent the only enabled state as an explicit adaptive
            # mode.  In that case Auto is the faithful default-on control.
            if "auto" in choices:
                return "auto"
        # Unknown remains provider default; never invent a mode.
        return ""

    @classmethod
    def _thinking_config(cls, model: ModelSpec | None, current: str) -> dict[str, Any] | None:
        choices = cls._thinking_choices(model)
        if not choices:
            return None
        names = {"on": "开", "off": "关", "auto": "自动"}
        return {
            "id": THINKING_CONFIG_ID,
            "name": "Deep thinking",
            "category": "thought_mode",
            "type": "select",
            "currentValue": current if current in choices else "",
            "options": [{"value": value, "name": names[value]} for value in choices],
        }

    @staticmethod
    def _reasoning_wire_value(model: ModelSpec | None, thinking_mode: str, effort: str) -> str:
        """Compatibility facade: effort only; provider adapters encode ON/OFF."""
        return transport_effort_level(model, thinking_mode, effort)

    @classmethod
    def _normalize_session_reasoning(
        cls,
        session: Session,
        model: ModelSpec | None,
    ) -> None:
        if model is None:
            session.effort = ""
            session.thinking_mode = ""
            session.effort_user_set = False
            session.thinking_user_set = False
            return

        # A concrete model switch is also a capability-boundary switch. An
        # effort value selected for the previous model must never survive when
        # the new model cannot express it. Type selections are handled outside
        # this helper and keep no session-wide effort at all.
        selectable = set(model.selectable_effort_levels)
        if session.effort_user_set:
            if session.effort not in selectable:
                session.effort_user_set = False
                session.effort = cls._effort_default(model)
        elif session.effort not in model.effort_supported_levels:
            session.effort = cls._effort_default(model)

        choices = cls._thinking_choices(model)
        if not session.thinking_user_set:
            if choices:
                if session.thinking_mode not in choices:
                    session.thinking_mode = cls._thinking_default(model)
            else:
                session.thinking_mode = cls._thinking_default(model)

    @staticmethod
    def _effort_default(model: ModelSpec | None) -> str:
        if model is None or model.reasoning_supported is not True:
            return ""
        if model.reasoning_mode == "fixed" and model.reasoning_fixed_level:
            # Fixed reasoning is not user-selectable, but an upstream that
            # explicitly advertised its sole accepted value may still require
            # that transport parameter.
            if model.reasoning_fixed_level in model.effort_supported_levels:
                return model.reasoning_fixed_level
            return ""
        if not model.effort_supported_levels:
            return ""
        if model.effort_default_level in model.effort_supported_levels:
            return model.effort_default_level
        # An empty current value means "let the provider use its own default".
        return ""

    @staticmethod
    def _effort_config(model: ModelSpec | None, current: str) -> dict[str, Any] | None:
        if model is None or not model.selectable_effort_levels:
            return None
        def display_name(level: str) -> str:
            if level.strip().lower() in {"none", "off", "disabled"}:
                return "Off"
            return level

        options = [
            {"value": level, "name": display_name(level)}
            for level in model.selectable_effort_levels
        ]
        current_value = current if current in model.selectable_effort_levels else ""
        # ACP select options do not require a synthetic "default" row. An empty
        # current value means the provider default is in effect; once the user
        # chooses a level Crew persists and writes the advertised vocabulary.
        return {
            "id": EFFORT_CONFIG_ID,
            "name": "Reasoning effort",
            "category": "thought_level",
            "type": "select",
            "currentValue": current_value,
            "options": options,
        }

