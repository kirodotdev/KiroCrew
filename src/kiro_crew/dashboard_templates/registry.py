"""Every dashboard template, one line each.

The line is the registration: slug, contract type, provider, source fold, contract
version. Adding a template means adding its four files and one row here; the gates in
``test_dashboard_templates.py`` then apply to it without being extended, which is the
point of the row existing at all.

``fold`` must name a projection the product already keeps. A template whose numbers
have no fold behind them is a template whose numbers were typed by somebody, and that
is the thing these contracts exist to remove. Choosing a NEW fold instead is the
exception and carries its own argument in the pull request that adds it.

The registry is deliberately allowed to be EMPTY. It ships the machinery, and the
first templates arrive with the work that needs them. An empty registry cannot make
the gates read as passing: they run their own planted-failure cases, so a vacuous
green is not reachable here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from kiro_crew.dashboard_templates import TemplateSpec

__all__ = ["REGISTRY", "html_path", "spec_for"]

#: slug -> (contract, provider, source fold). One line per template.
REGISTRY: Final[dict[str, TemplateSpec]] = {}


def spec_for(slug: str) -> TemplateSpec:
    """The registered template *slug*, or :class:`KeyError` naming what is registered."""
    try:
        return REGISTRY[slug]
    except KeyError:
        raise KeyError(f"no dashboard template {slug!r}; registered: {sorted(REGISTRY)}") from None


def html_path(slug: str) -> Path:
    """The page file a template's slug names.

    Package-relative, resolved from this module's own location, so a wheel install and a
    source checkout answer the same way. That is also the reason the page needs its own
    packaging entry: this path exists in a checkout whether or not the build copied the
    file, so a page missing from the wheel renders an empty card with every gate green.
    """
    return Path(__file__).resolve().parent / f"{slug}.html"
