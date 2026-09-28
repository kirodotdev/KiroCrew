# Manifest-only builtin: app.json is the whole app on the gateway side. The
# Channels surface itself lives in the core (``kiro_crew/channel.py`` plus
# ``handlers_channel.py``), so there is deliberately no code here and no
# ``register_routes`` to re-export.
#
# The package exists so this app's own ``tests/`` resolves under
# ``kiro_crew.apps.builtins.channels`` rather than as a top-level ``tests``.
# ``setup.cfg`` sets ``testpaths = test src/kiro_crew/apps/builtins``, so every
# app's ``tests/`` IS collected, and more than one app ships a module named
# ``test_platform_declaration.py``. Without this file those basenames collide
# and pytest fails collection outright with an import-file-mismatch error —
# which is why an app that ships tests must be a package, not merely a folder.
