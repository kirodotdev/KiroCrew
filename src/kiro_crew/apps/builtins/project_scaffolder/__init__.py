# Create Folders From Project builtin app — scan a project tree, then create the
# sidebar folders it implies.
#
# Manifest-only, like projects and agent-worlds: the dashboard page
# (website/src/apps/project-scaffolder/ProjectScaffolderPage.tsx) is part of the
# host bundle, and the two endpoints it calls
# (POST /api/project-scaffold/scan and POST /api/project-scaffold/create, in
# kiro_crew.dashboard.chat_folder_scaffold) are host routes registered by the
# gateway. So there is no ``register_routes`` to re-export, and deliberately no
# scan or create logic here — a second copy of either would be a second answer to
# "what does this tree contain", and the server's answer is the one the scaffold
# step re-derives its selection from.
#
# The package exists purely so ``discover_builtin_apps()`` finds app.json next to
# it, the same way it does for every other builtin.
