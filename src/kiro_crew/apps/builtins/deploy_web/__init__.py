"""deploy-web builtin app — publish artifacts to the user's own AWS (S3 + CloudFront + OAC)."""
from .handlers import register_routes

__all__ = ["register_routes"]
