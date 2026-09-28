# Task Runner builtin app — hand over a multi-step job and let it run unattended.
#
# Manifest-only, like agent-worlds and channels: the dashboard page
# (website/src/pages/ProjectsPage.tsx) and the /api/taskrunner handlers
# (kiro_crew.dashboard.handlers.taskrunner) are part of the host, so there is no
# ``register_routes`` to re-export here. The package exists purely so
# ``discover_builtin_apps()`` finds app.json next to it, the same way it does
# for every other builtin.
