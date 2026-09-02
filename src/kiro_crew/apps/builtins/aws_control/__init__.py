"""AWS Control — the account portal over the user's own AWS accounts.

One surface answers "which accounts can Kiro Crew use, do their credentials
still work, and what does that cost" — and later phases grow the S3-backed
cloud drive (Drive / Library / Backup) plus sharing on the same console.
Spec: ``docs/system-specs/features/aws-control.md``.

Architecture — in-process builtin, like ``issue_radar`` and ``crew_companion``:

* **This Python package** owns account aggregation. It reads the existing
  deploy profile registry (``kiro_crew.deploy.profiles`` — names, regions and
  display metadata only, never credential material) and groups profiles by the
  AWS account they resolve to, using the same free ``sts:GetCallerIdentity``
  probe the aws-consent surface runs. All AWS access goes through the deploy
  engine's single CLI chokepoint (``deploy.engine.run_aws``), gateway-side.
* ``website/src/apps/aws-control/`` owns the dashboard page, served
  same-origin by the gateway.

P0 is deliberately read-only against AWS: no bucket exists yet, nothing is
billed, and there is no mutating endpoint. The paid services later phases use
(``s3`` for the drive, ``ce`` for the bill) are declared in the aws-consent
service enum now so the consent cards can be confirmed per account before the
first billable call ever ships.
"""

# Required re-export: dashboard/server.py's startup route registration imports
# the PACKAGE and checks hasattr(_mod, "register_routes") — the same convention
# crew_companion/__init__.py and issue_radar/__init__.py follow (the call site
# is the `for _builtin_name in BUILTIN_NAMES` loop). Routes are registered at
# STARTUP, not on enable, which is why every handler carries its own enabled
# check and answers 403 while the app is off.
from kiro_crew.apps.builtins.aws_control.backend.routes import (  # noqa: F401
    register_routes,
)
