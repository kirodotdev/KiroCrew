"""Mood Music — songs that fit how you feel right now.

The person says how they feel, or names a song that moved them. The app's DJ
agent finds songs with the same feeling on the web and answers with a small
JSON block; the page checks every link, queues the picks and plays them:
YouTube inside the page through the privacy-enhanced embed, bilibili in a new
browser tab. Moments and like/skip feedback live in the app's own storage, and
the page sends a short taste summary with each request so the next picks use it.
"""

# Required re-export: the startup route loop imports this package and calls
# ``register_routes`` on it (not on the backend.routes submodule).
from .backend.routes import register_routes  # noqa: F401
