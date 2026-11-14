"""Mood Music's registrations in the core's :mod:`kiro_crew.apps.seams`.

The page talks to its DJ agent through one worker slot created under the app's
own mode, so that slot stays off the chat sidebar, which lists only ``""`` slots.
Imports nothing of the app: the core loads this module during its own import.
"""

from __future__ import annotations

from kiro_crew.apps import seams

#: The manifest name, which is also the worker slot mode.
APP_ID = "mood-music"

seams.register_worker_slot_mode(APP_ID)
