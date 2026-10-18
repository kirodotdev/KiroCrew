"""Shared pieces for ``test_dashboard_templates.py`` and ``test_dashboard_templates_readers.py``.

``ROOT`` (the checkout root), ``PAGE`` (a two-binding page) and the ``_Card`` and
``_MaybeCountCard`` contracts, which cases in both files read.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from kiro_crew.dashboard_templates import Unsaid

ROOT = Path(__file__).resolve().parents[1]


PAGE = """<!doctype html>
<html><body>
  <span data-dashboard-field="lede"></span>
  <span data-dashboard-field="open"></span>
</body></html>
"""


class _Card(TypedDict):
    lede: str | Unsaid
    open: str | Unsaid


class _MaybeCountCard(TypedDict):
    failed: int | Unsaid
