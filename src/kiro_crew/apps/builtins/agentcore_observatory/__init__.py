"""AgentCore Observatory — read-only visibility into Amazon Bedrock AgentCore.

Ships default-disabled: it is an opt-in app for operators who run AgentCore
workloads, and it contributes nothing to a crew that never enables it.
"""

# Required re-export: dashboard/routes/system.py imports the PACKAGE and checks
# hasattr(module, "register_routes") on it — not on backend.routes. Without this
# line the manifest's backend.routes string is documentary and the routes
# silently never register.
from kiro_crew.apps.builtins.agentcore_observatory.backend.routes import (  # noqa: F401
    register_routes,
)
