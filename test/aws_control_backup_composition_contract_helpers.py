"""Constants the AWS Control backup composition-contract tests share.

``PARTS`` holds the engine's owner modules in the facade's declared layer order, lowest
first; ``_REPO_ROOT`` is the repository root, resolved from this module's own path in
``test/``; ``_PARTS_PACKAGE`` is the dotted name of the ``backup_parts`` package.
``test_aws_control_backup_composition_contract.py`` and
``test_aws_control_backup_composition_contract_patch_guards.py`` import them by name.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

from kiro_crew.apps.builtins.aws_control.backend.backup_parts import (
    archive_inputs,
    catalog,
    conversations,
    egress_text,
    fingerprints,
    identity,
    layer_b,
    ledger,
    nightly,
    restore,
    retention,
    state,
    traversal,
    uploads,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


_PARTS_PACKAGE = "kiro_crew.apps.builtins.aws_control.backend.backup_parts"

#: The owners in the facade's declared layer order, lowest first.
PARTS: tuple[ModuleType, ...] = (
    egress_text,
    state,
    identity,
    fingerprints,
    traversal,
    ledger,
    layer_b,
    nightly,
    uploads,
    catalog,
    retention,
    archive_inputs,
    conversations,
    restore,
)
