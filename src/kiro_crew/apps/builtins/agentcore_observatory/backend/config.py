"""AgentCore Observatory — persisted connection config, **names only**.

Stores the AWS *profile name* and *region* under the platform-standard
app-scoped data dir (``~/.kiro/crew/apps/agentcore-observatory/data/config.json``
via :func:`kiro_crew.apps.manager.app_data_dir`). Credentials are never read,
written, or cached here: every AWS call resolves them through the ``aws`` CLI's
own provider chain from the profile name, at the
:func:`kiro_crew.cloud.aws.run_aws` chokepoint.

Both fields are re-validated on READ, not only on write. The file is ordinary
app config a user (or an agent with filesystem access) can hand-edit, so a
malformed value must not reach an argv. An invalid value reads back as empty —
fail closed — which the caller reports as "not configured" rather than
substituting a guess.

``region`` deliberately has **no default**, unlike ``cloud.json``'s
``us-east-1``. A defaulted region in a read-only observability surface is the
worst failure mode available: the wrong region returns an empty runtime list,
which renders as a healthy account with nothing deployed rather than as a
misconfiguration. The region a user's workloads live in is a fact to be
supplied, not inferred.

``root`` is accepted on every function (mirroring ``issue_radar``'s
``store.py``) so tests can point at a tmp dir instead of the real app data dir.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.aws_consent import _PROFILE_RE
from kiro_crew.deploy.profiles import _REGION_RE

logger = logging.getLogger(__name__)

APP_NAME = "agentcore-observatory"

_FILENAME = "config.json"


def data_dir(root: Path | None = None) -> Path:
    """Return the app's data dir, creating it if missing."""
    data = root if root is not None else app_data_dir(APP_NAME)
    data.mkdir(parents=True, exist_ok=True)
    return data


def config_path(root: Path | None = None) -> Path:
    return data_dir(root) / _FILENAME


def valid_profile(name: str) -> bool:
    """Whether ``name`` is a well-formed AWS profile name."""
    return bool(_PROFILE_RE.match(name or ""))


def valid_region(name: str) -> bool:
    """Whether ``name`` is a well-formed AWS region code."""
    return bool(_REGION_RE.match(name or ""))


@dataclass
class ObservatoryConfig:
    """The app's saved connection state (no secrets).

    An empty ``profile`` means "use the CLI's default profile resolution"; an
    empty ``region`` means "not configured", which is a distinct state from any
    particular region and is never filled in for the user.
    """

    profile: str = ""
    region: str = ""

    @property
    def configured(self) -> bool:
        """Whether this config can be used for an AWS call.

        Only the region is required. An empty profile is a legitimate choice —
        it lets the CLI resolve its own default — but an empty region is not,
        because no call can be issued without one.
        """
        return bool(self.region)

    @classmethod
    def load(cls, root: Path | None = None) -> "ObservatoryConfig":
        """Read the config, tolerating every corruption mode.

        An unreadable file, invalid JSON, a valid-JSON non-object (a
        hand-edited ``"hello"`` or ``[1,2]``), and an out-of-shape field value
        all degrade to the unconfigured default rather than raising: this is
        read on the page-render path, and a corrupt file must render an empty
        connection form, not a traceback.
        """
        path = config_path(root)
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()

        profile = str(data.get("profile", "") or "")
        region = str(data.get("region", "") or "")
        if profile and not valid_profile(profile):
            logger.warning("%s: dropping malformed profile name from config", APP_NAME)
            profile = ""
        if region and not valid_region(region):
            logger.warning("%s: dropping malformed region from config", APP_NAME)
            region = ""
        return cls(profile=profile, region=region)

    def save(self, root: Path | None = None) -> None:
        """Persist the config atomically.

        Refuses to write a malformed value, so the validation on read is a
        second line of defence against hand edits rather than the only one.
        """
        if self.profile and not valid_profile(self.profile):
            raise ValueError("invalid AWS profile name")
        if self.region and not valid_region(self.region):
            raise ValueError("invalid AWS region")
        atomic_write(config_path(root), json.dumps(asdict(self), indent=2))

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the HTTP surface."""
        return {"profile": self.profile, "region": self.region, "configured": self.configured}
