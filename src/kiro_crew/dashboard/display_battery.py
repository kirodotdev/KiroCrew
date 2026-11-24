"""The display redaction battery, and the source files its output depends on.

``display_battery(text)`` is what the dashboard runs over every non-user string
before showing it: the credential pass (``redact_credentials``), which masks every
credential value, inside a URL or not. ``dashboard.display_verdicts`` stores its
verdicts across restarts under a rules version, and that version must change
exactly when the battery's output for some (text, token-parameter prefixes) pair
can change. So the battery lives here, beside :data:`RULE_SOURCES`, the list of
files whose bytes go into the version: the files the battery executes and the
package modules they read their rules from.

What is NOT in the list, and why that is safe:

* the token-parameter prefix source (``platform.context``, companion policy):
  its OUTPUT, the prefix set, is part of every verdict tag;
* the owner switch's reader and writer (``config.loader``, ``atomic_write``,
  ``platform.governance``): the published setting is part of every tag, and the
  owner-view scope turns storage off;
* the URL egress gates (``security.exfil`` and its owners): they decide whether
  a command or a URL may leave, and the battery never calls them;
* everything else in the package: the battery does not execute it.

``test_display_battery_rules_version.py`` traces the battery and fails when it
executes a package file that is not listed here, and pins each module's
package-level imports, so a new dependency cannot slip in unversioned.

The trace sees code, not data. A battery input that is read at run time -- a
config value, an environment variable, a data file, an operator-added pattern
-- must be folded into the verdict tag (as the prefix set is) or into
``display_verdicts.rules_version``, and the owner-view switch turns storage off
altogether. Adding such an input without doing so would let a stored "clean"
verdict outlive the rule that would now redact the text.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from kiro_crew.security.redaction import redact_credentials

#: Package-relative paths whose bytes are rules inputs. Order is part of the digest.
RULE_SOURCES: tuple[str, ...] = (
    "dashboard/display_battery.py",
    "security/redaction.py",
    "security/pem_scan.py",
    "security/redaction_switch.py",
    "redaction_seams.py",
    "credential_patterns.py",
    "default_ignorable.py",
    "jwt_header.py",
    "security/redaction_patterns.py",
    "security/redaction_entropy.py",
) + tuple(
    # Every structured detector file, so a detector change gives every stored
    # verdict a new version, whichever file it was made in.
    "security/redaction_structured/" + path.name
    for path in sorted(
        (Path(__file__).resolve().parent.parent / "security" / "redaction_structured").glob("*.py")
    )
)

_PKG_ROOT = Path(__file__).resolve().parent.parent


def display_battery(
    text: str,
    *,
    token_param_prefixes: tuple[str, ...] | None = None,
) -> str:
    """Every display redactor: the credential pass.

    ``token_param_prefixes`` is the caller's snapshot of the pass-4 prefix set
    (see ``redact_credentials``); ``None`` is the live read.
    """
    out, _ = redact_credentials(text, token_param_prefixes=token_param_prefixes)
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
#: (``redaction`` was imported just above). A version computed
#: later from disk is only trusted when it still matches this.
try:
    LOADED_DIGEST: bytes | None = rule_source_digest()
except OSError:
    LOADED_DIGEST = None
