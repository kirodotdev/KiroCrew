"""Persisted cloud-launcher config — **profile name only, never credentials**.

Stores the AWS *profile name*, region, and the most-recent instance tag under
``~/.kirocrew/cloud.json``. AWS credentials are never written here — they are
resolved by the ``aws`` CLI's own provider chain from the profile.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

_FILENAME = "cloud.json"
DEFAULT_REGION = "us-east-1"

# Cap at 51 (not 63) to match ec2._TAG_RE / validate_tag: a longer last_tag
# would pass THIS sanitizer but then raise ValidationError on resume (the IAM
# role name kirocrew-ec2-<tag> maxes at 64), defeating the "just treat it as no
# last launch" intent. Keep in lockstep with ec2._TAG_RE.
_TAG_RE = re.compile(r"^[a-zA-Z0-9-]{1,51}$")


@dataclass
class CloudConfig:
    """The launcher's saved state (no secrets)."""

    profile: str = ""
    region: str = DEFAULT_REGION
    last_tag: str = ""

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "CloudConfig":
        p = path or (config_dir() / _FILENAME)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        # A hand-edited cloud.json may parse to valid JSON that is NOT an object
        # (e.g. `"hello"`, `[1,2]`, `42`, `null`); the .get() calls below would
        # then raise AttributeError and escape load() (handle_cloud only catches
        # AWS/validation errors), giving a raw traceback on every cloud command.
        # Honor the docstring's tolerate-a-corrupt-file promise: fall back to
        # defaults on any non-object shape.
        if not isinstance(data, dict):
            return cls()
        # Sanitize last_tag at the boundary: a hand-edited/corrupt cloud.json
        # must not carry a malformed tag into the resume path (downstream
        # validate_tag would raise; an empty tag just means "no last launch").
        last_tag = str(data.get("last_tag", ""))
        if last_tag and not _TAG_RE.match(last_tag):
            last_tag = ""
        return cls(
            profile=str(data.get("profile", "")),
            region=str(data.get("region", "") or DEFAULT_REGION),
            last_tag=last_tag,
        )

    def save(self, path: Optional[Path] = None) -> None:
        p = path or (config_dir() / _FILENAME)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp name per writer: concurrent cloud invocations must not
        # race on a shared .tmp path (see atomic_write's rationale, Mesh-100).
        atomic_write(p, json.dumps(asdict(self), indent=2))
