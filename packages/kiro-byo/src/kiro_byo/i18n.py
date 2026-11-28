"""Kiro BYO locale resources and safe UI preferences (not ACP wire fields)."""
from __future__ import annotations
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

LOCALES = ("zh-CN", "zh-TW", "en", "ja", "ko")
LOCALE_DIR = Path(__file__).parent / "locales"


def normalize_ui_language(value: Any) -> str:
    text = str(value or "auto").strip().replace("_", "-")
    if text.lower() in {"", "auto", "follow", "system"}:
        return "auto"
    if text.lower().startswith("zh-hant") or text.lower() in {"zh-tw", "zh-hk", "zh-mo"}:
        return "zh-TW"
    if text.lower().startswith("zh"):
        return "zh-CN"
    for locale in ("en", "ja", "ko"):
        if text.lower().split("-")[0] == locale:
            return locale
    raise ValueError("unsupported UI locale")


@lru_cache(maxsize=5)
def locale_messages(locale: str) -> dict[str, str]:
    normalized = normalize_ui_language(locale)
    if normalized == "auto":
        normalized = "en"
    content = json.loads((LOCALE_DIR / (normalized + ".json")).read_text(encoding="utf-8"))
    if not isinstance(content, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in content.items()):
        raise ValueError("invalid locale resource")
    return content


def error_code(error: str) -> str | None:
    """Small additive management-API taxonomy; preserves the raw diagnostic."""
    lower = str(error).lower()
    if any(k in lower for k in ("unauthorized", "authentication", "invalid api key", "401")):
        return "PROVIDER_AUTH_FAILED"
    if "session busy" in lower or "session is busy" in lower:
        return "SESSION_BUSY"
    if "incompatible" in lower and ("integration" in lower or "crew" in lower):
        return "INTEGRATION_INCOMPATIBLE"
    if "model" in lower and any(k in lower for k in ("not found", "unavailable", "not available")):
        return "MODEL_NOT_AVAILABLE"
    return None
