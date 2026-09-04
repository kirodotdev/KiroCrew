# Agent Worlds builtin app — running agents rendered as characters in an
# animated pixel-art scene.
#
# Manifest-only, like projects and project_scaffolder: the dashboard page
# (website/src/pages/WorldsPage.tsx, routed from website/src/apps/builtinRegistry.ts)
# is part of the host bundle, and there is no backend at all — app.json declares
# no ``backend`` key, so there is no ``register_routes`` to re-export and
# deliberately no Python here. The package exists purely so
# ``discover_builtin_apps()`` finds app.json next to it, the same way it does for
# every other builtin, and so this app's own ``tests/`` package resolves under
# ``kiro_crew.apps.builtins.agent_worlds`` instead of as a top-level ``tests``.
#
# Having NO code is also what makes the ``windows`` entry in app.json's
# ``platform.os`` honest: nothing here spawns a subprocess, so the app never
# reaches the sandbox layer that has no native Windows backend.
