"""The common BCP-47 language codes the speech Language pickers offer.

Shared by the Speech-to-Text settings route and the Text-to-Speech voice settings
route, so both pickers list the same languages. This module imports nothing from
the package, so either route can use it without an import cycle.
"""

from __future__ import annotations

#: Common BCP-47 codes, in picker order. The list only drives the dropdowns: each
#: route adds a stored code it does not hold so that code stays selectable, and
#: the providers behind them support many more.
COMMON_LANGUAGE_CODES: tuple[str, ...] = (
    "en-US",
    "en-GB",
    "fr-FR",
    "de-DE",
    "es-ES",
    "es-US",
    "it-IT",
    "pt-BR",
    "ja-JP",
    "ko-KR",
    "zh-CN",
    "vi-VN",
)
