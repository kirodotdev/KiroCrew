"""ACP host compatibility seams.

Stable ACP semantics live in the server. Product-specific metadata is decoded
through a host adapter so KiroCrew hints never become implicit protocol truth.
Other ACP clients simply receive the generic adapter and therefore cannot
silently pin Kiro-only model/effort fields.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SessionHostHints:
    host: str = "generic"
    requested_model: str = ""
    requested_effort: str = ""
    requested_thinking: str = ""


class AcpHostAdapter:
    name = "generic"

    def session_hints(self, params: dict[str, Any]) -> SessionHostHints:
        return SessionHostHints(host=self.name)


class KiroCrewHostAdapter(AcpHostAdapter):
    name = "kirocrew"

    def session_hints(self, params: dict[str, Any]) -> SessionHostHints:
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        kiro = meta.get("kiro") if isinstance(meta.get("kiro"), dict) else {}
        return SessionHostHints(
            host=self.name,
            requested_model=str(kiro.get("modelId") or "").strip(),
            requested_effort=str(kiro.get("effortLevel") or "").strip(),
            requested_thinking=str(kiro.get("thinkingMode") or "").strip().lower(),
        )


_GENERIC = AcpHostAdapter()
_KIRO = KiroCrewHostAdapter()


def adapter_for_session_params(params: dict[str, Any]) -> AcpHostAdapter:
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    if isinstance(meta.get("kiro"), dict):
        return _KIRO
    return _GENERIC
