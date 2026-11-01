"""Localized ACP tool titles, kept separate from executable identity and arguments.

These strings are presentation hints only. Permissions, logs and dispatch
must continue to use canonical tool identities and the unchanged rawInput.
"""
from __future__ import annotations

import locale as system_locale
import os
import re
import unicodedata
from typing import Any

from .i18n import locale_messages, normalize_ui_language

_NATIVE_TITLES = {
    "read": "read",
    "read_files": "read_files",
    "list_directory": "list_directory",
    "glob": "glob",
    "grep": "grep",
    "write": "write",
    "edit": "edit",
    "delete_file": "delete_file",
    "web_fetch": "web_fetch",
    "web_search": "web_search",
    "shell": "shell",
    "execute_shell_process": "shell",
    "control_shell_process": "control_shell_process",
    "code_symbols": "code_symbols",
    "code_query": "code_query",
    "update_plan": "update_plan",
}


def display_locale(preference: str, *, system_language: str | None = None) -> str:
    """Use BYO UI selection; Auto falls back to OS language, never response language.

    ACP does not supply KiroCrew's frontend locale, so Auto is best-effort.
    This resolution is independent of Mode, Type and provider model policy.
    """
    try:
        resolved = normalize_ui_language(preference)
        if resolved != "auto":
            return resolved
    except ValueError:
        pass
    if system_language is None:
        try:
            system_language = system_locale.getlocale()[0]
        except (ValueError, OSError):
            system_language = None
        if not system_language:
            system_language = next((os.environ.get(name) for name in ("LC_ALL", "LC_MESSAGES", "LANG") if os.environ.get(name)), None)
    try:
        detected = normalize_ui_language((system_language or "en").split(".", 1)[0])
        return detected if detected != "auto" else "en"
    except ValueError:
        return "en"


# The model's per-call description is an untrusted display hint, not a tool
# identity, consent description, or substitute for the raw command.
_COMMAND_LIKE = re.compile(
    r"^(?:git|gh|pwsh|powershell|cmd|bash|sh|python|python3|node|npm|npx|curl|wget)\s+(?=[A-Za-z0-9/_.-])",
    re.IGNORECASE,
)
_SECRET_LIKE = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:api[_ -]?key|password|access[_ -]?token|secret)\s*[:=]\s*\S+|sk-[a-zA-Z0-9_-]{8,})"
)


def _safe_action_description(value: Any, locale: str) -> str:
    """Accept a brief human explanation only; fail closed to localized title.

    Do not attempt to infer whether the proposed explanation truthfully matches
    the executable command: the authorization and auditing paths use rawInput.
    """
    if not isinstance(value, str) or not value or len(value) > 160:
        return ""
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        return ""
    text = value.strip()
    if not (2 <= len(text) <= 120):
        return ""
    if (_COMMAND_LIKE.match(text) or _SECRET_LIKE.search(text)
            or any(fragment in text for fragment in ("&&", "||", "|", "`", "$", "\\", "://", ";"))):
        return ""
    # Best-effort cross-language guard: if the model ignored the interface
    # language, prefer the existing known-good five-language fallback.
    if locale == "en" and re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text):
        return ""
    if locale in {"zh-CN", "zh-TW", "ja"} and not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text):
        return ""
    if locale == "ko" and not re.search(r"[\uac00-\ud7af]", text):
        return ""
    return text


def tool_display_title(
    public_name: str,
    *,
    ui_language: str = "auto",
    native_name: str = "",
    mcp_tool_name: str = "",
    description: Any = None,
) -> str:
    """A readable, localized title; never a substitute for tool provenance.

    Unknown tools keep their original public name rather than being mislabeled.
    A bounded per-invocation description may replace the generic title, but
    never affects tool identity, permissions or the unchanged rawInput.
    """
    loc = display_locale(ui_language)
    try:
        messages = locale_messages(loc)
        fallback = locale_messages("en")
    except (OSError, ValueError):
        # Never block a permitted tool call because display assets are damaged.
        return public_name
    if native_name:
        key = _NATIVE_TITLES.get(native_name)
        if not key:
            return public_name
        suggested = _safe_action_description(description, loc)
        if suggested:
            return suggested
        lookup = f"tool.title.{key}"
        return messages.get(lookup) or fallback.get(lookup) or public_name
    if mcp_tool_name:
        template = messages.get("tool.title.mcp") or fallback.get("tool.title.mcp") or "MCP: {v0}"
        return template.replace("{v0}", mcp_tool_name)
    return public_name
