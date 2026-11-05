"""The App Kit seam level is one number on both sides of the host.

``AppContext.sdk_level`` (Python) and ``APP_SDK_LEVEL`` from ``@kirocrew/app-sdk``
(the dashboard) describe the same seam set, so an app that reads either must get the
same answer.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.apps.app_sdk_level import APP_SDK_LEVEL

_TS = Path(__file__).resolve().parents[1] / "website" / "src" / "app-sdk" / "sdkLevel.ts"
_EXPORT_RE = re.compile(r"^export const APP_SDK_LEVEL = (\d+)\s*$", re.MULTILINE)


def test_the_dashboard_level_equals_the_gateway_level() -> None:
    found = _EXPORT_RE.findall(_TS.read_text(encoding="utf-8"))
    assert found == [str(APP_SDK_LEVEL)], f"{_TS} must export APP_SDK_LEVEL = {APP_SDK_LEVEL}"


def test_the_level_is_a_positive_int() -> None:
    assert isinstance(APP_SDK_LEVEL, int) and not isinstance(APP_SDK_LEVEL, bool)
    assert APP_SDK_LEVEL >= 1
