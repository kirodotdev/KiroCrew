"""How application code keeps a Bot chat's unconfirmed close for retry.

The handles a pending close is retried under, and the local tokens a close with
no native session id is tracked under, are the ACP layer's
(``kiro_crew.acp.session_provider_shared``), and so is the test of whether a
failed start's answer was lost on its way back (``answer_lost``);
``kiro_crew.bot_chat_app_tools`` reaches them here.
"""

from __future__ import annotations

from kiro_crew.acp.session_handle_runtime_contract import answer_lost
from kiro_crew.acp.session_provider_shared import (
    RuntimeTreeGone,
)
from kiro_crew.acp.session_provider_shared import _owner as owner
from kiro_crew.acp.session_provider_shared import (
    close_handle,
    is_local_close_token,
    local_close_token,
    merge_close_handle,
    replace_local_token,
)

__all__ = [
    "RuntimeTreeGone",
    "answer_lost",
    "close_handle",
    "is_local_close_token",
    "local_close_token",
    "merge_close_handle",
    "owner",
    "replace_local_token",
]
