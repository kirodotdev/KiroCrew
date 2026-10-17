"""The display redaction battery, and the source files its output depends on.

``display_battery(text)`` is what the dashboard runs over every non-user string
before showing it. ``dashboard.display_verdicts`` stores its verdicts across
restarts under a rules version, and that version must change exactly when the
battery's output for some (text, exempt hosts) pair can change. So the battery
lives here, beside :data:`RULE_SOURCES`, the list of files whose bytes go into
the version.

What is NOT in the list, and why that is safe:

* the exempt-host sources (``platform/*``, companion policy, per-workspace
  allowed hosts): their OUTPUT, the host set, is part of every verdict tag;
* ``kiro_crew.sel`` and ``security.denied_rules``: imported by ``exfil`` for the
  SEL audit and the bash audit, which the battery never calls;
* everything else in the package: the battery does not execute it.

``test_display_battery_rules_version.py`` traces the battery and fails when it
executes a package file that is not listed here, and pins each module's
package-level imports, so a new dependency cannot slip in unversioned.

The trace sees code, not data. A battery input that is read at run time -- a
config value, an environment variable, a data file, an operator-added pattern
-- must be folded into the verdict tag (as the exempt hosts are) or into
``display_verdicts.rules_version``, and the owner-view switch turns storage off
altogether. Adding such an input without doing so would let a stored "clean"
verdict outlive the rule that would now redact the text.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import kiro_crew.slack_manifest  # noqa: F401  (loaded before LOADED_DIGEST below)
from kiro_crew.security.exfil import redact_exfiltration_urls
from kiro_crew.security.redaction import redact_credentials

#: Package-relative paths whose bytes are rules inputs. Order is part of the digest.
RULE_SOURCES: tuple[str, ...] = (
    "dashboard/display_battery.py",
    "security/redaction.py",
    "security/pem_scan.py",
    "security/redaction_switch.py",
    "security/exfil.py",
    "credential_patterns.py",
    "slack_manifest.py",
    "slack-manifest.yaml",
)

_PKG_ROOT = Path(__file__).resolve().parent.parent


def display_battery(text: str) -> str:
    """Every display redactor, in order."""
    out, _ = redact_exfiltration_urls(text)
    out, _ = redact_credentials(out)
    return out


def rule_source_digest(root: Path | None = None) -> bytes:
    """SHA-256 over :data:`RULE_SOURCES` read from *root* (the package by default).

    Raises ``OSError`` when a file is missing: without the bytes there is no
    version, and the caller must not store verdicts.
    """
    root = _PKG_ROOT if root is None else root
    h = hashlib.sha256()
    for rel in RULE_SOURCES:
        data = (root / rel).read_bytes()
        h.update(rel.encode() + b"\0" + len(data).to_bytes(8, "big"))
        h.update(data)
    return h.digest()


#: The digest as of import, i.e. of the code this process actually loaded
#: (``exfil`` and ``redaction`` were imported just above). A version computed
#: later from disk is only trusted when it still matches this.
try:
    LOADED_DIGEST: bytes | None = rule_source_digest()
except OSError:
    LOADED_DIGEST = None
