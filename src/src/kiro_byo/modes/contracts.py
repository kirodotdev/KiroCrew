"""Built-in Mode plug-in contract.

Only BYO-shipped modules can declare built-in modes. A Mode owns its stable
Mode/Type identity and references an immutable, data-only policy resource.
The shared harness remains the sole owner of execution, tools and permissions.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuiltinMode:
    id: str
    policy_filename: str
    type_ids: tuple[str, ...]
    default_type: str
    enabled: bool
    registry_api: int = 1

    @property
    def policy_path(self) -> Path:
        # Shipped package resource; never resolved from user-supplied metadata.
        return Path(__file__).resolve().parents[1] / "policy_packs" / self.policy_filename
