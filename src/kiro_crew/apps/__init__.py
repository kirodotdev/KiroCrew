"""KiroCrew App Platform — federated agentic app ecosystem."""
from __future__ import annotations

from kiro_crew.apps.bridges import (
    RegistrationResult,
    deregister_app,
    deregister_app_crons_from_service,
    load_app_cron_defs,
    register_app,
    register_app_crons_with_service,
)
from kiro_crew.apps.manager import (
    AppResult,
    InstalledApp,
    app_data_dir,
    app_dir,
    apps_dir,
    disable_app,
    enable_app,
    get_app,
    get_app_manifest,
    install_app,
    list_apps,
    uninstall_app,
)
from kiro_crew.apps.manifest import AppManifest

__all__ = [
    "AppManifest",
    "AppResult",
    "InstalledApp",
    "RegistrationResult",
    "app_data_dir",
    "app_dir",
    "apps_dir",
    "deregister_app",
    "deregister_app_crons_from_service",
    "disable_app",
    "enable_app",
    "get_app",
    "get_app_manifest",
    "install_app",
    "list_apps",
    "load_app_cron_defs",
    "register_app_crons_with_service",
    "register_app",
    "uninstall_app",
]
