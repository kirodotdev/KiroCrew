"""Kiro Crew App Platform — federated agentic app ecosystem.

The names below are bound on first access, not at package import. Importing any
``kiro_crew.apps.<module>`` runs this file first, and the stdio MCP servers read
installed apps' config keys through ``kiro_crew.apps.manager_records`` at start-up.
Binding these names eagerly made every such server load the bridges (history,
cron, sessions) and every manager owner, none of which it uses.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.apps.bridges import (  # noqa: F401
        RegistrationResult,
        deregister_app,
        deregister_app_crons_from_service,
        load_app_cron_defs,
        register_app,
        register_app_crons_with_service,
    )
    from kiro_crew.apps.manager import (  # noqa: F401
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
    from kiro_crew.apps.manifest import AppManifest  # noqa: F401

#: Public name -> the module that binds it.
_LAZY_EXPORTS: dict[str, str] = {
    "RegistrationResult": "kiro_crew.apps.bridges",
    "deregister_app": "kiro_crew.apps.bridges",
    "deregister_app_crons_from_service": "kiro_crew.apps.bridges",
    "load_app_cron_defs": "kiro_crew.apps.bridges",
    "register_app": "kiro_crew.apps.bridges",
    "register_app_crons_with_service": "kiro_crew.apps.bridges",
    "AppResult": "kiro_crew.apps.manager",
    "InstalledApp": "kiro_crew.apps.manager",
    "app_data_dir": "kiro_crew.apps.manager",
    "app_dir": "kiro_crew.apps.manager",
    "apps_dir": "kiro_crew.apps.manager",
    "disable_app": "kiro_crew.apps.manager",
    "enable_app": "kiro_crew.apps.manager",
    "get_app": "kiro_crew.apps.manager",
    "get_app_manifest": "kiro_crew.apps.manager",
    "install_app": "kiro_crew.apps.manager",
    "list_apps": "kiro_crew.apps.manager",
    "uninstall_app": "kiro_crew.apps.manager",
    "AppManifest": "kiro_crew.apps.manifest",
}

__all__ = sorted(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        # Not an export: let ``from kiro_crew.apps import <submodule>`` fall back
        # to importing the submodule.
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
