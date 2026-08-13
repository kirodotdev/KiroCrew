"""Telegram channel for KiroCrew via Telegram Bot API.

DM-focused, long-polling loop to Telegram's getUpdates endpoint.
Reuses the existing session machinery (SessionManager / provider.stream); this
package only does Telegram I/O and maps it onto a small render layer.

Streaming is achieved via sendMessage → repeated editMessageText (throttled 1s).
Inline keyboards provide real button interactions for [OPTIONS:] and tool approval.
"""

from __future__ import annotations
