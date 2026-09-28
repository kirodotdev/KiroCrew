# Manifest-only builtin: app.json declares a ui.overlay that REPLACES the
# dashboard's quick-search panel, and the implementation is browser-side
# TypeScript under ``website/src/apps/command-bar/``. There is no backend and
# deliberately no Python here, so there is no ``register_routes`` to re-export.
#
# The package exists so this app's own ``tests/`` resolves under
# ``kiro_crew.apps.builtins.command_bar`` rather than as a top-level ``tests``
# — ``setup.cfg`` sets ``testpaths = test src/kiro_crew/apps/builtins``, so an
# app's ``tests/`` folder that is not a package can collide with another app's
# identically-named test module and fail collection.
