"""Provider-neutral inference API."""
from .base import ChatTurn, ProviderAdapter, ProviderError, ProviderInterrupted, ProviderTransportError, ToolCall, UsageInfo
from .openai_compatible import OpenAICompatibleError, OpenAICompatibleInterrupted
from .registry import adapter_for, chat_completion, chat_completion_turn

__all__ = [
    "ChatTurn",
    "ProviderAdapter",
    "ProviderError",
    "ProviderInterrupted",
    "ProviderTransportError",
    "ToolCall",
    "UsageInfo",
    "OpenAICompatibleError",
    "OpenAICompatibleInterrupted",
    "adapter_for",
    "chat_completion",
    "chat_completion_turn",
]
