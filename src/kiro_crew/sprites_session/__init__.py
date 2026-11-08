"""Fly.io Sprites as a session environment: a persistent Linux VM per session.

The engine stays on the gateway, as for every session environment. A sprite holds
only the workspace and the guest worker, which the gateway installs and starts
through the Sprites exec API and reaches through its TCP proxy, with the Sprites
API token held on the gateway alone.
"""
