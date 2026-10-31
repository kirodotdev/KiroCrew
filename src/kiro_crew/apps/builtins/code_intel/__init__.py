"""Code intelligence for the dashboard's file viewer, served by language servers.

The gateway runs one Language Server Protocol server per (language, project root)
on demand and answers go-to-definition, find-references, hover and document-symbol
requests for the file the dashboard has open. See
``docs/system-specs/modules/code-intel.md``.
"""

from kiro_crew.apps.builtins.code_intel.backend.routes import register_routes
from kiro_crew.apps.builtins.code_intel.manager import (
    CodeIntelError,
    LspManager,
    code_intel_manager,
    shutdown_code_intel,
)
from kiro_crew.apps.builtins.code_intel.servers import SERVER_SPECS, ServerSpec, spec_for_path

__all__ = [
    "SERVER_SPECS",
    "CodeIntelError",
    "LspManager",
    "ServerSpec",
    "code_intel_manager",
    "register_routes",
    "shutdown_code_intel",
    "spec_for_path",
]
