"""Route registration for custom themes and installed-theme asset serving.

One contiguous slice of the dashboard's route table, kept in its original
order. aiohttp resolves routes in REGISTRATION order, and several routes here
rely on a literal path being registered before a pattern that would otherwise
swallow it, so neither the lines within this function nor the order in which
``server.start_dashboard`` calls the registrars may be rearranged.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard import handlers


def register(app: web.Application) -> None:
    """Register the themes routes on *app*."""
    # Custom Themes (CRUD)
    app.router.add_get("/api/themes", handlers.api_themes)
    app.router.add_post("/api/themes", handlers.api_themes_create)
    app.router.add_post("/api/themes/install", handlers.api_themes_install)
    app.router.add_get("/api/themes/{slug}", handlers.api_theme_detail)
    app.router.add_put("/api/themes/{slug}", handlers.api_theme_detail)
    app.router.add_delete("/api/themes/{slug}", handlers.api_theme_detail)
    # Installed-theme asset serving (L1/L2)
    app.router.add_get("/api/theme/{slug}/assets/{path:.+}", handlers.api_theme_asset)
    app.router.add_get("/api/theme/{slug}/overlay/{id}", handlers.api_theme_overlay)
    app.router.add_get("/api/theme/{slug}/topbar/{mode}", handlers.api_theme_topbar)
