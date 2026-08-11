"""code-review-sage builtin app — GitHub PR review (KiroCrew OSS port)."""
from .backend.routes import on_disable, register_routes

__all__ = ["register_routes", "on_disable"]
