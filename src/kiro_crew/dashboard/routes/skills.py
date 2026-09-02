"""Route registration for prompts, skills CRUD and browser, skill discovery, steering files.

One contiguous slice of the dashboard's route table, kept in its original
order. aiohttp resolves routes in REGISTRATION order, and several routes here
rely on a literal path being registered before a pattern that would otherwise
swallow it, so neither the lines within this function nor the order in which
``server.start_dashboard`` calls the registrars may be rearranged.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.handlers.discover import (
    api_skills_discover,
    api_skills_discover_install,
    api_skills_discover_preview,
)


def register(app: web.Application) -> None:
    """Register the skills routes on *app*."""
    # Prompts (Agent SOPs)
    app.router.add_get("/api/prompts", handlers.api_prompts)
    app.router.add_post("/api/prompts", handlers.api_prompts_create)
    app.router.add_get("/api/prompts/{name:.+}", handlers.api_prompt_detail)
    app.router.add_put("/api/prompts/{name:.+}", handlers.api_prompt_detail)
    app.router.add_delete("/api/prompts/{name:.+}", handlers.api_prompt_detail)

    # Skills (CRUD + directory browser).  The browser routes use a ``/-/``
    # separator (GitLab-style) before tree/file so they can't collide with a
    # nested skill whose own last path segment is literally ``tree`` or
    # ``file`` (e.g. ``utils/tree`` → ``GET /api/skills/utils/tree`` is the
    # detail endpoint, not the browser).  They're still registered before the
    # catch-all {name:.+} so aiohttp reaches them first.
    app.router.add_get("/api/skills", handlers.api_skills)
    app.router.add_post("/api/skills", handlers.api_skills_create)
    # Multi-provider skill discovery (skills.sh REST browser)
    app.router.add_get("/api/skills/-/discover", api_skills_discover)
    app.router.add_get("/api/skills/-/discover/preview", api_skills_discover_preview)
    app.router.add_post("/api/skills/-/discover/install", api_skills_discover_install)
    # Auto-skill pending-approval queue + pin (v2). Registered before the
    # catch-all {name:.+} so the ``-`` sentinel paths resolve first.
    app.router.add_get("/api/skills/-/pending", handlers.api_skills_pending)
    app.router.add_post("/api/skills/-/pending/-/dismiss-all", handlers.api_skills_pending_dismiss_all)
    app.router.add_get("/api/skills/-/pending/{slug}", handlers.api_skill_pending_detail)
    app.router.add_post("/api/skills/-/pending/{slug}/approve", handlers.api_skill_pending_approve)
    app.router.add_post("/api/skills/-/pending/{slug}/dismiss", handlers.api_skill_pending_dismiss)
    app.router.add_post("/api/skills/-/pin", handlers.api_skill_pin)
    app.router.add_post("/api/skills/-/inject-on-trigger", handlers.api_skill_inject_on_trigger)
    # Skill context budget (read-only cost analysis with alias folding).
    app.router.add_get("/api/skills/-/budget", handlers.api_skills_budget)
    # Project-skills trust grants (per-directory consent). Registered before the
    # catch-all {name:.+} so the ``-`` sentinel path resolves first.
    app.router.add_get("/api/skills/-/trust", handlers.api_skills_trust)
    app.router.add_post("/api/skills/-/trust", handlers.api_skills_trust_grant)
    app.router.add_delete("/api/skills/-/trust", handlers.api_skills_trust_revoke)
    app.router.add_get("/api/skills/{name:.+}/-/tree", handlers.api_skill_tree)
    app.router.add_get("/api/skills/{name:.+}/-/file", handlers.api_skill_file)
    app.router.add_get("/api/skills/{name:.+}", handlers.api_skill_detail)
    app.router.add_put("/api/skills/{name:.+}", handlers.api_skill_detail)
    app.router.add_delete("/api/skills/{name:.+}", handlers.api_skill_detail)

    # Kiro steering files (~/.kiro/steering + <project>/.kiro/steering).  Plain
    # markdown documents, so no tree browser — the key is ``<source>/<relpath>``
    # and the fixed list/create route is registered before the catch-all
    # {key:.+} detail routes so aiohttp reaches it first.
    app.router.add_get("/api/steering", handlers.api_steering)
    app.router.add_post("/api/steering", handlers.api_steering_create)
    app.router.add_get("/api/steering/{key:.+}", handlers.api_steering_detail)
    app.router.add_put("/api/steering/{key:.+}", handlers.api_steering_detail)
    app.router.add_delete("/api/steering/{key:.+}", handlers.api_steering_detail)
